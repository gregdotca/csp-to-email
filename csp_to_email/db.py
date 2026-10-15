"""SQLite storage: connections, the schema, and its migrations."""

import json
import sqlite3
import time
from contextlib import contextmanager

BUSY_TIMEOUT_SECONDS = 5
DAY = 24 * 60 * 60
SENT_DIGEST_DAYS = 30
FINGERPRINT_DAYS = 90
MAX_FINGERPRINTS = 50_000
MAX_BACKOFF_MINUTES = 60
# Errors and notices are stored truncated so they can't bloat a digest.
STORED_TEXT_LIMIT = 1000

# Each entry is the list of statements that upgrades the schema by one version;
# user_version records how many have been applied. Only ever append.
MIGRATIONS = [
    [
        """CREATE TABLE digests (
            id INTEGER PRIMARY KEY,
            created_at REAL NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed')),
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at REAL,
            last_error TEXT,
            sent_at REAL,
            report_count INTEGER NOT NULL DEFAULT 0,
            group_count INTEGER NOT NULL DEFAULT 0,
            dropped TEXT NOT NULL DEFAULT '{}',
            notices TEXT NOT NULL DEFAULT '[]'
        )""",
        "CREATE INDEX digests_status ON digests (status)",
        """CREATE TABLE reports (
            id INTEGER PRIMARY KEY,
            received_at REAL NOT NULL,
            fingerprint TEXT NOT NULL,
            document_uri TEXT NOT NULL,
            blocked_uri TEXT NOT NULL,
            directive TEXT NOT NULL,
            disposition TEXT,
            source_file TEXT,
            line_number INTEGER,
            column_number INTEGER,
            sample TEXT,
            user_agent TEXT,
            digest_id INTEGER REFERENCES digests (id)
        )""",
        "CREATE INDEX reports_digest_id ON reports (digest_id)",
        "CREATE INDEX reports_fingerprint ON reports (fingerprint)",
        """CREATE TABLE fingerprints (
            fingerprint TEXT PRIMARY KEY,
            first_seen REAL NOT NULL,
            last_seen REAL NOT NULL,
            total_count INTEGER NOT NULL,
            reported_at REAL
        )""",
        "CREATE INDEX fingerprints_last_seen ON fingerprints (last_seen)",
        """CREATE TABLE counters (
            name TEXT PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0,
            first_at REAL
        )""",
        """CREATE TABLE notices (
            id INTEGER PRIMARY KEY,
            created_at REAL NOT NULL,
            text TEXT NOT NULL
        )""",
    ],
]

REPORT_COLUMNS = (
    "fingerprint",
    "document_uri",
    "blocked_uri",
    "directive",
    "disposition",
    "source_file",
    "line_number",
    "column_number",
    "sample",
    "user_agent",
)


def connect(path):
    """Open the database in autocommit mode; callers manage transactions."""
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
    conn.row_factory = sqlite3.Row
    _enable_wal(conn)
    # In WAL mode, NORMAL skips the fsync on every commit. The database
    # can't be corrupted and a process crash loses nothing; a power cut
    # can lose the last few commits, which at worst means a few reports or
    # a resent digest.
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _enable_wal(conn):
    # Switching a new database to WAL can fail with "database is locked"
    # straight away, without waiting for the busy timeout, when several
    # processes open it at once (web and worker on first start). WAL mode is
    # stored in the file, so this only matters until one of them succeeds.
    deadline = time.monotonic() + BUSY_TIMEOUT_SECONDS
    while True:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as error:
            if error.sqlite_errorcode != sqlite3.SQLITE_BUSY or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


