"""Build digest emails and send them over SMTP."""

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate

TIMEOUT_SECONDS = 30
SOFTWARE_NAME = "CSP to Email"


def build_message(config, digest_id, created_at, subject, body):
    message = EmailMessage()
    message["From"] = f"{SOFTWARE_NAME} <{config.smtp_sender}>"
    message["To"] = ", ".join(config.smtp_receivers)
    message["Subject"] = subject
    message["Date"] = formatdate(usegmt=True)
    # The same digest always gets the same Message-ID, so a resend after a
    # crash can be spotted as a duplicate.
    domain = config.smtp_sender.rpartition("@")[2] or "localhost"
    message["Message-ID"] = f"<csp-to-email.digest.{digest_id}.{int(created_at)}@{domain}>"
    message.set_content(body)
    return message


def default_smtp_factory(protocol, host, port, timeout):
    if protocol == "ssl":
        return smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
    return smtplib.SMTP(host, port, timeout=timeout)


def send(config, message, smtp_factory=default_smtp_factory):
    """Send a message over one SMTP connection.

    Returns a dict of refused recipients ({address: (code, message)}), empty
    when every recipient accepted it. Raises smtplib or OSError exceptions on
    failure; classify them with is_permanent().
    """
    with smtp_factory(config.smtp_protocol, config.smtp_server, config.smtp_port, TIMEOUT_SECONDS) as smtp:
        if config.smtp_protocol == "starttls":
            smtp.starttls(context=ssl.create_default_context())
        if config.smtp_username:
            smtp.login(config.smtp_username, config.smtp_password)
        return smtp.send_message(message, to_addrs=list(config.smtp_receivers))


def is_permanent(error):
    """True for a 5xx rejection of the message itself, which retrying won't fix.

    Connection problems, timeouts, 4xx replies, and authentication errors are
    temporary: they're retried with backoff until they succeed.
    """
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        codes = [code for code, _ in error.recipients.values()]
        return bool(codes) and all(500 <= code < 600 for code in codes)
    if isinstance(error, (smtplib.SMTPDataError, smtplib.SMTPSenderRefused)):
        return 500 <= error.smtp_code < 600
    return False


def describe(error):
    """A one-line description of an SMTP failure for logs and last_error."""
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return "all recipients refused: " + format_refused(error.recipients)
    if isinstance(error, smtplib.SMTPResponseException):
        detail = error.smtp_error
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", "replace")
        return f"{type(error).__name__} {error.smtp_code}: {detail}"
    return f"{type(error).__name__}: {error}"


def format_refused(refused):
    parts = []
    for address, (code, detail) in sorted(refused.items()):
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", "replace")
        parts.append(f"{address} ({code} {detail})")
    return "; ".join(parts)
