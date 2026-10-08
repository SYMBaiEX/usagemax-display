"""Bounded, display-only activity summaries and scalable agent families."""
import math
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any

from .usage_counters import canonical_usages


@dataclass(frozen=True)
class AgentNode:
    key: str
    name: str
    model: str
    parent: str = ""
    updated_at: float = 0
    active: bool = False


@dataclass(frozen=True)
class AgentFamily:
    root: AgentNode
    children: tuple[tuple[AgentNode, int], ...]


@dataclass(frozen=True)
class ModelLoad:
    model: str
    sessions: int
    active: int
    tokens: int
    rate: float
    updated_at: float


def model_loads(rows: list[Any], now: float) -> tuple[ModelLoad, ...]:
    groups: dict[str, list[Any]] = {}
    for row in rows:
        groups.setdefault((getattr(row, "provider", "").casefold(), row.model.upper()), []).append(row)
    result = []
    for (_, model), members in groups.items():
        fresh = [row for row in members if row.last_seen and 0 <= now - row.last_seen < 45]
        result.append(ModelLoad(model, len(members), len(fresh), sum(row.session_tokens for row in members),
                                sum(max(0.0, row.token_rate) for row in fresh if math.isfinite(row.token_rate)),
                                max((row.last_seen or 0 for row in members), default=0)))
    return tuple(sorted(result, key=lambda row: (-row.rate, -row.updated_at, row.model)))


def agent_families(relations: Any, now: float) -> tuple[AgentFamily, ...]:
    """Keep real identity/ancestry, including nested trees and closed children."""
    nodes: dict[str, AgentNode] = {}
    parents: dict[str, str] = {}
    for edge in sorted(relations, key=lambda item: item.updated_at):
        if not edge.parent_id or not edge.child_id or now - edge.updated_at > 1200:
            continue
        for key, name, model, stamp, active in (
            (edge.parent_id, edge.parent_name, edge.parent_model, edge.parent_updated_at, True),
            (edge.child_id, edge.child_name, edge.child_model, edge.child_updated_at, edge.status == "OPEN"),
        ):
            prior = nodes.get(key)
            if prior is None or stamp >= prior.updated_at:
                nodes[key] = AgentNode(key, name or "Agent", model or "unknown", "", stamp,
                                       active and 0 <= now - stamp < 90)
        if edge.parent_id != edge.child_id:
            parents[edge.child_id] = edge.parent_id
    # Break malformed cycles deterministically so every identity remains visible.
    for key in sorted(nodes):
        path = []
        cursor = key
        while cursor in parents:
            if cursor in path:
                parents.pop(min(path[path.index(cursor):]), None)
                break
            path.append(cursor)
            cursor = parents[cursor]
    children: dict[str, list[str]] = {}
    for child, parent in parents.items():
        children.setdefault(parent, []).append(child)
    roots = sorted((key for key in nodes if key not in parents),
                   key=lambda key: (nodes[key].name.lower(), key))
    families = []
    for root in roots:
        descendants = []
        queue = deque((child, 1) for child in sorted(children.get(root, ())))
        while queue:
            child, depth = queue.popleft()
            node = nodes[child]
            descendants.append((AgentNode(node.key, node.name, node.model, parents[child],
                                           node.updated_at, node.active), depth))
            queue.extend((key, depth + 1) for key in sorted(children.get(child, ())))
        families.append(AgentFamily(nodes[root], tuple(descendants)))
    return tuple(families)