@contextmanager
def transaction(conn):
    """Run the block in one write transaction, rolling back if anything fails."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def migrate(conn):
    """Bring the schema up to date. Safe to run from several processes at once."""
    with transaction(conn):
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for number, statements in enumerate(MIGRATIONS[version:], start=version + 1):
            for statement in statements:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {number}")


def insert_reports(conn, reports, now, max_queued):
    """Store reports and update their fingerprints, up to the queue cap.

    The cap counts every stored report (pending, in an unsent digest, or in a
    failed digest). Returns how many reports were dropped because the queue
    was full.
    """
    if not reports:
        return 0
    with transaction(conn):
        stored = conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
        room = max(0, max_queued - stored)
        accepted = reports[:room]
        placeholders = ", ".join("?" for _ in REPORT_COLUMNS)
        for report in accepted:
            conn.execute(
                f"INSERT INTO reports (received_at, {', '.join(REPORT_COLUMNS)}) "
                f"VALUES (?, {placeholders})",
                (now, *(report[column] for column in REPORT_COLUMNS)),
            )
            conn.execute(
                "INSERT INTO fingerprints (fingerprint, first_seen, last_seen, total_count) "
                "VALUES (?, ?, ?, 1) "
                "ON CONFLICT (fingerprint) DO UPDATE SET "
                "last_seen = excluded.last_seen, total_count = total_count + 1",
                (report["fingerprint"], now, now),
            )
    return len(reports) - len(accepted)


def add_drops(conn, drops):
    """Add dropped-report counts, given as {name: (count, earliest drop time)}."""
    with transaction(conn):
        for name, (count, first_at) in drops.items():
            _add_counter(conn, name, count, first_at)


def _add_counter(conn, name, count, first_at):
    # first_at keeps the earliest time of any drop not yet in a digest.
    if count <= 0:
        return
    conn.execute(
        "INSERT INTO counters (name, value, first_at) VALUES (?, ?, ?) "
        "ON CONFLICT (name) DO UPDATE SET "
        "value = value + excluded.value, "
        "first_at = CASE WHEN first_at IS NULL THEN excluded.first_at "
        "ELSE MIN(first_at, excluded.first_at) END",
        (name, count, first_at),
    )


def add_notice(conn, now, text):
    """Queue a one-off message for the next digest. Call inside a transaction."""
    conn.execute(
        "INSERT INTO notices (created_at, text) VALUES (?, ?)", (now, text[:STORED_TEXT_LIMIT])
    )


def pending_digest(conn):
    """The digest in flight, or None."""
    return conn.execute("SELECT * FROM digests WHERE status = 'pending' ORDER BY id LIMIT 1").fetchone()


def claim_digest(conn, now, interval):
    """Start a new digest if one is due. Returns its id, or None.

    A digest is due when nothing is in flight and the oldest pending report,
    uncounted drop, or notice is at least `interval` seconds old. It takes
    every pending report, and moves the drop counters and notices into itself.
    """
    with transaction(conn):
        if pending_digest(conn) is not None:
            return None
        oldest = conn.execute(
            "SELECT MIN(t) FROM ("
            "SELECT MIN(received_at) AS t FROM reports WHERE digest_id IS NULL "
            "UNION ALL SELECT MIN(first_at) FROM counters WHERE value > 0 "
            "UNION ALL SELECT MIN(created_at) FROM notices)"
        ).fetchone()[0]
        if oldest is None or now - oldest < interval:
            return None

        counters = conn.execute("SELECT * FROM counters WHERE value > 0 ORDER BY name").fetchall()
        dropped = {row["name"]: {"count": row["value"], "first_at": row["first_at"]} for row in counters}
        notices = conn.execute("SELECT * FROM notices ORDER BY id").fetchall()
        notice_list = [{"created_at": row["created_at"], "text": row["text"]} for row in notices]

        digest_id = conn.execute(
            "INSERT INTO digests (created_at, status, next_attempt_at, dropped, notices) "
            "VALUES (?, 'pending', ?, ?, ?)",
            (now, now, json.dumps(dropped), json.dumps(notice_list)),
        ).lastrowid
        max_id = conn.execute("SELECT MAX(id) FROM reports WHERE digest_id IS NULL").fetchone()[0]
        if max_id is not None:
            claimed = conn.execute(
                "UPDATE reports SET digest_id = ? WHERE digest_id IS NULL AND id <= ?",
                (digest_id, max_id),
            ).rowcount
            conn.execute("UPDATE digests SET report_count = ? WHERE id = ?", (claimed, digest_id))
        for row in counters:
            conn.execute(
                "UPDATE counters SET value = value - ?, "
                "first_at = CASE WHEN value - ? <= 0 THEN NULL ELSE first_at END WHERE name = ?",
                (row["value"], row["value"], row["name"]),
            )
        if notices:
            conn.execute("DELETE FROM notices WHERE id <= ?", (notices[-1]["id"],))
    return digest_id


def load_digest(conn, digest_id):
    """Everything needed to render a digest's email."""
    digest = conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
    reports = conn.execute(
        "SELECT r.*, f.first_seen, f.total_count, f.reported_at FROM reports r "
        "JOIN fingerprints f ON f.fingerprint = r.fingerprint "
        "WHERE r.digest_id = ? ORDER BY r.id",
        (digest_id,),
    ).fetchall()
    return {
        "digest": digest,
        "dropped": json.loads(digest["dropped"]),
        "notices": json.loads(digest["notices"]),
        "reports": reports,
        "failed": failed_digests(conn),
    }


