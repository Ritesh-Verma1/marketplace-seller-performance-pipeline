"""HTTP client with timeouts, retries + exponential backoff and a raw-response cache.

The cache directory *is* the raw layer for API data: every successful response body
is stored byte-for-byte as received (before any business rule) and re-used on later
runs unless --refresh-cache is given.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Callable

import requests

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 425, 429}


class ExtractionError(RuntimeError):
    """Raised when source data cannot be obtained completely. Aborts the run."""


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


class ApiClient:
    def __init__(
        self,
        cache_dir: Path,
        timeout: float = 20.0,
        attempts: int = 4,
        backoff_base: float = 1.0,
        refresh: bool = False,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if attempts < 3:
            raise ValueError("assignment requires at least 3 attempts")
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.attempts = attempts
        self.backoff_base = backoff_base
        self.refresh = refresh
        self.session = session or requests.Session()
        self.sleep = sleep

    def cache_path(self, name: str) -> Path:
        return self.cache_dir / f"{_safe(name)}.json"

    def get_text(self, name: str, url: str, params: dict | None = None) -> str:
        """GET `url`; return the raw body text. Raises ExtractionError on failure."""
        path = self.cache_path(name)
        if path.exists() and not self.refresh:
            log.info("API cache hit: %s", path.name)
            return path.read_text(encoding="utf-8")

        last_error = "unknown error"
        for attempt in range(1, self.attempts + 1):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                if resp.status_code == 200:
                    body = resp.text
                    json.loads(body)  # must be valid JSON, otherwise treat as a failed attempt
                    tmp = path.with_suffix(".tmp")
                    tmp.write_text(body, encoding="utf-8")
                    os.replace(tmp, path)  # atomic: never leave a half-written raw file
                    return body
                last_error = f"HTTP {resp.status_code}"
                if resp.status_code < 500 and resp.status_code not in RETRYABLE_STATUS:
                    # 4xx (other than 408/425/429) will not fix itself - fail fast and clearly
                    raise ExtractionError(
                        f"API '{name}' returned non-retryable {last_error} for {url} "
                        f"params={params}. Pipeline aborted; no data was loaded."
                    )
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            except json.JSONDecodeError as exc:
                last_error = f"invalid JSON body ({exc})"
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"

            if attempt < self.attempts:
                delay = self.backoff_base * (2 ** (attempt - 1))
                log.warning("API '%s' attempt %d/%d failed (%s); retrying in %.1fs",
                            name, attempt, self.attempts, last_error, delay)
                self.sleep(delay)

        raise ExtractionError(
            f"API '{name}' ({url}) failed after {self.attempts} attempts: {last_error}. "
            "Pipeline aborted rather than loading partial data."
        )
