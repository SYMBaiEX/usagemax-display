"""Harness-neutral session graph and bounded orbital layout. No I/O or fake agents."""
import hashlib
import math
from dataclasses import dataclass, replace
from typing import Any

from .usage_counters import canonical_usages, observed_output_rate

PALETTE = ("#FF2499", "#00F0FF", "#B875FF", "#BEFF24", "#FF9F43", "#63A4FF",
           "#FF617C", "#61FFCF", "#FFE36A", "#DD78FF", "#64CFFF", "#FF70CB")
MODEL_LIMIT = 6
ORBIT_LIMIT = 48
PAGE_SECONDS = 12
Key = tuple[str, str, str]


def model_color(model: str) -> str:
    """Any model gets a repeatable accent without a vendor/model allowlist."""
    digest = hashlib.sha256(model.strip().casefold().encode()).digest()
    return PALETTE[int.from_bytes(digest[8:12], "big") % len(PALETTE)]


@dataclass(frozen=True)
class SessionNode:
    key: Key
    session: str
    name: str
    model: str
    provider: str
    harness: str
    source: str
    updated_at: float
    active: bool
    rate: float | None = None
    tokens: int | None = None

    @property
    def model_key(self) -> str:
        return f"{self.provider.casefold()}\x1f{self.model.casefold()}"


@dataclass(frozen=True)
class ModelCluster:
    key: str
    model: str
    provider: str
    nodes: tuple[SessionNode, ...]

    @property
    def rate(self) -> float | None:
        measured = [node.rate for node in self.nodes if node.rate is not None]
        return sum(measured) if measured else None


@dataclass(frozen=True)
class Constellation:
    clusters: tuple[ModelCluster, ...] = ()
    edges: tuple[tuple[Key, Key], ...] = ()

    @property
    def nodes(self) -> tuple[SessionNode, ...]:
        return tuple(node for cluster in self.clusters for node in cluster.nodes)


def session_graph(snapshot: Any, now: float) -> Constellation:
    """Keep host/harness/session identity distinct and join legacy short IDs safely.

    Harness is explicit telemetry, never inferred from a model's spelling.
    Legacy topology can inherit a harness only from one unambiguous usage node.
    """
    edges = [edge for edge in snapshot.agent_relations if 0 <= now - edge.updated_at < 1200]
    nodes: dict[Key, SessionNode] = {}
    for key, usage in canonical_usages(snapshot, now).items():
        source = usage.source.strip() or "Unknown host"
        harness = (usage.harness or usage.agent or "Unknown harness").strip()
        session = key[2]
        prior = nodes.get(key)
        if prior and prior.updated_at >= usage.updated_at:
            continue
        fresh = 0 <= now - usage.updated_at < 45
        rate = observed_output_rate(usage, now)
        nodes[key] = SessionNode(key, session, "", usage.model.strip() or "Unknown model",
                                 getattr(usage, "provider", "").strip(), harness, source,
                                 usage.updated_at, fresh, rate,
                                 max(0, usage.session_tokens))

    links = set()
    for edge in sorted(edges, key=lambda edge: edge.updated_at):
        endpoints = []
        for role in ("parent", "child"):
            session = getattr(edge, f"{role}_id")
            if not session:
                break
            source = edge.source.strip() or "Unknown host"
            hint = getattr(edge, f"{role}_harness", "") or getattr(edge, "harness", "")
            candidates = [key for key in nodes if key[0] == source.casefold() and key[2] == session
                          and (not hint or key[1] == hint.casefold())]
            key = candidates[0] if len(candidates) == 1 else (source.casefold(), (hint or "Unknown harness").casefold(), session)
            stamp = getattr(edge, f"{role}_updated_at")
            name = getattr(edge, f"{role}_name")
            model = getattr(edge, f"{role}_model") or "Unknown model"
            prior = nodes.get(key)
            if prior is None:
                nodes[key] = SessionNode(key, session, name, model,
                                         getattr(edge, f"{role}_provider", ""), hint or "Unknown harness",
                                         source, stamp, 0 <= now - stamp < 90 and
                                         (role == "parent" or edge.status == "OPEN"))
            else:
                # Numeric counters always retain their own timestamp and model;
                # a newer topology heartbeat must never resurrect stale speed.
                nodes[key] = replace(prior, name=name or prior.name)
            endpoints.append(key)
        if len(endpoints) == 2 and endpoints[0] != endpoints[1]:
            links.add(tuple(endpoints))
    groups: dict[str, list[SessionNode]] = {}
    for node in nodes.values():
        groups.setdefault(node.model_key, []).append(node)
    clusters = []
    for key, members in sorted(groups.items()):
        members.sort(key=lambda node: node.key)
        exemplar = max(members, key=lambda node: node.updated_at)
        clusters.append(ModelCluster(key, exemplar.model, exemplar.provider, tuple(members)))
    return Constellation(tuple(clusters), tuple(sorted(links)))


