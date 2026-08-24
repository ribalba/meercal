"""The single source of the version number.

Read from the VERSION file at the repository root so that the file the release
tooling edits is the file the running process reports — nothing here is
duplicated in code, and a container built from a dirty tree cannot claim a
number the tree does not have.
"""

from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"

try:
    VERSION = VERSION_FILE.read_text(encoding="utf-8").strip()
except OSError:  # running from a wheel or an image that dropped the file
    VERSION = "0.0.0"
