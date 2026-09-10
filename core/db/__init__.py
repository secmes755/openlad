"""
OpenLAD Database Layer
"""
import sqlite3
from datetime import datetime

# Pin the datetime encoding explicitly.
#
# SQLite has no date type. Every timestamp column in this layer is declared
# TIMESTAMP with `DEFAULT CURRENT_TIMESTAMP`, which stores TEXT
# 'YYYY-MM-DD HH:MM:SS' in UTC. Range comparisons such as
# `updated_at < datetime('now', '-24 hours')` are only meaningful when every
# writer stores that same TEXT shape — and SQLite orders every REAL below every
# TEXT, so even one writer storing a float silently breaks every such comparison.
#
# Python 3.12 deprecated sqlite3's implicit datetime adapter and 3.13 removes
# it, so depending on it means a DeprecationWarning today and a hard failure on
# upgrade. Registering the adapter here keeps the shape pinned once for the whole
# layer, byte-for-byte identical to CURRENT_TIMESTAMP (space separator, second
# precision) so TEXT ordering and `datetime.fromisoformat` reads stay correct.
sqlite3.register_adapter(
    datetime, lambda dt: dt.isoformat(sep=" ", timespec="seconds")
)

from .system_db import SystemDB, get_system_db  # noqa: E402
from .tenant_db import (  # noqa: E402
    TenantDBFactory,
    get_tenant_metadata_db,
    get_tenant_vector_db,
)

__all__ = [
    "SystemDB",
    "get_system_db",
    "TenantDBFactory",
    "get_tenant_metadata_db",
    "get_tenant_vector_db",
]
