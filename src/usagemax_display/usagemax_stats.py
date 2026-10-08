"""Read-only UsageMax public API adapter, isolated from the USB/frame loop."""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

MAX_RESPONSE_BYTES = 512_000
COST_BASES = {"reported", "estimated", "api-equivalent", "mixed", "unknown"}


def number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected a numeric counter")
    if not math.isfinite(value) or value < 0:
        raise ValueError("invalid counter")
    return value


@dataclass(frozen=True)
class UsageMaxStats:
    total_tokens: int
    cache_read_tokens: int
    total_spend: float | None
    sessions: int | None
    devices: int | None
    sources: tuple[str, ...]
    cost_basis: str
    # Preserve provider identity; do not merge equal model names across providers.
    models: tuple[tuple[str, str, int], ...]
    last_sync_at: float
    fetched_at: float
    status: str
    daily: tuple[tuple[str, int], ...] | None = None
    top_model: str = ""
    top_model_metric: str = ""
    integrity_residual: int | None = None

    def today_tokens(self, now: float) -> int | None:
        if self.daily is None:
            return None
        today = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        return next((tokens for day, tokens in self.daily if day == today), None)

    def window_tokens(self, now: float, days: int) -> tuple[int | None, ...] | None:
        """UTC calendar days ending today; ignore out-of-window/future rows."""
        if not 1 <= days <= 28:
            raise ValueError("daily window must be between 1 and 28 days")
        if self.daily is None:
            return None
        today = datetime.fromtimestamp(now, timezone.utc).date()
        values = dict(self.daily)
        return tuple(values.get((today - timedelta(days=offset)).isoformat())
                     for offset in range(days - 1, -1, -1))

def parse_profile(payload: Any, now: float) -> UsageMaxStats:
    if not isinstance(payload, dict) or not isinstance(payload.get("stats"), dict):
        raise ValueError("missing profile stats")
    stats = payload["stats"]
    models = payload.get("models", [])
    sources = stats.get("sources", [])
    if not isinstance(models, list) or not isinstance(sources, list):
        raise ValueError("invalid profile dimensions")
    rows = []
    for row in models:
        if not isinstance(row, dict):
            raise ValueError("invalid model row")
        rows.append((str(row.get("provider") or "unknown")[:80],
                     str(row.get("model") or "unattributed")[:120],
                     int(number(row.get("totalTokens")))))
    basis = stats.get("costBasis", "unknown")
    sync = stats.get("syncStatus", "stale")
    synced_at = number(stats.get("lastSyncAt") or 0) / 1000  # API Unix milliseconds
    return UsageMaxStats(
        total_tokens=int(number(stats.get("totalTokens"))),
        cache_read_tokens=int(number(stats.get("cacheReadTokens") or 0)),
        total_spend=(number(stats["totalSpendUsd"]) if stats.get("totalSpendUsd") is not None else None),
        sessions=(int(number(stats["sessionCount"])) if stats.get("sessionCount") is not None else None),
        devices=(int(number(stats["deviceCount"])) if stats.get("deviceCount") is not None else None),
        sources=tuple(sorted({item[:80] for item in sources if isinstance(item, str) and item})),
        cost_basis=basis if isinstance(basis, str) and basis in COST_BASES else "unknown",
        models=tuple(sorted(rows, key=lambda row: (-row[2], row[0], row[1]))[:12]),
        top_model=str(stats.get("topModel") or ""),
        top_model_metric=str(stats.get("topModelMetric") or ""),
        integrity_residual=(sum(int(number(stats[key])) for key in ("inputTokens", "outputTokens", "cacheReadTokens",
                          "cacheWriteTokens", "reasoningTokens", "unclassifiedTokens")) - int(number(stats["totalTokens"]))
                          if all(key in stats for key in ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "reasoningTokens", "unclassifiedTokens")) else None),
        last_sync_at=synced_at,
        fetched_at=now,
        status=("SYNCED" if synced_at else "STALE") if sync == "healthy" else (
            "DEGRADED" if sync == "degraded" else "STALE"
        ),
    )


