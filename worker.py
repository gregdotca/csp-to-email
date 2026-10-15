"""Entry point for the worker service: `python worker.py`."""

import sys

from dotenv import load_dotenv

load_dotenv()

from csp_to_email.worker import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
