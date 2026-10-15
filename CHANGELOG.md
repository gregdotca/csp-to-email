# Changelog

## 2026-10-14 (0.1.0)

The first release. CSP to Email receives Content Security Policy violation reports, queues them in SQLite, and emails them as digests every few minutes. It's a rebuild of an earlier proof of concept, which has since become csprelay.com.

### Added

- Two services from one image: `web` receives reports and `worker` emails digests. They share a SQLite database on the `csp-to-email-data` volume, so queued reports survive the containers being recreated. `docker-compose.yml` publishes the web service on `127.0.0.1:5000` only, to run behind a reverse proxy, and ships with placeholder hostnames in `CSP2E_ALLOWED_HOSTS` to replace with your own.
- Digests: the worker emails one once the oldest queued report is `CSP2E_DIGEST_INTERVAL` old (5 minutes by default). The digest groups reports by page and violation, puts violations not reported before first, marked `[NEW]` with their all-time counts, lists at most 200 groups, and ends with counts of dropped reports and any notices. Every reported value is escaped so a crafted report can't add lines to the email, and the subject is built only from counts.
- Reliable delivery: one digest is sent at a time, and its reports are deleted only once the mail server accepts it. If it accepts the digest for some recipients and refuses others, the digest counts as sent and the next one lists the refused addresses. If the worker stops after a send but before recording it, the digest is sent again on restart, with the same `Message-ID` so it shows as a duplicate. Temporary failures, including a wrong password, are retried with backoff from 1 to 60 minutes. A digest the server rejects outright (a 5xx error) is marked failed and keeps its reports, without holding up later digests. A second worker waits on standby instead of sending twice, and stopping the worker finishes the current send first.
- Reporting API support: `POST /` accepts `application/reports+json` batches as well as `report-uri` reports (`application/csp-report` or `application/json`), `OPTIONS /` answers the browser's CORS preflight, and responses carry `Access-Control-Allow-Origin: *`.
- Limits against abuse: 256 KB per request, the first 100 reports of a batch, an allowlist of reporting hosts (`CSP2E_ALLOWED_HOSTS`), a per-IP rate limit (`CSP2E_RATE_LIMIT`, 120 a minute), and a cap on stored reports (`CSP2E_MAX_QUEUED`). Dropped reports are counted and listed in the next digest. `CSP2E_PROXY_HOPS` sets how many reverse proxies to trust for the client IP.
- Privacy: stored URLs lose their query string, fragment, and any `user:password@`, script samples are kept, and shown in the digest (one per violation), only when `CSP2E_INCLUDE_SAMPLES` is on, and client IPs are never written to disk.
- SMTP options: `CSP2E_PROTOCOL` (`starttls`, `ssl`, or `none`, with the default port following it), logging in only when `CSP2E_USERNAME` is set, and several addresses in `CSP2E_RECEIVER`.
- `GET /healthz`, and healthchecks for both services in `docker-compose.yml`.
- Admin commands (`python manage.py`): `status`, `healthcheck`, `retry-failed`, and `purge-failed`.
- README sections on installing step by step, running behind a reverse proxy (with a Caddy example, notes for nginx and Traefik, and how to reach the web service from a proxy that runs in Docker), sending reports with both `report-uri` and `report-to`, checking it works with a test report, keeping out junk reports, what's stored and for how long, every setting, and the admin commands.
- `ACR.md`, rating the project under [AI Code Rating](https://aicoderating.com/spec/0.1/), and an ACR badge in the README.
- Docker images for `linux/amd64`, `linux/arm64`, and `linux/arm/v7`, based on `python:3.15.0-slim` and running as a non-root user (UID and GID 10001), with Flask 3.1.3, gunicorn 26.2.0, and python-dotenv 1.2.4.
