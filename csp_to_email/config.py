"""Read and validate the CSP2E_* settings."""

import os
import uuid
from dataclasses import dataclass

PROTOCOLS = ("starttls", "ssl", "none")
PROTOCOL_ALIASES = {"tls": "starttls"}
DEFAULT_PORTS = {"starttls": 587, "ssl": 465, "none": 25}
TRUE_VALUES = ("1", "true", "yes", "on")
FALSE_VALUES = ("", "0", "false", "no", "off")


class ConfigError(Exception):
    """A setting is missing or invalid. The message is meant for the operator."""


@dataclass(frozen=True)
class Config:
    smtp_server: str | None
    smtp_port: int
    smtp_protocol: str
    smtp_username: str | None
    smtp_password: str | None
    smtp_sender: str | None
    smtp_receivers: tuple[str, ...]
    digest_interval: int
    data_dir: str
    max_queued: int
    rate_limit: int
    allowed_hosts: frozenset[str]
    proxy_hops: int
    include_samples: bool

    @property
    def db_path(self):
        return os.path.join(self.data_dir, "csp-to-email.db")

    @property
    def lock_path(self):
        return os.path.join(self.data_dir, "worker.lock")

    @property
    def heartbeat_path(self):
        return os.path.join(self.data_dir, "worker.heartbeat")


def _text(env, name):
    value = env.get(name, "").strip()
    return value or None


def _int(env, name, default, minimum, maximum=None):
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from None
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be at most {maximum}, got {value}")
    return value


def _bool(env, name):
    raw = env.get(name, "").strip().lower()
    if raw in TRUE_VALUES:
        return True
    if raw in FALSE_VALUES:
        return False
    raise ConfigError(f"{name} must be on or off, got {raw!r}")


def _list(env, name):
    return [item.strip() for item in env.get(name, "").split(",") if item.strip()]


def load(env=None):
    """Build a Config from environment variables. Raises ConfigError."""
    env = os.environ if env is None else env

    protocol = env.get("CSP2E_PROTOCOL", "").strip().lower() or "starttls"
    protocol = PROTOCOL_ALIASES.get(protocol, protocol)
    if protocol not in PROTOCOLS:
        raise ConfigError(
            f"CSP2E_PROTOCOL must be one of {', '.join(PROTOCOLS)}, got {protocol!r}"
        )

    return Config(
        smtp_server=_text(env, "CSP2E_SERVER"),
        smtp_port=_int(env, "CSP2E_PORT", DEFAULT_PORTS[protocol], 1, 65535),
        smtp_protocol=protocol,
        smtp_username=_text(env, "CSP2E_USERNAME"),
        # Not stripped: spaces at either end may be part of the password.
        smtp_password=env.get("CSP2E_PASSWORD") or None,
        smtp_sender=_text(env, "CSP2E_SENDER"),
        smtp_receivers=tuple(_list(env, "CSP2E_RECEIVER")),
        digest_interval=_int(env, "CSP2E_DIGEST_INTERVAL", 300, 1),
        data_dir=env.get("CSP2E_DATA_DIR", "").strip() or "/data",
        max_queued=_int(env, "CSP2E_MAX_QUEUED", 10000, 1),
        rate_limit=_int(env, "CSP2E_RATE_LIMIT", 120, 1),
        allowed_hosts=frozenset(h.lower() for h in _list(env, "CSP2E_ALLOWED_HOSTS")),
        proxy_hops=_int(env, "CSP2E_PROXY_HOPS", 0, 0),
        include_samples=_bool(env, "CSP2E_INCLUDE_SAMPLES"),
    )


def check_smtp(config):
    """Raise ConfigError if the worker can't send mail with these settings."""
    missing = []
    if not config.smtp_server:
        missing.append("CSP2E_SERVER")
    if not config.smtp_sender:
        missing.append("CSP2E_SENDER")
    if not config.smtp_receivers:
        missing.append("CSP2E_RECEIVER")
    if config.smtp_username and not config.smtp_password:
        missing.append("CSP2E_PASSWORD (CSP2E_USERNAME is set)")
    if missing:
        raise ConfigError("Missing SMTP settings: " + ", ".join(missing))


def check_data_dir(config):
    """Raise ConfigError unless the data directory exists and is writable."""
    path = config.data_dir
    probe = os.path.join(path, f".write-test-{uuid.uuid4().hex}")
    try:
        with open(probe, "w"):
            pass
        os.remove(probe)
    except OSError as error:
        raise ConfigError(
            f"Data directory {path} isn't writable by UID {os.getuid()}: {error.strerror}. "
            f"Create it and give that user ownership (for example, chown {os.getuid()} {path})."
        ) from None
