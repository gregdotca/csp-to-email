"""The worker: claim digests, email them, and retry or record failures."""

import fcntl
import json
import logging
import os
import signal
import sys
import threading

from . import Clock, config as config_module, db, digest, mailer

log = logging.getLogger("csp_to_email.worker")

TICK_SECONDS = 5
HEARTBEAT_SECONDS = 30
HEARTBEAT_MAX_AGE = 90
SEND_MAX_SECONDS = 15 * 60
LOCK_POLL_SECONDS = 1


class SendState:
    """When the current SMTP send started, shared with the heartbeat thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._started = None

    def start(self, now):
        with self._lock:
            self._started = now

    def finish(self):
        with self._lock:
            self._started = None

    @property
    def started(self):
        with self._lock:
            return self._started


def run_once(conn, clock, cfg, send, send_state=None):
    """One worker pass: clean up, claim a digest if one is due, and send it if due.

    `send(message)` delivers an EmailMessage and returns the refused
    recipients, like mailer.send(). Returns what happened, for logs and tests:
    None, "sent", "retry", or "failed".
    """
    db.cleanup(conn, clock.now())
    db.claim_digest(conn, clock.now(), cfg.digest_interval)

    pending = db.pending_digest(conn)
    if pending is None or pending["next_attempt_at"] > clock.now():
        return None

    digest_id = pending["id"]
    data = db.load_digest(conn, digest_id)
    rendered = digest.render(data, clock.now())
    message = mailer.build_message(cfg, digest_id, pending["created_at"], rendered.subject, rendered.body)

    if send_state is not None:
        send_state.start(clock.now())
    try:
        refused = send(message)
    except Exception as error:
        description = mailer.describe(error)
        if mailer.is_permanent(error):
            db.mark_failed(conn, digest_id, clock.now(), description)
            log.error("Digest %s was rejected and won't be retried: %s", digest_id, description)
            return "failed"
        delay = db.retry_later(conn, digest_id, clock.now(), description)
        log.error("Digest %s couldn't be sent, retrying in %d min: %s", digest_id, delay // 60, description)
        return "retry"
    finally:
        if send_state is not None:
            send_state.finish()

    refused_text = mailer.format_refused(refused) if refused else None
    db.mark_sent(conn, digest_id, clock.now(), rendered.shown_fingerprints, rendered.group_count, refused_text)
    if refused_text:
        log.error("Digest %s was refused for some recipients: %s", digest_id, refused_text)
    log.info("Sent digest %s: %s", digest_id, rendered.subject)
    return "sent"


def write_heartbeat(path, clock, send_started):
    """Record that the worker is alive, and when its current send started."""
    temporary = f"{path}.tmp"
    with open(temporary, "w") as file:
        json.dump({"time": clock.now(), "send_started": send_started}, file)
    os.replace(temporary, path)


def healthcheck(path, clock):
    """Return (healthy, message) from the heartbeat file."""
    try:
        with open(path) as file:
            beat = json.load(file)
        beat_time = float(beat["time"])
        send_started = beat.get("send_started")
    except (OSError, ValueError, KeyError, TypeError) as error:
        return False, f"no readable heartbeat at {path}: {error}"
    age = clock.now() - beat_time
    if age > HEARTBEAT_MAX_AGE:
        return False, f"heartbeat is {age:.0f} seconds old (limit {HEARTBEAT_MAX_AGE})"
    if send_started is not None and clock.now() - float(send_started) > SEND_MAX_SECONDS:
        return False, f"a send has been running for {clock.now() - float(send_started):.0f} seconds"
    return True, f"heartbeat {age:.0f} seconds old"


def acquire_lock(path, stop, on_standby=None):
    """Take the worker lock, waiting as a standby if another worker holds it.

    Returns the open lock file, which must stay open to keep the lock, or
    None if `stop` was set while waiting. Polls rather than blocking in
    flock, which would keep a standby from stopping on SIGTERM.
    """
    lock_file = open(path, "a")
    standing_by = False
    while True:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock_file
        except BlockingIOError:
            if not standing_by:
                standing_by = True
                if on_standby is not None:
                    on_standby()
        if stop.wait(LOCK_POLL_SECONDS):
            lock_file.close()
            return None


def _signal(text):
    # Test-only synchronisation: tests read these lines to know the lock state.
    if os.environ.get("CSP2E_TEST_SIGNALS") == "1":
        print(text, flush=True)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = config_module.load()
        config_module.check_smtp(cfg)
        config_module.check_data_dir(cfg)
    except config_module.ConfigError as error:
        log.error("%s", error)
        return 1

    stop = threading.Event()

    def request_stop(signum, frame):
        log.info("Stopping after the current pass")
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    def on_standby():
        log.info("Another worker is running; standing by")
        _signal("STANDBY")

    lock_file = acquire_lock(cfg.lock_path, stop, on_standby)
    if lock_file is None:
        return 0
    _signal("LOCKED")
    log.info("Worker started; digest interval %s seconds", cfg.digest_interval)

    clock = Clock()
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    db.reset_in_flight(conn, clock.now())

    send_state = SendState()

    def heartbeat():
        while True:
            try:
                write_heartbeat(cfg.heartbeat_path, clock, send_state.started)
            except OSError as error:
                log.error("Couldn't write the heartbeat: %s", error)
            if stop.wait(HEARTBEAT_SECONDS):
                return

    threading.Thread(target=heartbeat, name="heartbeat", daemon=True).start()

    def send(message):
        return mailer.send(cfg, message)

    while not stop.is_set():
        try:
            run_once(conn, clock, cfg, send, send_state)
        except Exception:
            log.exception("Worker pass failed")
        stop.wait(TICK_SECONDS)

    conn.close()
    lock_file.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
