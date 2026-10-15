"""Gunicorn entry point for the web service: `gunicorn app:app`."""

import logging

from dotenv import load_dotenv

load_dotenv()

from csp_to_email.web import create_app  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app()

if __name__ == "__main__":
    app.run(use_reloader=False)
