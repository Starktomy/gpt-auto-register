from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


_TEMP_DIR = tempfile.mkdtemp(prefix="regix-tests-")

# regix.storage reads this at import time. Keep it ahead of any application import
# so collection can never initialize the developer's production database.
os.environ["WEBUI_DB_PATH"] = str(Path(_TEMP_DIR) / "test.db")
os.environ["WEBUI_LOG_DIR"] = str(Path(_TEMP_DIR) / "logs")
os.environ.pop("WEBUI_ADMIN_PASSWORD", None)


@pytest.fixture
def clean_db():
    from regix.storage import facade as db

    db.init_db()
    con = db._conn()
    for table in (
        "remail_orders",
        "proxy_pool",
        "runs",
        "registered",
        "outlook_accounts",
        "settings",
    ):
        con.execute(f"DELETE FROM {table}")
    con.commit()
    con.close()

    yield db
