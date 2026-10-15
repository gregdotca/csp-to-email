"""Admin commands: status, healthcheck, retry-failed, and purge-failed."""

import argparse
import sys
from datetime import datetime, timezone

from . import Clock, config as config_module, db, worker


def _time(timestamp):
    if timestamp is None:
        return "-"
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def format_status(info, health):
    lines = [
        f"Worker: {'healthy' if health[0] else 'UNHEALTHY'} ({health[1]})",
        f"Pending reports: {info['pending_reports']} (stored in total: {info['stored_reports']})",
    ]
    digest = info["in_flight"]
    if digest is None:
        lines.append("In-flight digest: none")
    else:
        lines.append(
            f"In-flight digest: #{digest['id']}, {digest['report_count']} reports, "
            f"{digest['attempts']} failed attempts, next attempt {_time(digest['next_attempt_at'])}"
        )
        if digest["last_error"]:
            lines.append(f"  Last error: {digest['last_error']}")
    if info["counters"]:
        lines.append("Dropped reports waiting for the next digest:")
        for row in info["counters"]:
            lines.append(f"  {row['name']}: {row['value']} (since {_time(row['first_at'])})")
    lines.append(f"Notices waiting for the next digest: {info['notices']}")
    if info["failed"]:
        lines.append(f"Failed digests ({len(info['failed'])}); resend with retry-failed or delete with purge-failed:")
        for item in info["failed"]:
            lines.append(f"  #{item['id']}: {item['report_count']} reports, created {_time(item['created_at'])}, error: {item['last_error']}")
    else:
        lines.append("Failed digests: none")
    lines.append(f"Known violations (fingerprints): {info['fingerprints']}")
    return "\n".join(lines)


def main(argv=None, clock=None, env=None, out=sys.stdout):
    parser = argparse.ArgumentParser(prog="manage.py", description="CSP to Email admin commands")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="show the queue, the in-flight digest, and failures")
    commands.add_parser("healthcheck", help="exit non-zero if the worker is stopped or stuck")
    commands.add_parser("retry-failed", help="put failed digests' reports back in the queue")
    commands.add_parser("purge-failed", help="delete failed digests and their reports")
    args = parser.parse_args(argv)
    clock = clock or Clock()

    try:
        cfg = config_module.load(env)
    except config_module.ConfigError as error:
        print(error, file=out)
        return 2

    if args.command == "healthcheck":
        healthy, message = worker.healthcheck(cfg.heartbeat_path, clock)
        print(message, file=out)
        return 0 if healthy else 1

    conn = db.connect(cfg.db_path)
    try:
        db.migrate(conn)
        if args.command == "status":
            print(format_status(db.status(conn), worker.healthcheck(cfg.heartbeat_path, clock)), file=out)
        elif args.command == "retry-failed":
            count = db.retry_failed(conn)
            print(f"Returned {count} failed digest(s) to the queue; they'll go out in the next digest.", file=out)
        elif args.command == "purge-failed":
            digests, reports = db.purge_failed(conn)
            print(f"Deleted {digests} failed digest(s) and {reports} report(s).", file=out)
    finally:
        conn.close()
    return 0
