"""Group a digest's reports and render the email."""

import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone

MAX_GROUPS = 200
MAX_NOTICES = 20
MAX_FAILED = 10
FOOTER_TEXT_LIMIT = 200

DROP_LABELS = {
    "batch_too_large": "over 100 reports in one request",
    "host_not_allowed": "page host not in CSP2E_ALLOWED_HOSTS",
    "queue_full": "queue full (CSP2E_MAX_QUEUED)",
    "rate_limited": "rate limited (CSP2E_RATE_LIMIT)",
}

# Characters that would let a value break onto a new line or reorder the
# text around it: line and paragraph separators and bidi controls.
UNSAFE_FORMAT_CHARACTERS = {
    " ", " ",
    "؜", "‎", "‏",
    "‪", "‫", "‬", "‭", "‮",
    "⁦", "⁧", "⁨", "⁩",
}


def clean(value):
    """Make an untrusted value safe to put on one line of a plain-text email.

    Line breaks, tabs, other control characters, and Unicode line and bidi
    characters become visible escapes, so a crafted value can't start a new
    line or pass itself off as another part of the email.
    """
    if value is None:
        return ""
    out = []
    for char in str(value):
        if char == "\n":
            out.append("\\n")
        elif char == "\r":
            out.append("\\r")
        elif char == "\t":
            out.append("\\t")
        elif char == "\\":
            out.append("\\\\")
        elif char in UNSAFE_FORMAT_CHARACTERS or unicodedata.category(char) in ("Cc", "Zl", "Zp"):
            code = ord(char)
            out.append(f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}")
        else:
            out.append(char)
    return "".join(out)


@dataclass
class Group:
    fingerprint: str
    document_uri: str
    directive: str
    blocked_uri: str
    first_seen: float
    total_count: int
    is_new: bool
    count: int = 0
    dispositions: set = field(default_factory=set)
    source: str | None = None
    sample: str | None = None


@dataclass
class Rendered:
    subject: str
    body: str
    shown_fingerprints: list
    group_count: int


def render(data, now):
    """Build the subject and body for a digest loaded by db.load_digest()."""
    groups = _group(data["reports"])
    ordered = sorted(groups.values(), key=lambda g: (not g.is_new, -g.count, g.document_uri, g.directive, g.blocked_uri))
    shown, cut = ordered[:MAX_GROUPS], ordered[MAX_GROUPS:]

    report_count = len(data["reports"])
    new_count = sum(1 for g in ordered if g.is_new)
    dropped_total = sum(item["count"] for item in data["dropped"].values())

    lines = [f"CSP to Email digest, {_time(now)}", ""]
    if ordered:
        lines += _body(shown)
        if cut:
            pages = len({g.document_uri for g in cut})
            lines += [f"... and {len(cut)} more {_plural(len(cut), 'violation')} on {pages} {_plural(pages, 'page')}.", ""]
    else:
        lines += ["No reports were stored in this digest.", ""]
    lines += _footer(data, data["digest"]["id"])

    subject = f"CSP digest: {report_count} {_plural(report_count, 'report')}, {len(ordered)} {_plural(len(ordered), 'violation')} ({new_count} new)"
    if dropped_total:
        subject += f", {dropped_total} dropped"
    return Rendered(
        subject=subject,
        body="\n".join(lines).rstrip() + "\n",
        shown_fingerprints=[g.fingerprint for g in shown],
        group_count=len(ordered),
    )


def _group(reports):
    groups = {}
    for row in reports:
        group = groups.get(row["fingerprint"])
        if group is None:
            group = groups[row["fingerprint"]] = Group(
                fingerprint=row["fingerprint"],
                document_uri=row["document_uri"],
                directive=row["directive"],
                blocked_uri=row["blocked_uri"],
                first_seen=row["first_seen"],
                total_count=row["total_count"],
                is_new=row["reported_at"] is None,
            )
        group.count += 1
        if row["disposition"]:
            group.dispositions.add(row["disposition"])
        if group.source is None and row["source_file"]:
            group.source = row["source_file"] + (f":{row['line_number']}" if row["line_number"] is not None else "")
        # Only stored when CSP2E_INCLUDE_SAMPLES is on.
        if group.sample is None and row["sample"]:
            group.sample = row["sample"]
    return groups


def _body(groups):
    # Pages appear in the order of their highest-ranked group.
    pages = {}
    for group in groups:
        pages.setdefault(group.document_uri, []).append(group)
    lines = []
    for page, page_groups in pages.items():
        lines.append(clean(page))
        for g in page_groups:
            marker = "[NEW] " if g.is_new else ""
            blocked = clean(g.blocked_uri) or "(none)"
            lines.append(f"  {marker}{g.count} x {clean(g.directive)}  {blocked}")
            details = [f"first seen {_time(g.first_seen)}", f"{g.total_count} all time"]
            if g.source:
                details.append(f"source {clean(g.source)}")
            if g.dispositions:
                details.append(", ".join(clean(d) for d in sorted(g.dispositions)))
            lines.append("      " + ", ".join(details))
            if g.sample:
                lines.append(f"      sample: {clean(g.sample)}")
        lines.append("")
    return lines


def _footer(data, digest_id):
    lines = []
    dropped = data["dropped"]
    if dropped:
        lines += ["Dropped reports (not stored):"]
        for name in sorted(dropped):
            lines.append(f"  {DROP_LABELS.get(name, clean(name))}: {dropped[name]['count']}")
        lines.append("")

    notices = data["notices"]
    if notices:
        lines += ["Notices:"]
        for notice in notices[:MAX_NOTICES]:
            lines.append(f"  - {_cut(notice['text'])}")
        if len(notices) > MAX_NOTICES:
            more = len(notices) - MAX_NOTICES
            lines.append(f"  ... and {more} more {_plural(more, 'notice')}.")
        lines.append("")

    failed = [item for item in data["failed"] if item["id"] != digest_id]
    if failed:
        lines += ["Failed digests (not delivered; resend with manage.py retry-failed):"]
        for item in failed[:MAX_FAILED]:
            parts = [f"#{item['id']}: {item['report_count']} {_plural(item['report_count'], 'report')}"]
            drops = sum(entry["count"] for entry in item["dropped"].values())
            if drops:
                parts.append(f"{drops} dropped")
            parts.append(f"error: {_cut(item['last_error'] or 'unknown')}")
            lines.append("  " + ", ".join(parts))
        if len(failed) > MAX_FAILED:
            more = len(failed) - MAX_FAILED
            lines.append(f"  ... and {more} more failed {_plural(more, 'digest')} (see manage.py status).")
        lines.append("")
    return lines


def _cut(text):
    text = clean(text)
    return text if len(text) <= FOOTER_TEXT_LIMIT else text[: FOOTER_TEXT_LIMIT - 3] + "..."


def _plural(count, word):
    return word if count == 1 else word + "s"


def _time(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
