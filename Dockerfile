FROM python:3.15.0-slim

WORKDIR /code

# A fixed UID and GID, so a bind-mounted data directory can be given to it
# with chown 10001:10001.
RUN groupadd --system --gid 10001 csp-to-email \
    && useradd --system --uid 10001 --gid csp-to-email --no-create-home csp-to-email \
    && mkdir /data \
    && chown csp-to-email:csp-to-email /data

COPY requirements.txt /code/
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py worker.py manage.py /code/
COPY csp_to_email /code/csp_to_email

USER csp-to-email
ENV CSP2E_DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 5000

# The web service. The worker service runs `python worker.py` instead.
# No control socket: it isn't used, and the default path is in a home
# directory this user doesn't have.
CMD ["gunicorn", "app:app", "-b", "0.0.0.0:5000", "--workers", "1", "--threads", "8", "--no-control-socket"]
