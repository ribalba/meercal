"""One line per thing that happened, on stdout.

The agent runs as a service more often than it runs in a terminal, and the
first question about it is always "is it still working", so every pass says
what it did even when it did nothing.
"""

from __future__ import annotations

import sys
from datetime import datetime


def log(message: str, *, error: bool = False) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    stream = sys.stderr if error else sys.stdout
    print(f"{stamp} {message}", file=stream, flush=True)