def failed_digests(conn):
    rows = conn.execute("SELECT * FROM digests WHERE status = 'failed' ORDER BY id").fetchall()
    return [
        {
            "id": row["id"],
            "report_count": row["report_count"],
            "dropped": json.loads(row["dropped"]),
            "last_error": row["last_error"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def mark_sent(conn, digest_id, now, shown_fingerprints, group_count, refused=None):
    """Record a delivered digest: delete its reports and mark its groups reported.

    `refused` describes recipients the server refused while accepting others;
    it's kept as the digest's last_error and queued as a notice.
    """
    with transaction(conn):
        conn.execute(
            "UPDATE digests SET status = 'sent', sent_at = ?, group_count = ?, "
            "next_attempt_at = NULL, last_error = ? WHERE id = ?",
            (now, group_count, refused[:STORED_TEXT_LIMIT] if refused else None, digest_id),
        )
        conn.execute("DELETE FROM reports WHERE digest_id = ?", (digest_id,))
        conn.executemany(
            "UPDATE fingerprints SET reported_at = ? WHERE fingerprint = ? AND reported_at IS NULL",
            [(now, fingerprint) for fingerprint in shown_fingerprints],
        )
        if refused:
            add_notice(conn, now, f"Digest {digest_id} was refused for some recipients: {refused}")


def mark_failed(conn, digest_id, now, error):
    """Record a permanent failure.

    A digest with reports is kept as failed until retry-failed or purge-failed.
    One with only drop counts and notices hands them back to the queue instead,
    so every failed digest holds at least one report.
    """
    with transaction(conn):
        digest = conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
        reports = conn.execute("SELECT COUNT(*) FROM reports WHERE digest_id = ?", (digest_id,)).fetchone()[0]
        if reports == 0:
            _return_counts(conn, digest)
            conn.execute("DELETE FROM digests WHERE id = ?", (digest_id,))
        else:
            conn.execute(
                "UPDATE digests SET status = 'failed', next_attempt_at = NULL, last_error = ? WHERE id = ?",
                (error[:STORED_TEXT_LIMIT], digest_id),
            )


def retry_later(conn, digest_id, now, error):
    """Record a temporary failure and schedule the next attempt with backoff.

    Returns the delay in seconds: 1, 2, 4 ... minutes, capped at 60.
    """
    with transaction(conn):
        attempts = conn.execute(
            "UPDATE digests SET attempts = attempts + 1, last_error = ? WHERE id = ? RETURNING attempts",
            (error[:STORED_TEXT_LIMIT], digest_id),
        ).fetchone()[0]
        delay = 60 * min(MAX_BACKOFF_MINUTES, 2 ** min(attempts - 1, 6))
        conn.execute("UPDATE digests SET next_attempt_at = ? WHERE id = ?", (now + delay, digest_id))
    return delay


def _return_counts(conn, digest):
    # Hand a digest's drop counts and notices back to the queue, keeping
    # their original times so they're due for the next claim straight away.
    for name, item in json.loads(digest["dropped"]).items():
        _add_counter(conn, name, item["count"], item["first_at"])
    for notice in json.loads(digest["notices"]):
        conn.execute(
            "INSERT INTO notices (created_at, text) VALUES (?, ?)", (notice["created_at"], notice["text"])
        )


def retry_failed(conn):
    """Return failed digests' reports, drop counts, and notices to the queue.

    Returns how many digests were dissolved.
    """
    with transaction(conn):
        digests = conn.execute("SELECT * FROM digests WHERE status = 'failed'").fetchall()
        for digest in digests:
            conn.execute("UPDATE reports SET digest_id = NULL WHERE digest_id = ?", (digest["id"],))
            _return_counts(conn, digest)
            conn.execute("DELETE FROM digests WHERE id = ?", (digest["id"],))
    return len(digests)


def purge_failed(conn):
    """Delete failed digests and their reports. Returns (digests, reports) deleted."""
    with transaction(conn):
        reports = conn.execute(
            "DELETE FROM reports WHERE digest_id IN (SELECT id FROM digests WHERE status = 'failed')"
        ).rowcount
        digests = conn.execute("DELETE FROM digests WHERE status = 'failed'").rowcount
    return digests, reports


def reset_in_flight(conn, now):
    """Make an in-flight digest due now, so a restart retries straight away."""
    with transaction(conn):
        conn.execute("UPDATE digests SET next_attempt_at = ? WHERE status = 'pending'", (now,))


def cleanup(conn, now):
    """Delete old sent digests and fingerprints that are old or over the limit."""
    unreferenced = "NOT EXISTS (SELECT 1 FROM reports r WHERE r.fingerprint = fingerprints.fingerprint)"
    with transaction(conn):
        conn.execute(
            "DELETE FROM digests WHERE status = 'sent' AND sent_at < ?",
            (now - SENT_DIGEST_DAYS * DAY,),
        )
        conn.execute(
            f"DELETE FROM fingerprints WHERE last_seen < ? AND {unreferenced}",
            (now - FINGERPRINT_DAYS * DAY,),
        )
        excess = conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0] - MAX_FINGERPRINTS
        if excess > 0:
            conn.execute(
                "DELETE FROM fingerprints WHERE fingerprint IN ("
                f"SELECT fingerprint FROM fingerprints WHERE {unreferenced} "
                "ORDER BY last_seen LIMIT ?)",
                (excess,),
            )


def status(conn):
    """A summary of the queue for `manage.py status`."""
    return {
        "pending_reports": conn.execute(
            "SELECT COUNT(*) FROM reports WHERE digest_id IS NULL"
        ).fetchone()[0],
        "stored_reports": conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0],
        "in_flight": pending_digest(conn),
        "counters": conn.execute("SELECT * FROM counters WHERE value > 0 ORDER BY name").fetchall(),
        "notices": conn.execute("SELECT COUNT(*) FROM notices").fetchone()[0],
        "failed": failed_digests(conn),
        "fingerprints": conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0],
    }
