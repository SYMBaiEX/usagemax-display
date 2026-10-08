"""Numeric-only usage accounting. No prompts or responses are retained."""
from __future__ import annotations

import json
import math
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


def timestamp(value):
    try:
        result = float(value) if isinstance(value, (int, float)) else datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
        return result if math.isfinite(result) else 0.0
    except (ValueError, TypeError, OverflowError):
        return 0.0


def counter(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('invalid token counter')
    return int(value)


def output_rate(previous, current):
    """Observed output/s between source events, never input/cache or poll time.

    A gap over two minutes cannot describe active generation. Counter resets,
    missing baselines and duplicate/out-of-order records have no estimate.
    """
    elapsed = current[0] - previous[0]
    delta = current[1] - previous[1]
    if not .1 <= elapsed <= 120 or delta < 0:
        return None
    return delta / elapsed


@dataclass(frozen=True)
class DailyUsage:
    source: str
    harness: str
    day: str
    tokens: int
    updated_at: float
    complete: bool = True

    @classmethod
    def parse(cls, source, payload):
        day = str(payload['date'])
        if datetime.strptime(day, '%Y-%m-%d').strftime('%Y-%m-%d') != day:
            raise ValueError('invalid usage date')
        return cls(source, str(payload.get('harness') or 'Unknown harness'), day,
                   counter(payload['today_tokens']), timestamp(payload.get('timestamp')), payload.get('complete', True) is True)


def live_day_total(rows, now):
    """One latest observation per host/harness; never add these to account data."""
    day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    latest = {}
    for row in rows:
        if not row.complete or row.day != day or not 0 <= now-row.updated_at < 120:
            continue
        key = (row.source.casefold(), row.harness.casefold())
        if key not in latest or row.updated_at > latest[key].updated_at:
            latest[key] = row
    return sum(row.tokens for row in latest.values()) if latest else None


@dataclass
class _FileLedger:
    offset: int = 0
    modified: int = 0
    modern: dict = field(default_factory=dict)
    legacy: dict = field(default_factory=dict)
    last_legacy_total: int | None = None
    turn_id: str = ""
    modern_turns: set = field(default_factory=set)


class CodexDayLedger:
    """Incremental UTC-day ledger, including sessions started before midnight.

    Response IDs deduplicate forked rollouts. Paired legacy token_count records
    use authoritative modern records for each turn, including delayed legacy copies. Cached input is already
    in total_tokens and is never added twice. Only complete JSONL lines commit.
    """
    def __init__(self, root: Path):
        self.root = root
        self.day = ''
        self.files = {}
        self.paths = []
        self.scanned_at = 0.0

    def snapshot(self, now=None, source='THIS MAC'):
        now = time.time() if now is None else now
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()
        if day != self.day:
            self.day, self.files, self.paths, self.scanned_at = day, {}, [], 0.0
        if now-self.scanned_at >= 10:
            self.scanned_at = now
            self.paths = []
            for path in self.root.rglob('*.jsonl'):
                try:
                    if path.stat().st_mtime >= start:
                        self.paths.append(path)
                except OSError:
                    continue
        complete = self.root.is_dir()
        for path in self.paths:
            try:
                stat = path.stat()
                state = self.files.setdefault(path, _FileLedger())
                if stat.st_size < state.offset or (stat.st_size == state.offset and stat.st_mtime_ns != state.modified):
                    state = self.files[path] = _FileLedger()
                if stat.st_size == state.offset:
                    continue
                with path.open('rb') as handle:
                    handle.seek(state.offset)
                    while raw := handle.readline():
                        if not raw.endswith(b'\n'):
                            break
                        state.offset = handle.tell()
                        if not any(kind in raw for kind in (b'"token_usage_record"', b'"token_count"', b'"turn_context"')):
                            continue
                        try:
                            event = json.loads(raw)
                            stamp = timestamp(event.get('timestamp'))
                            if stamp > now:
                                # Retry a future-stamped line after the source clock catches up.
                                state.offset -= len(raw)
                                break
                            inner = event.get('payload') or {}
                            if event.get('type') == 'turn_context':
                                state.turn_id = str(inner.get('turn_id') or '')
                                continue
                            modern = event.get('type') == 'token_usage_record'
                            if modern:
                                usage = inner.get('usage') or {}
                                turn = str(inner.get('turn_id') or state.turn_id)
                                if turn:
                                    state.modern_turns.add(turn)
                                identity = str(inner.get('response_id') or '')
                                if not identity:
                                    continue
                            elif event.get('type') == 'event_msg' and inner.get('type') == 'token_count':
                                info = inner.get('info') or {}
                                usage = info.get('last_token_usage') or {}
                                cumulative = counter((info.get('total_token_usage') or {}).get('total_tokens'))
                                if cumulative == state.last_legacy_total:
                                    continue
                                state.last_legacy_total = cumulative
                                # Copied histories and duplicate cumulative snapshots count once.
                                identity = (cumulative, timestamp(event.get('timestamp')))
                            else:
                                continue
                            signature = (counter(usage.get('input_tokens')), counter(usage.get('output_tokens')),
                                         counter(usage.get('total_tokens')))
                            record = (stamp, signature, turn if modern else state.turn_id)
                            (state.modern if modern else state.legacy)[identity] = record
                        except (ValueError, TypeError, AttributeError):
                            continue
                state.modified = stat.st_mtime_ns
            except OSError:
                complete = False
                continue
        modern, legacy = {}, {}
        modern_turns = set().union(*(state.modern_turns for state in self.files.values()))
        paired_legacy = set()
        for state in self.files.values():
            modern.update(state.modern)
            legacy.update(state.legacy)
            # Older formats have no turn IDs. Pair occurrences within the same
            # rollout, one-to-one; equal counters on another host/session are
            # never evidence of duplication. No timestamp proximity assumption.
            unmatched = Counter(signature for _, signature, turn in state.modern.values() if not turn)
            for identity, (_, signature, turn) in state.legacy.items():
                if turn and turn in modern_turns:
                    paired_legacy.add(identity)
                elif not turn and unmatched[signature]:
                    unmatched[signature] -= 1
                    paired_legacy.add(identity)
        total = sum(signature[2] for stamp, signature, _ in modern.values() if start <= stamp <= now)
        total += sum(signature[2] for identity, (stamp, signature, _) in legacy.items()
                     if identity not in paired_legacy and start <= stamp <= now)
        return {'kind': 'daily_usage', 'date': day, 'today_tokens': total,
                'source': source, 'harness': 'CODEX', 'timestamp': now, 'complete': complete,
                'event': 'CODEX / DAILY USAGE / OBSERVED', 'level': 'OK'}


class DayLedgerWorker:
    """Keep the initial history scan off the event relay and display loops."""
    def __init__(self, root: Path, source: str):
        self.ledger, self.source = CodexDayLedger(root), source
        self.value = None
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name='daily-usage', daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stopped.is_set():
            try:
                self.value = self.ledger.snapshot(source=self.source)
            except (OSError, ValueError):
                pass  # Last successful source timestamp will expire normally.
            self.stopped.wait(5)

    def close(self):
        self.stopped.set()


def resolve_session_key(session_id, known):
    if session_id in known:
        return session_id
    if len(session_id) >= 12:
        matches = [key for key in known if key.casefold().endswith(session_id[-12:].casefold())]
        if len(matches) == 1:
            return matches[0]
    return session_id


def canonical_usages(snapshot, now, max_age=1200):
    """One latest host/harness/session observation shared by every metric."""
    known = {}
    for edge in snapshot.agent_relations:
        if 0 <= now - edge.updated_at < max_age:
            known.setdefault(edge.source.strip().casefold(), set()).update((edge.parent_id, edge.child_id))
    latest = {}
    streams = snapshot.live_usages or ((snapshot.live_usage,) if snapshot.live_usage else ())
    for row in streams:
        if not 0 <= now - row.updated_at < max_age:
            continue
        host = row.source.strip().casefold()
        harness = (row.harness or row.agent or 'Unknown harness').strip().casefold()
        session = resolve_session_key(row.session_id.strip(), known.get(host, set()))
        key = (host, harness, session or 'primary')
        if key not in latest or row.updated_at > latest[key].updated_at:
            latest[key] = row
    return latest


def observed_output_rate(usage, now):
    if not 0 <= now - usage.updated_at < 45:
        return None
    if getattr(usage, 'execution_state', '') in {'completed', 'idle', 'stopped', 'failed'}:
        return None
    rate = usage.token_rate
    if not math.isfinite(rate) or rate < 0 or not (usage.rate_available or rate > 0):
        return None
    return rate
