"""Database access for the API (read-only usage)."""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Connection, Engine

DEFAULT_URL = "mysql+pymysql://root:root@127.0.0.1:3306/marketplace"


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return create_engine(os.getenv("DATABASE_URL", DEFAULT_URL), pool_pre_ping=True, pool_recycle=1800)


def get_conn() -> Iterator[Connection]:
    """FastAPI dependency: one connection per request, always released."""
    with get_engine().connect() as conn:
        yield conn
