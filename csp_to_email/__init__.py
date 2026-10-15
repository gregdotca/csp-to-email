"""CSP to Email: receive CSP violation reports and email them as digests."""

import time


class Clock:
    """Wall-clock time as Unix seconds. Tests pass a fake with the same method."""

    def now(self):
        return time.time()
