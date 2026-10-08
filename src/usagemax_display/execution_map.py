"""Readable harness/model/session flow layout, independent of telemetry vendors."""
import math
from dataclasses import dataclass

from .constellation import Constellation, ModelCluster, SessionNode


def delegation_arrow(previous, end):
    """Stop at the child glyph and point along the actual incoming path."""
    dx, dy = end[0] - previous[0], end[1] - previous[1]
    length = math.hypot(dx, dy) or 1
    ux, uy = dx / length, dy / length
    tip = (end[0] - ux * 8, end[1] - uy * 8)
    return ((tip[0] - ux*5 - uy*3, tip[1] - uy*5 + ux*3), tip,
            (tip[0] - ux*5 + uy*3, tip[1] - uy*5 - ux*3))


@dataclass(frozen=True)
class ModelLane:
    cluster: ModelCluster
    center: float
    height: float
    nodes: tuple[tuple[SessionNode, float, float], ...]
    page: int
    pages: int


@dataclass(frozen=True)
class FlowLayout:
    lanes: tuple[ModelLane, ...]
    sources: tuple[tuple[tuple[str, str], tuple[SessionNode, ...]], ...]
    page: int
    pages: int


def flow_layout(graph: Constellation, now: float, height: int = 240, width: int = 1080) -> FlowLayout:
    """Keep readable names and pin parents, with complete overflow coverage."""
    batches = [graph.clusters[i:i + 6] for i in range(0, len(graph.clusters), 6)]
    if not batches:
        return FlowLayout((), (), 0, 1)
    parents = {key for key, _ in graph.edges}
    columns = 1 if width < 1000 else 2
    agent_x = width - 192 if columns == 1 else 714

    def members(cluster, count):
        capacity = min(12, max(2, columns * int(height / count // 20)))
        pinned = tuple(node for node in cluster.nodes if node.key in parents)[:max(1, capacity // 3)]
        known = {node.key for node in pinned}
        rest = tuple(node for node in cluster.nodes if node.key not in known)
        free = capacity - len(pinned)
        return pinned, rest, free, max(1, math.ceil(len(rest) / free))

    durations = [max(members(cluster, len(batch))[3] for cluster in batch) * 10 for batch in batches]
    elapsed = int(now) % sum(durations)
    page = 0
    while elapsed >= durations[page]:
        elapsed -= durations[page]
        page += 1
    batch = batches[page]
    lane_height = height / len(batch)
    lanes = []
    for index, cluster in enumerate(batch):
        pinned, rest, free, pages = members(cluster, len(batch))
        subpage = elapsed // 10 % pages
        visible = pinned + tuple(rest[(subpage * free + i) % len(rest)] for i in range(min(len(rest), free)))
        rows = math.ceil(len(visible) / columns)
        center = (index + .5) * lane_height
        points = []
        for i, node in enumerate(visible):
            column, row = i % columns, i // columns
            y = center + (row - (rows - 1) / 2) * 20
            points.append((node, agent_x + column * 202, y))
        lanes.append(ModelLane(cluster, center, lane_height, tuple(points), subpage, pages))
    sources = {}
    for lane in lanes:
        for node in lane.cluster.nodes:
            sources.setdefault((node.harness, node.source), []).append(node)
    return FlowLayout(tuple(lanes), tuple((key, tuple(value)) for key, value in sorted(sources.items())), page, len(batches))
