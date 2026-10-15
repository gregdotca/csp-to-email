"""The web app: accept CSP reports, store them, and return straight away."""

import atexit
import logging
import sqlite3
import threading
from collections import OrderedDict, deque
from urllib.parse import urlsplit

from flask import Flask, Response, request
from werkzeug.middleware.proxy_fix import ProxyFix

from . import Clock, config as config_module, db
from .reports import InvalidReport, UnsupportedType, parse_body

log = logging.getLogger("csp_to_email.web")

MAX_BODY_BYTES = 256 * 1024
MAX_BATCH = 100
RATE_WINDOW_SECONDS = 60
MAX_TRACKED_IPS = 10_000
FLUSH_SECONDS = 1
PREFLIGHT_MAX_AGE = 86400


class DropBuffer:
    """Dropped-report counts kept in memory and written to the database in batches.

    So a rejected report costs no database write. A flush takes the whole
    buffer; if writing it fails, the counts go back in for the next flush.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._counts = {}

    def add(self, name, count, now):
        if count <= 0:
            return
        with self._lock:
            current, first_at = self._counts.get(name, (0, now))
            self._counts[name] = (current + count, min(first_at, now))

    def snapshot(self):
        with self._lock:
            return dict(self._counts)

    def flush(self, conn):
        with self._lock:
            taken, self._counts = self._counts, {}
        if not taken:
            return
        try:
            db.add_drops(conn, taken)
        except BaseException:
            with self._lock:
                for name, (count, first_at) in taken.items():
                    current, current_first = self._counts.get(name, (0, first_at))
                    self._counts[name] = (current + count, min(first_at, current_first))
            raise


class RateLimiter:
    """A per-IP sliding window of accepted reports, bounded in memory."""

    def __init__(self, limit, window=RATE_WINDOW_SECONDS, max_ips=MAX_TRACKED_IPS):
        self.limit = limit
        self.window = window
        self.max_ips = max_ips
        self._lock = threading.Lock()
        # Ordered by last request, oldest first.
        self._ips = OrderedDict()

    def allow(self, ip, count, now):
        """Record up to `count` reports from `ip` and return how many are allowed."""
        cutoff = now - self.window
        with self._lock:
            self._sweep(cutoff)
            times = self._ips.get(ip)
            if times is None:
                if len(self._ips) >= self.max_ips:
                    self._ips.popitem(last=False)
                times = self._ips[ip] = deque()
            while times and times[0] <= cutoff:
                times.popleft()
            allowed = max(0, min(count, self.limit - len(times)))
            times.extend([now] * allowed)
            if times:
                self._ips.move_to_end(ip)
            else:
                del self._ips[ip]
            return allowed

    def refund(self, ip, count, now):
        """Give back `count` reports allowed at `now`, such as when storing them failed.

        Only entries with that request's timestamp are removed, so a refund
        can't cancel a later request's allowance.
        """
        with self._lock:
            times = self._ips.get(ip)
            if times is None:
                return
            kept = list(times)
            for index in range(len(kept) - 1, -1, -1):
                if count == 0:
                    break
                if kept[index] == now:
                    del kept[index]
                    count -= 1
            if kept:
                self._ips[ip] = deque(kept)
            else:
                del self._ips[ip]

    def _sweep(self, cutoff):
        # Drop IPs whose most recent report has left the window. The map is
        # ordered by last request, so they're at the front.
        while self._ips:
            ip, times = next(iter(self._ips.items()))
            if times and times[-1] > cutoff:
                break
            del self._ips[ip]

    def __len__(self):
        return len(self._ips)


def _host(uri):
    try:
        return (urlsplit(uri).hostname or "").lower()
    except ValueError:
        return ""


def _cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


def create_app(cfg=None, clock=None, start_flusher=True):
    cfg = cfg or config_module.load()
    clock = clock or Clock()
    config_module.check_data_dir(cfg)

    conn = db.connect(cfg.db_path)
    try:
        db.migrate(conn)
    finally:
        conn.close()

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES
    if cfg.proxy_hops:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=cfg.proxy_hops, x_proto=0, x_host=0, x_port=0, x_prefix=0)

    drops = DropBuffer()
    limiter = RateLimiter(cfg.rate_limit)
    # The request threads take turns writing. Left to SQLite's busy handler,
    # which retries without any fairness, one of them can wait out the whole
    # busy timeout under constant load while the others keep winning.
    write_lock = threading.Lock()
    app.extensions["csp_to_email"] = {"drops": drops, "limiter": limiter, "config": cfg}

    @app.route("/", methods=["POST", "OPTIONS"], provide_automatic_options=False)
    def ingest():
        if request.method == "OPTIONS":
            response = Response(status=204)
            response.headers["Access-Control-Allow-Methods"] = "POST"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type"
            response.headers["Access-Control-Max-Age"] = str(PREFLIGHT_MAX_AGE)
            return _cors(response)

        now = clock.now()
        try:
            reports = parse_body(
                request.mimetype,
                request.get_data(cache=False),
                user_agent=request.headers.get("User-Agent"),
                include_samples=cfg.include_samples,
            )
        except UnsupportedType:
            return _cors(Response("Unsupported content type\n", status=415, mimetype="text/plain"))
        except InvalidReport as error:
            return _cors(Response(f"Invalid report: {error}\n", status=400, mimetype="text/plain"))

        if len(reports) > MAX_BATCH:
            drops.add("batch_too_large", len(reports) - MAX_BATCH, now)
            reports = reports[:MAX_BATCH]

        if cfg.allowed_hosts:
            kept = [report for report in reports if _host(report["document_uri"]) in cfg.allowed_hosts]
            drops.add("host_not_allowed", len(reports) - len(kept), now)
            reports = kept

        ip = request.remote_addr or ""
        allowed = limiter.allow(ip, len(reports), now)
        drops.add("rate_limited", len(reports) - allowed, now)
        reports = reports[:allowed]

        if reports:
            try:
                with write_lock:
                    conn = db.connect(cfg.db_path)
                    try:
                        full = db.insert_reports(conn, reports, now, cfg.max_queued)
                    finally:
                        conn.close()
            except sqlite3.Error:
                # The reports weren't stored, so they don't count against the
                # client's rate limit if it retries.
                limiter.refund(ip, len(reports), now)
                log.exception("Couldn't store %d report(s)", len(reports))
                return _cors(Response("Temporarily unavailable\n", status=503, mimetype="text/plain"))
            drops.add("queue_full", full, now)

        return _cors(Response(status=204))

    @app.get("/healthz")
    def healthz():
        try:
            conn = db.connect(cfg.db_path)
            try:
                conn.execute("SELECT COUNT(*) FROM digests").fetchone()
            finally:
                conn.close()
        except sqlite3.Error as error:
            return Response(f"database unavailable: {error}\n", status=503, mimetype="text/plain")
        return Response("ok\n", mimetype="text/plain")

    if start_flusher:
        _start_flusher(cfg, drops)
    return app


class Flusher:
    """Saves the drop buffer every FLUSH_SECONDS, and once more at exit."""

    def __init__(self, cfg, drops):
        self.cfg = cfg
        self.drops = drops
        self.stop = threading.Event()
        # Held during a flush, so the exit handler waits for one in progress
        # rather than finding the buffer empty and exiting before it commits.
        self._flushing = threading.Lock()

    def flush_once(self):
        with self._flushing:
            try:
                conn = db.connect(self.cfg.db_path)
                try:
                    self.drops.flush(conn)
                finally:
                    conn.close()
            except Exception:
                log.exception("Couldn't save dropped-report counts; will retry")

    def run(self):
        while not self.stop.wait(FLUSH_SECONDS):
            self.flush_once()

    def at_exit(self):
        self.stop.set()
        self.flush_once()


def _start_flusher(cfg, drops):
    flusher = Flusher(cfg, drops)
    threading.Thread(target=flusher.run, name="drop-flusher", daemon=True).start()
    atexit.register(flusher.at_exit)
