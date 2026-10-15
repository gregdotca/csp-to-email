"""Admin commands: `python manage.py status|healthcheck|retry-failed|purge-failed`."""

import sys

from dotenv import load_dotenv

load_dotenv()

from csp_to_email.admin import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