def family_tiles(width: int, height: int, total: int, now: float) -> tuple[list[tuple[int, int, int, int]], int, int]:
    """Fixed readable tiles; page overflow instead of squeezing text."""
    columns = max(1, width // 104)
    rows = max(1, height // 31)
    capacity = columns * rows
    pages = max(1, math.ceil(total / capacity))
    page = int(now // 9) % pages
    boxes = []
    for index in range(min(capacity, max(0, total - page * capacity))):
        column, row = index % columns, index // columns
        boxes.append((column * width // columns, row * 31,
                      (column + 1) * width // columns - 5, row * 31 + 26))
    return boxes, page * capacity, pages


class ActivityWindow:
    """Local token deltas and real event counts; never account-sync deltas."""
    def __init__(self) -> None:
        self.baselines: OrderedDict = OrderedDict()
        self.deltas: deque = deque(maxlen=8192)
        self.seen: OrderedDict = OrderedDict()
        self.events: deque = deque(maxlen=4096)
        self.samples: deque = deque(maxlen=60)
        self.last_sample = -1
        self.started_at = None
        self.now = 0.0
        self.gap_until = 0.0

    def update(self, snapshot: Any, now: float, rate: float) -> None:
        self.now = now
        if self.started_at is None:
            self.started_at = now
        for key, usage in canonical_usages(snapshot, now).items():
            current = (usage.updated_at, usage.session_tokens)
            prior = self.baselines.get(key)
            if prior is not None and current[0] <= prior[0]:
                continue
            if prior is not None and 0 <= now - current[0] < 45 and current[1] >= prior[1]:
                # Both ends must be observed within a bounded source interval.
                if 0 < current[0] - prior[0] <= 60:
                    self.deltas.append((prior[0], current[0], current[1] - prior[1]))
                else:
                    self.gap_until = now + 60
            self.baselines[key] = current
            self.baselines.move_to_end(key)
            if len(self.baselines) > 4096:
                self.baselines.popitem(last=False)
        if any(0 <= now - row.timestamp < 300 for row in snapshot.otel_records):
            observed = [(row.timestamp, row.service, row.name, row.status, ("otel", *row.identity)) for row in snapshot.otel_records]
        else:
            observed = [(event.timestamp, event.source, event.message, event.level,
                         ("event", event.timestamp, event.source, event.message)) for event in snapshot.events]
        for stamp, source, message, status, key in sorted(observed):
            if key in self.seen or not 0 <= now - stamp < 300:
                continue
            self.seen[key] = True
            if len(self.seen) > 4096:
                self.seen.popitem(last=False)
            self.events.append((stamp, source, message, status))
        self.deltas = deque((row for row in self.deltas if row[1] >= now - 60), maxlen=8192)
        # A late source can deliver out of order; don't assume timestamp order.
        self.events = deque((row for row in self.events if row[0] >= now - 300), maxlen=4096)
        if int(now) != self.last_sample:
            self.samples.append(self.tokens)
            self.last_sample = int(now)

    @property
    def tokens(self) -> int:
        return sum(delta for start, end, delta in self.deltas if self.now - 60 <= start <= end <= self.now)

    @property
    def partial(self) -> bool:
        return self.started_at is None or self.now - self.started_at < 60 or self.now < self.gap_until

    def events_per_minute(self, now: float) -> int:
        return sum(stamp >= now - 60 for stamp, *_ in self.events)

    def latest(self) -> tuple[float, str, str, str] | None:
        useful = [row for row in self.events if any(token in row[2].upper() for token in
                  ("TOOL", "EXEC", "FAIL", "ERROR", "TASK_COMPLETE", "SPAWN", "LAUNCH", "TURN"))]
        return max(useful or self.events, key=lambda row: row[0], default=None)


def activity_label(message: str, status: str) -> str:
    value = message.upper()
    if status.upper() in {"ERROR", "ERR"} or "FAIL" in value:
        return "Recovering from an error"
    if "TASK_COMPLETE" in value or "TURN_COMPLETE" in value:
        return "Task completed"
    if "SPAWN" in value or "LAUNCH" in value:
        return "A new agent joined"
    if "TOOL" in value or "EXEC" in value:
        return "Agent executing tools"
    if "TURN" in value:
        return "Working on a new turn"
    if "TOKEN" in value:
        return "Model usage updated"
    return "Agent activity received"