def parse_daily(payload: Any) -> tuple[tuple[str, int], ...]:
    if not isinstance(payload, dict) or not isinstance(payload.get("days"), list):
        raise ValueError("missing daily stats")
    # Only the ungrouped endpoint is authoritative for account daily totals.
    if payload.get("groupBy") is not None:
        raise ValueError("grouped daily stats are not account totals")
    days = {}
    for row in payload["days"]:
        if not isinstance(row, dict):
            raise ValueError("invalid day")
        key = row.get("date")
        if not isinstance(key, str) or date.fromisoformat(key).isoformat() != key or key in days:
            raise ValueError("invalid or duplicate day")
        days[key] = int(number(row.get("totalTokens")))
    return tuple(sorted(days.items())[-28:])


def fetch_json(url: str, timeout: float) -> Any:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "UsageMax-Display/1"})
    with urlopen(request, timeout=timeout) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("oversized UsageMax response")
    return json.loads(raw)


class UsageMaxClient:
    """One sleeping worker; snapshot reads never perform network or disk I/O."""

    def __init__(self, config: dict[str, Any], *, fetcher=fetch_json, start: bool = True) -> None:
        self.interval = max(60.0, float(config.get("usagemax_poll_seconds", 120)))
        self.timeout = min(10.0, max(0.5, float(config.get("usagemax_timeout_seconds", 4))))
        self.fetcher = fetcher
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._stats: UsageMaxStats | None = None
        self._failed = False
        self._failures = 0
        self._thread: threading.Thread | None = None
        base = str(config.get("usagemax_base_url", "https://usagemax.com")).rstrip("/")
        handle = str(config.get("usagemax_handle", "")).strip()
        parsed = urlsplit(base)
        self.enabled = bool(config.get("usagemax_enabled", False)) and bool(handle)
        # HTTP is useful for a local fixture/self-hosted service, never credentials.
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            self.enabled = False
        self.url = f"{base}/api/profiles/{quote(handle, safe='')}"
        if start and self.enabled:
            self._thread = threading.Thread(target=self._run, name="usagemax-stats", daemon=True)
            self._thread.start()

    def refresh(self) -> bool:
        """Called only by the worker (or deterministic tests). Publish atomically."""
        try:
            stats = parse_profile(self.fetcher(self.url, self.timeout), time.time())
        except Exception:
            # Errors/HTML/auth failures never replace valid counters with zero.
            with self._lock:
                self._failed = True
            self._failures += 1
            return False
        if self._stop.is_set():
            return False
        try:
            daily = parse_daily(self.fetcher(f"{self.url}/daily?days=28", self.timeout))
            stats = replace(stats, daily=daily)
            complete = True
        except Exception:
            # A partial API rollout must not hide valid profile totals or imply
            # that today's missing counters are zero.
            stats = replace(stats, status="PARTIAL" if stats.status == "SYNCED" else stats.status)
            complete = False
        with self._lock:
            self._stats = stats
            self._failed = False
        self._failures = 0 if complete else self._failures + 1
        return complete

    def snapshot(self) -> UsageMaxStats | None:
        with self._lock:
            stats, failed = self._stats, self._failed
        if stats is not None and (failed or time.time() - stats.fetched_at > self.interval * 2):
            return replace(stats, status="STALE")
        if stats is not None and stats.integrity_residual:
            return replace(stats, status="MISMATCH")
        if stats is not None and stats.status == "SYNCED" and not 0 <= time.time() - stats.last_sync_at < self.interval * 2:
            return replace(stats, status="DELAYED")
        return stats

    def retry_delay(self) -> float:
        return max(self.interval, min(900.0, self.interval * 2 ** min(self._failures, 4)))

    def _run(self) -> None:
        while not self._stop.is_set():
            self.refresh()
            if self._stop.wait(self.retry_delay()):
                return

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.1)
