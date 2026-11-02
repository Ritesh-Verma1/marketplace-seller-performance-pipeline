"""Runtime configuration. Everything is overridable via env vars or CLI flags;
nothing is hard-coded to an absolute path."""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    input_dir: Path = Path("data/kaggle")        # Olist CSVs
    internal_dir: Path = Path("data/internal")   # human-maintained files
    raw_dir: Path = Path("data/raw")             # raw layer (as received)
    database_url: str = "mysql+pymysql://root:root@127.0.0.1:3306/marketplace"

    # Public APIs (base URLs are configurable because versions/hosts change)
    fx_base_url: str = "https://api.frankfurter.app"
    holidays_base_url: str = "https://date.nager.at/api/v3"
    states_url: str = "https://brasilapi.com.br/api/ibge/uf/v1"

    http_timeout: float = 20.0
    http_attempts: int = 4          # >= 3 required by the assignment
    backoff_base: float = 1.0       # sleeps 1s, 2s, 4s ...
    refresh_cache: bool = False     # True => ignore cached API responses

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        env = os.environ
        base = cls(
            input_dir=Path(env.get("PIPELINE_INPUT_DIR", cls.input_dir)),
            internal_dir=Path(env.get("PIPELINE_INTERNAL_DIR", cls.internal_dir)),
            raw_dir=Path(env.get("PIPELINE_RAW_DIR", cls.raw_dir)),
            database_url=env.get("DATABASE_URL", cls.database_url),
            fx_base_url=env.get("FX_BASE_URL", cls.fx_base_url),
            holidays_base_url=env.get("HOLIDAYS_BASE_URL", cls.holidays_base_url),
            states_url=env.get("STATES_URL", cls.states_url),
            http_attempts=int(env.get("API_ATTEMPTS", cls.http_attempts)),
            backoff_base=float(env.get("API_BACKOFF_BASE", cls.backoff_base)),
        )
        clean = {k: v for k, v in overrides.items() if v is not None}
        for k in ("input_dir", "internal_dir", "raw_dir"):
            if k in clean:
                clean[k] = Path(clean[k])
        return replace(base, **clean)
