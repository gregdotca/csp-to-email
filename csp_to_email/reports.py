"""Parse CSP reports in both browser formats and normalise them to one shape."""

import hashlib
import json

URL_LIMIT = 512
SAMPLE_LIMIT = 512
TEXT_LIMIT = 256
# Line and column numbers above this are junk, and anything past SQLite's
# 64-bit integers would fail to store.
NUMBER_LIMIT = 2**31 - 1

LEGACY_TYPES = ("application/csp-report", "application/json")
REPORTING_API_TYPE = "application/reports+json"

# Field names for the report-uri format (kebab-case) and the Reporting API
# body (camelCase), mapped to the stored names.
LEGACY_FIELDS = {
    "document_uri": "document-uri",
    "blocked_uri": "blocked-uri",
    "effective_directive": "effective-directive",
    "violated_directive": "violated-directive",
    "disposition": "disposition",
    "source_file": "source-file",
    "line_number": "line-number",
    "column_number": "column-number",
    "sample": "script-sample",
}
REPORTING_API_FIELDS = {
    "document_uri": "documentURL",
    "blocked_uri": "blockedURL",
    "effective_directive": "effectiveDirective",
    "violated_directive": "violatedDirective",
    "disposition": "disposition",
    "source_file": "sourceFile",
    "line_number": "lineNumber",
    "column_number": "columnNumber",
    "sample": "sample",
}


class UnsupportedType(Exception):
    """The request's content type isn't a CSP report type (415)."""


class InvalidReport(Exception):
    """The body isn't valid JSON or isn't shaped like a CSP report (400)."""


def parse_body(content_type, body, user_agent=None, include_samples=False):
    """Return the normalised CSP reports in a request body.

    `content_type` is the bare media type (no parameters). Raises
    UnsupportedType or InvalidReport. A Reporting API batch with no CSP
    entries is valid and returns an empty list.
    """
    content_type = (content_type or "").lower()
    if content_type not in LEGACY_TYPES and content_type != REPORTING_API_TYPE:
        raise UnsupportedType(content_type)

    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        raise InvalidReport("body isn't valid JSON") from None

    if content_type == REPORTING_API_TYPE:
        return _parse_reporting_api(data, user_agent, include_samples)
    return [_parse_legacy(data, user_agent, include_samples)]


def _parse_legacy(data, user_agent, include_samples):
    if not isinstance(data, dict) or not isinstance(data.get("csp-report"), dict):
        raise InvalidReport('expected an object with a "csp-report" object')
    report = normalise(data["csp-report"], LEGACY_FIELDS, user_agent, include_samples)
    if report is None:
        raise InvalidReport("report has no document-uri")
    return report


def _parse_reporting_api(data, request_user_agent, include_samples):
    if not isinstance(data, list):
        raise InvalidReport("expected a JSON array of reports")
    reports = []
    for entry in data:
        if not isinstance(entry, dict) or entry.get("type") != "csp-violation":
            continue
        body = entry.get("body")
        if not isinstance(body, dict):
            continue
        if not isinstance(body.get("documentURL"), str) and isinstance(entry.get("url"), str):
            body = {**body, "documentURL": entry["url"]}
        # Each entry carries the user agent of the page that generated it;
        # fall back to the request's header when it's missing.
        user_agent = entry.get("user_agent")
        if not isinstance(user_agent, str) or not user_agent.strip():
            user_agent = request_user_agent
        report = normalise(body, REPORTING_API_FIELDS, user_agent, include_samples)
        if report is not None:
            reports.append(report)
    return reports


def normalise(raw, fields, user_agent=None, include_samples=False):
    """Map one raw report to the stored fields, or None if it has no document URI."""

    def text(name, limit=TEXT_LIMIT):
        value = _string(raw.get(fields[name]))
        return value[:limit] if value else None

    def url(name):
        value = _string(raw.get(fields[name]))
        return strip_url(value)[:URL_LIMIT] if value else None

    def number(name):
        value = raw.get(fields[name])
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= NUMBER_LIMIT:
            return None
        return value

    document_uri = url("document_uri")
    if not document_uri:
        return None

    directive = text("effective_directive")
    if not directive:
        violated = text("violated_directive")
        directive = violated.split()[0] if violated else None
    directive = (directive or "unknown").lower()

    blocked_uri = url("blocked_uri") or ""
    agent = _string(user_agent)

    return {
        "document_uri": document_uri,
        "blocked_uri": blocked_uri,
        "directive": directive,
        "disposition": text("disposition"),
        "source_file": url("source_file"),
        "line_number": number("line_number"),
        "column_number": number("column_number"),
        "sample": text("sample", SAMPLE_LIMIT) if include_samples else None,
        "user_agent": agent[:TEXT_LIMIT] if agent else None,
        "fingerprint": fingerprint(document_uri, directive, blocked_uri),
    }


def _string(value):
    """A stripped string with lone surrogates replaced by U+FFFD, or None for anything else.

    json.loads accepts escaped lone surrogates ("\\ud800"), which can't be
    encoded as UTF-8, so they'd break hashing and storage. U+FFFD rather than
    "?", since a "?" would then be cut off as a query string.
    """
    if not isinstance(value, str):
        return None
    return value.encode("utf-8", "surrogatepass").decode("utf-8", "replace").strip()


def strip_url(value):
    """Remove the query string, fragment, and any user:password@ from a URL.

    Values that aren't URLs (CSP keywords like "inline" or "eval") come back
    unchanged, apart from anything after a "?" or "#".
    """
    cut = min((i for i in (value.find("?"), value.find("#")) if i >= 0), default=len(value))
    value = value[:cut]
    # Split by hand rather than with urlsplit, which raises on malformed
    # ports and would leave the credentials in place.
    if value.startswith("//"):
        prefix, rest = "//", value[2:]
    else:
        scheme, separator, rest = value.partition("://")
        if not separator:
            return value
        prefix = scheme + separator
    netloc, slash, path = rest.partition("/")
    if "@" in netloc:
        netloc = netloc.rpartition("@")[2]
    return f"{prefix}{netloc}{slash}{path}"


def fingerprint(document_uri, directive, blocked_uri):
    """Identify a violation: the same directive and blocked URI on the same page."""
    # A JSON array keeps the fields apart whatever characters they contain.
    key = json.dumps([document_uri, directive, blocked_uri])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
