"""Test-wide setup.

``MEERCAL_CONFIG=""`` before anything imports the config: the loader would
otherwise read the developer's own ``meercal.toml``, and a test that passes
because of what is on one machine is not a test.
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("MEERCAL_CONFIG", "")
os.environ.setdefault("TZ", "Europe/Berlin")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The API tests need a real Postgres — the queries they cover are Postgres'
# (regex operators, JSONB, partial indexes), so SQLite would prove nothing.
# They run against MEERCAL_TEST_DB and skip without it, so that `pytest` on a
# laptop with no database still runs everything else, and so that a test run can
# never be pointed at the database somebody keeps their calendars in.
_test_db = os.environ.get("MEERCAL_TEST_DB")
if _test_db:
    os.environ["DATABASE_URL"] = _test_db
