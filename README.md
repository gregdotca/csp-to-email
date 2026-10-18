CSP to Email
------------

[![ACR B4c](https://img.shields.io/badge/ACR-B4c-2140B5)](ACR.md)

## What is CSP to Email?

A small web service that receives your site's Content Security Policy (CSP) violation reports and emails you a digest of them. There's no UI or dashboard: point your CSP's reporting at it, and every few minutes it emails you what was reported, grouped by page and violation.

It accepts reports straight away and stores them in a queue, so a slow or unreachable mail server never delays the browser sending them. A separate worker emails the queued reports as one digest every 5 minutes (configurable), marks violations it hasn't reported before as `[NEW]`, and retries when the mail server is unavailable.

It runs as two services from one image, sharing a data volume:

- `web` accepts reports on `POST /`, in both formats browsers send: `report-uri` (`application/csp-report`) and the Reporting API (`report-to`, `application/reports+json`).
- `worker` emails the digests. Run exactly one.

It started as a proof of concept, which has since become csprelay.com. Version 0.1.0 is a rebuild that queues reports and emails them as digests.

## Installation (Docker Compose)

1. Copy the contents below into a `docker-compose.yml` file (or copy this repository's `docker-compose.yml`).
2. Fill in the `CSP2E_*` settings: your SMTP server and login, the sender and receiver addresses, and `CSP2E_ALLOWED_HOSTS`, the hostnames of the sites that will send reports (see [Keeping out junk reports](#keeping-out-junk-reports)). Every setting is listed under [Settings](#settings).
3. Run `docker compose up -d`.
4. Put a reverse proxy in front of it (see [Reverse proxy](#reverse-proxy)). The web service listens on `127.0.0.1:5000`, so it's only reachable from the same machine.
5. Add the reporting headers to your site (see [Sending your reports to it](#sending-your-reports-to-it)).
6. Check that it works (see [Checking it works](#checking-it-works)).

````
x-csp2e-env: &csp2e-env
  CSP2E_SERVER: mail.example.com
  CSP2E_PROTOCOL: starttls
  CSP2E_PORT: "587"
  CSP2E_USERNAME: user
  CSP2E_PASSWORD: pass
  CSP2E_SENDER: sender@example.com
  CSP2E_RECEIVER: receiver@example.com
  CSP2E_ALLOWED_HOSTS: "example.com,www.example.com"
  CSP2E_PROXY_HOPS: "1"

services:
  web:
    image: gregdotca/csp-to-email:latest
    ports:
      # Only reachable from this machine: put a reverse proxy in front.
      - "127.0.0.1:5000:5000"
    environment: *csp2e-env
    volumes:
      - csp-to-email-data:/data
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=5)"]
      interval: 30s
      timeout: 10s
      retries: 3

  worker:
    image: gregdotca/csp-to-email:latest
    command: ["python", "worker.py"]
    environment: *csp2e-env
    volumes:
      - csp-to-email-data:/data
    restart: unless-stopped
    stop_grace_period: 120s
    healthcheck:
      test: ["CMD", "python", "manage.py", "healthcheck"]
      interval: 30s
      timeout: 10s
      retries: 3

volumes:
  csp-to-email-data:
````

`docker compose ps` shows each service as healthy or unhealthy. The worker is unhealthy when it has stopped or a send is stuck. Compose doesn't restart unhealthy containers by itself, so check it if digests stop arriving.

## Installation (Docker)

If you don't want to run Docker Compose, run the two containers directly. Put the settings in a file, such as `csp-to-email.env`:

````
CSP2E_SERVER=mail.example.com
CSP2E_PROTOCOL=starttls
CSP2E_PORT=587
CSP2E_USERNAME=user
CSP2E_PASSWORD=pass
CSP2E_SENDER=sender@example.com
CSP2E_RECEIVER=receiver@example.com
CSP2E_ALLOWED_HOSTS=example.com,www.example.com
CSP2E_PROXY_HOPS=1
````

Then:

````
docker volume create csp-to-email-data

docker run -d \
  --name csp-to-email-web \
  -p 127.0.0.1:5000:5000 \
  -v csp-to-email-data:/data \
  --env-file csp-to-email.env \
  --restart unless-stopped \
  gregdotca/csp-to-email:latest

docker run -d \
  --name csp-to-email-worker \
  -v csp-to-email-data:/data \
  --env-file csp-to-email.env \
  --restart unless-stopped \
  --stop-timeout 120 \
  gregdotca/csp-to-email:latest \
  python worker.py
````

The containers run as UID and GID 10001. A named volume like the one above gets the right ownership automatically. If you use a bind mount instead, give the directory to that user first, such as `sudo chown 10001:10001 /srv/csp-to-email`; otherwise both services exit with a message saying the data directory isn't writable.

Then carry on from step 4 of the Docker Compose steps above: the reverse proxy, your site's headers, and the check.

## Reverse proxy

Run the web service behind a reverse proxy such as Caddy, nginx, or Traefik, which also gives it HTTPS. Don't publish port 5000 straight to the internet. The web service has only 8 threads, and a few clients sending their requests slowly could tie them all up, so the proxy must read each request in full before passing it on. nginx does this by default. Caddy and Traefik don't: Caddy needs `request_buffers`, as below, and Traefik needs its Buffering middleware. The proxy should also cap the body at the web service's limit (256 KB) and give up on clients that take too long to send it, so slow clients can't tie up the proxy instead. For example, with Caddy on the same machine:

````
csp.example.com {
    request_body {
        max_size 256KiB
        read_timeout 10s
    }
    reverse_proxy 127.0.0.1:5000 {
        request_buffers 256KiB
    }
}
````

If your reverse proxy runs in a Docker container itself, it can't reach `127.0.0.1:5000`: inside the proxy's container, that address is the container itself, not the host. Put the proxy on the same Docker network as the web service instead, and point it at the web service by name:

- **Docker Compose:** use `web:5000` (`reverse_proxy web:5000` in the example above). Compose creates a network for the project, which `docker network ls` lists, usually as `<folder>_default`. Connect the proxy's container to it with `docker network connect <network> <proxy container>`, or add it as an external network in the proxy's own compose file.
- **Docker:** create a network with `docker network create csp-to-email`, add `--network csp-to-email` to both `docker run` commands and to the proxy's, and use `csp-to-email-web:5000`.

The `127.0.0.1:5000` port mapping isn't needed then, and it's still one proxy, so `CSP2E_PROXY_HOPS` stays `1`.

`CSP2E_PROXY_HOPS` must be the number of proxies in front of the web service: `1` for a single proxy like the one above, `0` if there's none. The rate limit identifies clients by IP, and with too high a number a client can choose its own IP by sending a forged `X-Forwarded-For` header.

## Sending your reports to it

Reports go to the root of the address your reverse proxy serves, such as `https://csp.example.com/`. Add both reporting directives to your site's CSP: browsers that support `report-to` use it and ignore `report-uri`, and the rest fall back to `report-uri`.

````
Reporting-Endpoints: csp-endpoint="https://csp.example.com/"
Report-To: {"group":"csp-endpoint","max_age":10886400,"endpoints":[{"url":"https://csp.example.com/"}]}
Content-Security-Policy: default-src 'self'; report-uri https://csp.example.com/; report-to csp-endpoint
````

`Reporting-Endpoints` names the endpoint that `report-to` refers to; the name (`csp-endpoint` here) is up to you. `Report-To` is the older way of naming it, for Chromium-based browsers released before `Reporting-Endpoints` was supported. They understand `report-to` and so ignore `report-uri`, but can't use `Reporting-Endpoints`, so without `Report-To` they'd send nothing. Use the same name and URL in both. To try a policy without blocking anything, send it as `Content-Security-Policy-Report-Only` instead: browsers report the violations but don't enforce them. When the browser says which it was, the digest shows `report` or `enforce` next to each violation.

Browsers send reports on their own schedule, and delivery isn't guaranteed: `report-to` reports are usually batched and sent about a minute later, and a browser that can't reach the endpoint retries only for a while before giving up. Reports that arrive go out in the next digest.

## Checking it works

Send a test report through your reverse proxy, with one of the hostnames in `CSP2E_ALLOWED_HOSTS` as the page:

````
curl -i -X POST https://csp.example.com/ \
  -H 'Content-Type: application/csp-report' \
  -d '{"csp-report":{"document-uri":"https://example.com/","blocked-uri":"https://test.example/x.js","effective-directive":"script-src"}}'
````

It should answer with status 204. Then check the worker:

````
docker compose exec worker python manage.py status
````

(Without Compose, `docker exec csp-to-email-worker python manage.py status`.) `Worker: healthy` means the worker is running. The test report is counted under `Pending reports` until the worker starts its digest, then under `In-flight digest` until the email is sent (or under `Failed digests` if the mail server rejects it). If it's listed under dropped reports as `host_not_allowed` instead, the page's hostname isn't in `CSP2E_ALLOWED_HOSTS`.

The digest should arrive within `CSP2E_DIGEST_INTERVAL` (5 minutes by default). If it doesn't, run `status` again: the in-flight digest's `Last error` shows what went wrong, such as the mail server refusing the login.

## Keeping out junk reports

The endpoint is public, so anyone can send it reports, including for pages that aren't yours. Set `CSP2E_ALLOWED_HOSTS` to the hostnames of the sites whose CSP points at it, such as `example.com,www.example.com`. Hostnames must match exactly, so list every variant your sites use. Reports for any other page are dropped before they're stored, and the digest says how many.

The rate limit (`CSP2E_RATE_LIMIT`, per client IP) and the queue limit (`CSP2E_MAX_QUEUED`) also cap how much any one sender, or a flood of them, can store.

## What's stored

CSP to Email keeps as little as it needs to build the digests:

- **For each report, until its digest is emailed:** the page URL, the blocked URL, and the source file, each with its query string, fragment, and any `user:password@` removed, since those can hold session tokens or personal details; the violated directive; whether the policy was enforced or report-only, when the browser says; the line and column; and the browser's user agent, which helps spot violations caused by browser extensions. Reports are deleted as soon as their digest is sent. A digest the mail server rejected keeps its reports until you run `retry-failed` or `purge-failed`.
- **Script samples** (the first characters of a blocked inline script, which can include page content) only if you set `CSP2E_INCLUDE_SAMPLES=on`.
- **For each violation it has seen:** a hash of the page, directive, and blocked URL, with when it was first and last seen and how many times. That's what makes `[NEW]` and the all-time counts work. Violations not seen for 90 days are forgotten, and it keeps about 50,000 at most; a violation whose reports are still waiting to be emailed is kept until they've gone.
- **For each digest sent:** its counts and any error, for 30 days. Not the reports themselves.

CSP to Email itself never writes client IP addresses to disk: the rate limit keeps them in memory for a minute. Your reverse proxy may log them, as nginx and others do by default.

## Settings

| Setting | Default | Description |
| --- | --- | --- |
| `CSP2E_SERVER` | (required) | SMTP server hostname. |
| `CSP2E_PROTOCOL` | `starttls` | `starttls` (`tls` also works), `ssl` for implicit TLS, or `none` for a local relay. |
| `CSP2E_PORT` | 587, 465, or 25 | SMTP port. The default follows `CSP2E_PROTOCOL`. |
| `CSP2E_USERNAME`, `CSP2E_PASSWORD` | (none) | SMTP login. Leave the username empty to skip logging in. |
| `CSP2E_SENDER` | (required) | The digest's From address. |
| `CSP2E_RECEIVER` | (required) | Where digests go. Separate several addresses with commas. |
| `CSP2E_DIGEST_INTERVAL` | `300` | Seconds to collect reports before sending a digest. |
| `CSP2E_ALLOWED_HOSTS` | (accept all) | Comma-separated hostnames whose reports are accepted, such as `example.com,www.example.com`. Reports for other pages are dropped. |
| `CSP2E_RATE_LIMIT` | `120` | Reports accepted per client IP per minute. |
| `CSP2E_MAX_QUEUED` | `10000` | Most reports stored at once. Reports over the limit are dropped. |
| `CSP2E_PROXY_HOPS` | `0` | Number of reverse proxies in front of the web service (see [Reverse proxy](#reverse-proxy)). |
| `CSP2E_INCLUDE_SAMPLES` | `off` | Set to `on` to store and email the script samples browsers include, which can contain page content. |
| `CSP2E_DATA_DIR` | `/data` | Where the database and the worker's lock and heartbeat files live. |

Dropped reports aren't stored, but the next digest says how many were dropped and why.

## Admin commands

Run these in the worker container, such as `docker compose exec worker python manage.py status`:

- `status`: the worker's health, the queue, the digest being sent (with its last error, if sending is failing), and failed digests.
- `retry-failed`: put the reports from digests the mail server rejected back in the queue, to go out in the next digest.
- `purge-failed`: delete digests the mail server rejected, and their reports.
- `healthcheck`: exit non-zero if the worker has stopped or is stuck (used by the compose healthcheck).

If digests stop arriving, `status` shows why. A wrong password or an unreachable server is retried with backoff (up to once an hour) rather than given up on, so after fixing the settings, restart the worker to retry straight away.

## License

MIT License

Copyright (c) 2025-2026 Greg Chetcuti <greg@chetcuti.com>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
