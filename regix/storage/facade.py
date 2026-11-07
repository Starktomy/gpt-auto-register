"""Aggregate storage API used by services and the legacy ``webui.db`` facade."""

from __future__ import annotations

from .database import (  # noqa: F401
    DB_PATH,
    REMAIL_MAX_ATTEMPTS,
    _conn,
    _lock,
    init_db,
)
from .accounts import *  # noqa: F401,F403
from .registered import *  # noqa: F401,F403
from .runs import *  # noqa: F401,F403
from .settings import *  # noqa: F401,F403
from .proxies import *  # noqa: F401,F403
from .mail_orders import *  # noqa: F401,F403

# Regix has historically initialized SQLite on the first storage import. Keep
# that compatibility behavior here, rather than in every repository module.
init_db()