@dataclass(frozen=True)
class Orbit:
    cluster: ModelCluster
    center: tuple[int, int]
    radius: tuple[int, int]
    points: tuple[tuple[SessionNode, tuple[int, int]], ...]
    offset: int
    pages: int


@dataclass(frozen=True)
class GraphLayout:
    orbits: tuple[Orbit, ...]
    page: int
    pages: int

    @property
    def points(self) -> dict[Key, tuple[int, int]]:
        return {node.key: point for orbit in self.orbits for node, point in orbit.points}


def orbital_layout(graph: Constellation, width: int, height: int, now: float) -> GraphLayout:
    """Stable slots between roster changes; every model and session gets a page.

    Model pages dwell long enough to visit every session page in that group.
    Ordering never depends on fluctuating throughput or randomized Python hashes.
    """
    batches = [graph.clusters[i:i + MODEL_LIMIT] for i in range(0, len(graph.clusters), MODEL_LIMIT)]
    if not batches:
        return GraphLayout((), 0, 1)
    parents = {key for key, _ in graph.edges}

    def page_members(cluster):
        pinned = tuple(node for node in cluster.nodes if node.key in parents)[:12]
        pinned_keys = {node.key for node in pinned}
        rest = tuple(node for node in cluster.nodes if node.key not in pinned_keys)
        capacity = ORBIT_LIMIT - len(pinned)
        return pinned, rest, capacity, max(1, math.ceil(len(rest) / capacity))

    durations = [max(page_members(cluster)[3] for cluster in batch) * PAGE_SECONDS for batch in batches]
    elapsed = int(now) % sum(durations)
    page = 0
    while elapsed >= durations[page]:
        elapsed -= durations[page]
        page += 1
    visible = batches[page]
    columns = len(visible) if len(visible) <= 3 else (2 if len(visible) == 4 else 3)
    rows = math.ceil(len(visible) / columns)
    orbits = []
    for index, cluster in enumerate(visible):
        row, column = divmod(index, columns)
        # Center a partial final row without a dead empty slot.
        in_row = min(columns, len(visible) - row * columns)
        cw, ch = (width - 24) / columns, (height - 12) / rows
        cx = round(width / 2 + (column - (in_row - 1) / 2) * cw)
        cy = round(6 + (row + 0.5) * ch)
        rx, ry = round(cw / 2 - 13), round(ch / 2 - 10)
        pinned, rest, capacity, pages = page_members(cluster)
        offset = (elapsed // PAGE_SECONDS % pages) * capacity
        # Keep coordinators anchored and fill the last page from the beginning.
        # Every session is still visited, without a nearly empty final orbit.
        members = pinned + tuple(rest[(offset + i) % len(rest)] for i in range(min(capacity, len(rest))))
        # Two separated ellipses leave a clear center for model/counter labels.
        outer_count = min(28, len(members))
        points = []
        for ordinal, node in enumerate(members):
            outer = ordinal < outer_count
            count = outer_count if outer else len(members) - outer_count
            slot = ordinal if outer else ordinal - outer_count
            angle = -math.pi / 2 + math.tau * slot / max(1, count) + (0 if outer else 0.1)
            factor_x, factor_y = (1, 1) if outer else (.83, .81)
            point = (round(cx + math.cos(angle) * rx * factor_x),
                     round(cy + math.sin(angle) * ry * factor_y))
            points.append((node, point))
        orbits.append(Orbit(cluster, (cx, cy), (rx, ry), tuple(points), offset, pages))
    return GraphLayout(tuple(orbits), page, len(batches))
