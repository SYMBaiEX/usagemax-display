#!/usr/bin/env python3
"""UsageMax for the Thermalright Trofeo Vision 9.16 USB LCD.

The display is a USB framebuffer, not a normal monitor. This small runtime
keeps the device ownership and rendering path in one process, then presents a
calm summary of local agent activity plus optional remote reachability.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import random
import re
import socket
import subprocess
import sys
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import (
    Image,
    ImageChops,
    ImageColor,
    ImageDraw,
    ImageEnhance,
    ImageFilter,
    ImageFont,
    ImageOps,
)

from .activity_feed import LANES, ActivityFeed
from .api import validate_frame_sink, validate_renderer
from .constellation import PALETTE, Constellation, model_color, session_graph
from .deck_telemetry import ActivityWindow, model_loads
from .display_effects import CachedDraw, NeonBloom, OverdriveDirector, PanelProtector
from .execution_map import delegation_arrow, flow_layout
from .otel_ingest import OtelFileIngestor, OtelRecord, OtlpHttpReceiver
from .paths import default_data_dir, resolve_path
from .pet_motion import HabitatDirector, MotionLibrary
from .plugins import PluginLoadError, load_plugin
from .usage_counters import (
    DailyUsage,
    DayLedgerWorker,
    canonical_usages,
    counter,
    live_day_total,
    observed_output_rate,
    output_rate,
)
from .usagemax_stats import UsageMaxClient, UsageMaxStats

try:
    from usb.core import find as pyusb_find
except ImportError:  # USB is optional for preview-only use.
    pyusb_find = None

try:
    import usb.util as usb_util
except ImportError:  # USB is optional for preview-only use.
    usb_util = None

try:
    from libusb_package import find as usb_find
except ImportError:  # pragma: no cover - generic PyUSB backend
    usb_find = pyusb_find

try:
    import psutil
except ImportError:  # pragma: no cover - optional during preview-only work
    psutil = None

try:
    import pynvml
except ImportError:  # pragma: no cover - optional GPU telemetry
    pynvml = None


VID = 0x0416
PID = 0x5408
WIDTH = 1920
HEIGHT = 462
AGGREGATE_RIGHT = 400
LIVE_SPLIT = 1158
PET_LEFT = 1480
TOPOLOGY_BOTTOM = 282
CHUNK_SIZE = 512
CHUNK_DATA_SIZE = 496
WRITE_BURST = 4096
# Vendor captures cluster around 310-320 KiB. Staying under that envelope also
# bounds packet count if a future background is unusually difficult to compress.
MAX_JPEG_BYTES = 320_000
MAX_PANEL_FPS = 30.0
MIN_PANEL_FPS = 0.2
LEVEL_TOKEN_CAP = 2_000_000_000_000
MAX_PET_LEVEL = 99

BG = "#020308"
PANEL = "#070A13"
PANEL_2 = "#101321"
GRID = "#52677E"
GRID_SOFT = "#182438"
TEXT = "#F5FAFF"
MUTED = "#BDCDD9"
PURPLE = "#B875FF"
MAGENTA = "#FF2499"
BRAND_ORANGE = "#FF4C00"
ORANGE = "#FF9F43"
BLUE = "#63A4FF"
CYAN = "#00F0FF"
GREEN = "#BEFF24"
AMBER = "#FFE36A"
RED = "#FF4D70"

NEON_DIM = {
    CYAN: "#105A66",
    PURPLE: "#4B2D69",
    MAGENTA: "#731444",
    BRAND_ORANGE: "#712400",
    GREEN: "#4B621D",
    AMBER: "#65551D",
    RED: "#672134",
}

BLOOM_THRESHOLD_LUT = tuple(
    0 if value < 86 else min(255, (value - 86) * 3)
    for value in range(256)
)

SECRET_PATTERNS = (
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-+/=]+"), r"\1•••"),
    (re.compile(r"(?i)\b(sk-[A-Za-z0-9]{12,}|ghp_[A-Za-z0-9]{12,}|xox[baprs]-[A-Za-z0-9-]+)\b"), "•••"),
    (re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)\S+"), r"\1•••"),
)


def redact(value: str, limit: int = 96) -> str:
    """Keep log snippets useful without putting credentials on the LCD."""
    text = " ".join(value.replace("\x00", "").split())
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text[:limit].rstrip()


def font(
    size: int, mono: bool = False, bold: bool = False
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if os.name == "nt":
        if mono:
            candidates = (r"C:\Windows\Fonts\consolab.ttf" if bold else r"C:\Windows\Fonts\consola.ttf",)
        elif bold:
            candidates = (r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\bahnschrift.ttf")
        else:
            candidates = (r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\bahnschrift.ttf")
    elif sys.platform == "darwin":
        candidates = (("/System/Library/Fonts/Menlo.ttc",) if mono else
                      ("/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else
                       "/System/Library/Fonts/Supplemental/Arial.ttf",))
    else:
        if mono:
            candidates = ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",)
        elif bold:
            candidates = ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",)
        else:
            candidates = ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",)
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def neon_line(
    draw: ImageDraw.ImageDraw, xy: tuple[int, int, int, int], color: str,
    width: int = 2,
) -> None:
    """A crisp phosphor line with a cheap, JPEG-friendly halo."""
    draw.line(xy, fill=NEON_DIM.get(color, GRID_SOFT), width=width + 3)
    draw.line(xy, fill=color, width=width)


def neon_corners(
    draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], color: str,
    length: int = 18,
) -> None:
    """Frame a focal region without surrounding every card in glow."""
    left, top, right, bottom = box
    segments = (
        (left, top, left + length, top), (left, top, left, top + length),
        (right - length, top, right, top), (right, top, right, top + length),
        (left, bottom, left + length, bottom), (left, bottom - length, left, bottom),
        (right - length, bottom, right, bottom), (right, bottom - length, right, bottom),
    )
    for segment in segments:
        neon_line(draw, segment, color, 1)


@lru_cache(maxsize=8192)
def _font_width(value: str, face: Any) -> float:
    return face.getlength(value)


def text_width(draw: ImageDraw.ImageDraw, value: str, face: Any) -> float:
    return _font_width(value, face)


def fit_text(draw: ImageDraw.ImageDraw, value: str, face: Any, max_width: int) -> str:
    value = redact(value)
    if text_width(draw, value, face) <= max_width:
        return value
    suffix = "…"
    while value and text_width(draw, value + suffix, face) > max_width:
        value = value[:-1]
    return value + suffix


def fit_identifier(draw, value, face, max_width):
    """Keep distinguishing version/suffix text when a model identity is long."""
    value = redact(value)
    if text_width(draw, value, face) <= max_width:
        return value
    head, tail = max(1, len(value) // 3), max(1, len(value) * 2 // 3)
    while head + tail > 2:
        result = value[:head] + "…" + value[-tail:]
        if text_width(draw, result, face) <= max_width:
            return result
        if head > 5:
            head -= 1
        else:
            tail -= 1
    return fit_text(draw, value, face, max_width)


def age_text(timestamp: float | None) -> str:
    if not timestamp:
        return "—"
    age = max(0, int(time.time() - timestamp))
    if age < 60:
        return f"{age}s"
    if age < 3600:
        return f"{age // 60}m"
    return f"{age // 3600}h"


def compact_number(value: float, digits: int = 1) -> str:
    absolute = abs(value)
    for threshold, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if absolute >= threshold:
            shown = value / threshold
            decimals = 0 if abs(shown) >= 100 else digits
            return f"{shown:.{decimals}f}{suffix}"
    return f"{value:.0f}"


def parse_timestamp(value: Any, fallback: float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return fallback


@dataclass
class Agent:
    name: str
    state: str
    detail: str
    events: int = 0
    errors: int = 0
    last_seen: float | None = None
    source: str = "AI-DESKTOP"
    model: str = ""
    token_rate: float = 0
    session_tokens: int = 0
    flow_kbps: float = 0

    @property
    def color(self) -> str:
        return {
            "ACTIVE": GREEN,
            "RUNNING": CYAN,
            "IDLE": MUTED,
            "OFFLINE": RED,
            "WAITING": AMBER,
            "ERROR": RED,
        }.get(self.state, MUTED)


@dataclass
class Event:
    timestamp: float
    source: str
    message: str
    level: str = "INFO"

    @property
    def color(self) -> str:
        return {"ERROR": RED, "WARN": AMBER, "OK": GREEN}.get(self.level, CYAN)


@dataclass
class SystemStats:
    cpu: float = 0
    ram: float = 0
    ram_used: float = 0
    ram_total: float = 0
    gpu: float = 0
    vram_used: float = 0
    vram_total: float = 0
    gpu_temp: float = 0
    wsl: str = "unknown"
    host_name: str = ""


@dataclass
class LiveUsage:
    source: str = ""
    agent: str = "Unknown harness"
    model: str = ""
    session_id: str = ""
    session_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    context_window: int = 0
    token_rate: float = 0
    updated_at: float = 0
    harness: str = ""
    provider: str = ""
    rate_available: bool = False
    has_output_counter: bool = True
    execution_state: str = "unknown"


@dataclass
class AgentRelation:
    parent_id: str
    child_id: str
    parent_name: str = "Agent"
    child_name: str = "Agent"
    parent_model: str = ""
    child_model: str = ""
    status: str = "OPEN"
    source: str = "THIS MAC"
    parent_updated_at: float = 0
    child_updated_at: float = 0
    harness: str = ""
    parent_harness: str = ""
    child_harness: str = ""
    parent_provider: str = ""
    child_provider: str = ""
    observed_at: float = 0

    @property
    def updated_at(self) -> float:
        return max(self.parent_updated_at, self.child_updated_at, self.observed_at)


@dataclass
class TokenStats:
    login: str = "local"
    total_tokens: int = 0
    total_spend: float = 0
    today_tokens: int = 0
    today_spend: float = 0
    sessions: int = 0
    rank: int = 0
    streak: int = 0
    active_days: int = 0
    top_model: str = "—"
    daily: tuple[dict[str, Any], ...] = ()
    models: tuple[tuple[str, int], ...] = ()
    updated_at: float = 0
    status: str = "LINKING"


@dataclass
class Snapshot:
    agents: list[Agent]
    events: list[Event]
    stats: SystemStats
    usb_status: str = "LINKING"
    usb_detail: str = "0416:5408"
    fps: float = 0
    token_stats: TokenStats | None = None
    live_usage: LiveUsage | None = None
    flow_kbps: float = 0
    live_usages: tuple[LiveUsage, ...] = ()
    otel_records: tuple[OtelRecord, ...] = ()
    agent_relations: tuple[AgentRelation, ...] = ()
    usagemax_stats: UsageMaxStats | None = None
    daily_usages: tuple[DailyUsage, ...] = ()


@dataclass
class ModelActivity:
    model: str
    session: str = ""
    session_tokens: int = 0
    token_rate: float = 0
    last_seen: float | None = None
    instances: int = 0
    provider: str = ""


def active_model_ingest(snapshot: Snapshot, now: float) -> list[ModelActivity]:
    """Return one freshness-ranked row per active model session."""
    rows = []
    for key, usage in canonical_usages(snapshot, now).items():
        if not usage.model.strip():
            continue
        rows.append(ModelActivity(usage.model.upper(), key[2], max(0, usage.session_tokens),
                                  observed_output_rate(usage, now) or 0.0, usage.updated_at, 1, usage.provider))
    return sorted(rows, key=lambda row: (row.token_rate, row.last_seen or 0), reverse=True)


def combined_token_rate(snapshot: Snapshot, now: float) -> float:
    """Observed output across the exact same canonical sessions as the graph."""
    return sum(observed_output_rate(row, now) or 0 for row in canonical_usages(snapshot, now).values())


@dataclass(frozen=True)
class RefreshPlan:
    """One hardware-aware frame decision."""

    mode: str
    fps: float
    jpeg_quality: int


class FrameGovernor:
    """Select phase-locked full-frame cadences for the configured display.

    The built-in Trofeo transport acknowledges complete JPEG frames and cannot
    accept regional updates. Use discrete rates to avoid uneven fractional
    cadences; the configured maximum can be raised for a custom display.
    """

    def __init__(self, config: dict[str, Any], max_fps: float) -> None:
        self.enabled = bool(config.get("adaptive_refresh", True))
        configured_max = max(1.0, min(240.0, float(config.get("max_fps", max_fps))))
        self.max_fps = max(1.0, min(configured_max, max_fps))
        self.burst_fps = self._rate(config.get("burst_fps", 30), self.max_fps)
        self.active_fps = self._rate(config.get("active_fps", 30), self.burst_fps)
        # This controller visibly resets/flickers when complete-frame keepalives
        # become sparse. Hold every quiet mode on a continuous 30 FPS cadence and
        # save work in data polling and optional effects instead.
        self.idle_fps = self._rate(config.get("idle_fps", 30), self.active_fps)
        self.sleep_fps = self._rate(config.get("sleep_fps", 30), self.idle_fps)
        self.burst_seconds = max(1.0, float(config.get("burst_seconds", 6)))
        self.burst_rearm = max(2.0, float(config.get("burst_rearm_seconds", 8)))
        self.active_window = max(3.0, float(config.get("active_window_seconds", 20)))
        self.sleep_after = max(
            self.active_window + 10,
            float(config.get("sleep_after_seconds", 180)),
        )
        self.headroom = max(0.55, min(0.95, float(config.get("frame_headroom", 0.82))))
        base_quality = int(config.get("jpeg_quality", 34))
        self.quality = {
            "burst": int(config.get("jpeg_quality_burst", 28)),
            "active": int(config.get("jpeg_quality_active", 34)),
            "idle": int(config.get("jpeg_quality_idle", 34)),
            "rest": int(config.get("jpeg_quality_idle", 34)),
            "fixed": base_quality,
        }
        self.started_at = time.monotonic()
        self.last_activity_at = 0.0
        self.burst_until = 0.0
        self.penalty_until = 0.0
        self.work_ema = 0.0
        self.usb_ema = 0.0
        self.last_plan = RefreshPlan("idle", self.idle_fps, self._quality("idle"))

    @staticmethod
    def _rate(value: Any, ceiling: float) -> float:
        return max(MIN_PANEL_FPS, min(float(value), ceiling))

    @staticmethod
    def _activity_at(snapshot: Snapshot) -> float:
        timestamps = [event.timestamp for event in snapshot.events if event.timestamp > 0]
        timestamps.extend(
            record.timestamp for record in snapshot.otel_records if record.timestamp > 0
        )
        timestamps.extend(
            usage.updated_at for usage in snapshot.live_usages if usage.updated_at > 0
        )
        if snapshot.live_usage is not None and snapshot.live_usage.updated_at > 0:
            timestamps.append(snapshot.live_usage.updated_at)
        return max(timestamps, default=0.0)

    def _quality(self, mode: str) -> int:
        return max(10, min(95, self.quality[mode]))

    def select(self, snapshot: Snapshot, wall_now: float, mono_now: float) -> RefreshPlan:
        if not self.enabled:
            plan = RefreshPlan("fixed", self.max_fps, self._quality("fixed"))
            self.last_plan = plan
            return plan

        activity_at = self._activity_at(snapshot)
        if activity_at > self.last_activity_at:
            gap = activity_at - self.last_activity_at if self.last_activity_at else math.inf
            is_fresh = wall_now - activity_at <= 2.5
            if is_fresh and gap >= self.burst_rearm:
                self.burst_until = mono_now + self.burst_seconds
            self.last_activity_at = activity_at

        if self.last_activity_at:
            quiet_for = max(0.0, wall_now - self.last_activity_at)
        else:
            quiet_for = mono_now - self.started_at

        has_live_flow = any(
            usage.token_rate > 0 and wall_now - usage.updated_at <= 8
            for usage in snapshot.live_usages
        )
        if (
            mono_now < self.burst_until
            and (quiet_for <= self.active_window or has_live_flow)
        ):
            mode, requested = "burst", self.burst_fps
        elif quiet_for <= self.active_window or has_live_flow:
            mode, requested = "active", self.active_fps
        elif quiet_for < self.sleep_after:
            mode, requested = "idle", self.idle_fps
        else:
            mode, requested = "rest", self.sleep_fps

        # Never derive the visible cadence from a continuously changing work
        # estimate. That produced rates such as 12.7 or 18.2 FPS and caused
        # obvious beat/flicker on this fixed-refresh strip. Missed deadlines are
        # naturally skipped by the scheduler; the next frame stays on the same
        # 30/24 Hz phase grid.
        plan = RefreshPlan(mode, requested, self._quality(mode))
        self.last_plan = plan
        return plan

    def observe(self, work_seconds: float, usb_seconds: float, now: float, failed: bool = False) -> None:
        alpha = 0.14
        self.work_ema = (
            work_seconds if self.work_ema <= 0
            else self.work_ema * (1 - alpha) + work_seconds * alpha
        )
        self.usb_ema = (
            usb_seconds if self.usb_ema <= 0
            else self.usb_ema * (1 - alpha) + usb_seconds * alpha
        )
        if failed:
            self.penalty_until = now + 8.0
            self.burst_until = 0.0


class AgentLog:
    """Keep a quiet, chronological preview of real agent and tool activity."""

    def __init__(self) -> None:
        self.entries: deque[Event] = deque(maxlen=24)
        self.seen: deque[tuple[float, str, str, str]] = deque(maxlen=96)

    def update(self, events: list[Event]) -> None:
        known = set(self.seen)
        for event in sorted(events, key=lambda item: item.timestamp):
            key = (event.timestamp, event.source, event.message, event.level)
            if key in known:
                continue
            self.entries.append(event)
            self.seen.append(key)
            known.add(key)

    @staticmethod
    def classify(event: Event) -> tuple[str, str]:
        message = event.message.upper()
        if event.level == "ERROR":
            return "ERROR", RED
        if event.level == "WARN":
            return "WARN", AMBER
        if "TOOL" in message:
            return "TOOL", CYAN
        if "DONE" in message or "COMPLETE" in message:
            return "DONE", GREEN
        if "REASON" in message or "THINK" in message:
            return "THINK", PURPLE
        if "TOKEN" in message:
            return "TOKEN", MAGENTA
        if "TURN" in message or "PROMPT" in message:
            return "TURN", ORANGE
        return "EVENT", BLUE

    @staticmethod
    def source_label(source: str) -> str:
        normalized = source.strip().upper()
        aliases = {
            "THIS MAC": "MAC",
            "AI-DESKTOP": "DESK",
            "AI-LAPTOP": "LAP",
        }
        return aliases.get(normalized, normalized.replace(" ", "")[:5] or "LOCAL")

    @classmethod
    def line_parts(cls, event: Event) -> tuple[str, str]:
        agent = cls.source_label(event.source)
        message = event.message.strip()
        known_agents = {
            "CODEX", "CLAUDE", "SUKI", "SCOUT", "WATCHER", "CURSOR",
            "GEMINI", "DEEPSEEK", "MUSE", "OPENAI",
        }
        head, separator, remainder = message.partition(" / ")
        if separator and head.upper() in known_agents:
            agent = head.upper()
            message = remainder
        return agent, message


@dataclass
class OtelLine:
    timestamp: float
    agent: str
    span: str
    duration_ms: int | None
    status: str
    color: str
    visual: bool = False


class OtelBurnTape:
    """Turn observed OTLP records into an event-gated, panel-speed trace bus.

    Actual OTLP records are retained exactly once in ``lines``. While real
    activity is fresh, the presentation layer fills the gaps with lightweight
    child-stage lines derived from the newest observed record. Those visual
    microspans never leave the renderer and stop when the agent goes quiet.
    """

    MICRO_STAGES = {
        "tool": ("tool.dispatch", "tool.stream", "tool.await", "tool.decode"),
        "token": ("token.decode", "usage.account", "context.sample", "meter.flush"),
        "reasoning": ("reasoning.delta", "model.decode", "context.attend", "trace.link"),
        "response": ("response.delta", "stream.encode", "event.emit", "span.flush"),
        "turn": ("turn.route", "agent.schedule", "context.load", "trace.link"),
        "default": ("otel.recv", "span.decode", "trace.link", "collector.emit"),
    }
    ACTIVITY_HOLD_SECONDS = 30.0

    def __init__(self, _legacy_hz: int = 60) -> None:
        # The panel tops out at 30 FPS. Generating rows faster only wastes CPU
        # and USB bandwidth because those intermediate frames cannot be shown.
        self.hz = max(1, min(30, int(_legacy_hz)))
        self.lines: deque[OtelLine] = deque(maxlen=180)
        self.burn: deque[OtelLine] = deque(maxlen=180)
        self.seen_order: deque[tuple[str, str, str, float, str]] = deque(maxlen=1440)
        self.seen: set[tuple[str, str, str, float, str]] = set()
        self.latest_at = 0.0
        self.received = 0
        self.last_visual_tick = 0.0
        self.sequence = 0
        self.active_hint_until = 0.0
        self.last_observed_at = 0.0

    @staticmethod
    def _agent(record: OtelRecord) -> str:
        value = (record.model or record.service or "OTEL").upper()
        for label in ("ASTRA", "LUNA", "TERRA", "SOL", "CLAUDE", "GEMINI", "MUSE", "CODEX"):
            if label in value:
                return label[:6]
        return value.replace("GPT-", "GPT").replace(" ", "")[:6]

    @staticmethod
    def _color(record: OtelRecord) -> str:
        if record.status == "ERR":
            return RED
        if record.status == "WRN":
            return AMBER
        name = record.name.upper()
        if "TOOL" in name:
            return CYAN
        if "COMPLETE" in name or "DONE" in name:
            return GREEN
        if "TOKEN" in name or "MODEL" in name:
            return MAGENTA
        return PURPLE if record.kind == "SPAN" else BLUE

    @staticmethod
    def _family(span: str) -> str:
        normalized = span.lower()
        for family in ("tool", "token", "reasoning", "response", "turn"):
            if family in normalized:
                return family
        return "default"

    def _micro_line(self, timestamp: float, roots: tuple[OtelLine, ...]) -> OtelLine:
        self.sequence += 1
        stage_group = self.MICRO_STAGES[self._family(roots[-1].span)]
        stage = stage_group[self.sequence % len(stage_group)]
        root_count = min(8, len(roots))
        root = roots[-1 - ((self.sequence // len(stage_group)) % root_count)]
        color = CYAN if self.sequence % 5 else PURPLE
        if "token" in stage or "model" in stage:
            color = MAGENTA
        elif "emit" in stage or "flush" in stage:
            color = GREEN
        return OtelLine(timestamp, root.agent, stage, 0, "FLOW", color, visual=True)

    def active(self, now: float) -> bool:
        observed = self.latest_at > 0 and 0 <= now - self.latest_at < self.ACTIVITY_HOLD_SECONDS
        return bool(self.lines) and (observed or now < self.active_hint_until)

    def update(
        self, records: tuple[OtelRecord, ...], now: float, active: bool = False,
    ) -> None:
        if active:
            self.active_hint_until = max(self.active_hint_until, now + 2.0)
        additions: list[OtelLine] = []
        for record in sorted(records, key=lambda item: item.timestamp):
            key = record.identity
            if key in self.seen:
                continue
            if len(self.seen_order) == self.seen_order.maxlen:
                self.seen.discard(self.seen_order[0])
            self.seen_order.append(key)
            self.seen.add(key)
            span = re.sub(r"[^a-zA-Z0-9_.-]+", "_", record.name).strip("._")[:34]
            line = OtelLine(
                record.timestamp, self._agent(record), span or "otel.record",
                record.duration_ms, record.status, self._color(record),
            )
            self.lines.append(line)
            additions.append(line)
            self.latest_at = max(self.latest_at, record.timestamp)
            self.received += 1
        if not additions:
            return
        self.last_observed_at = now
        if not self.burn:
            # Start at the live edge instead of replaying an hour of history.
            self.burn.extend(list(self.lines)[-17:])
            self.last_visual_tick = now
        else:
            # Every observed root appears once. Display-only microspans fill
            # time between roots, rather than delaying a real event in a queue.
            self.burn.extend(additions)

    def observed_rows(self, now: float, count: int = 8, advance: bool = True) -> tuple[list[OtelLine], float]:
        """Scroll on actual arrivals; never manufacture log records or timings."""
        rows = list(self.lines)
        progress = max(0.0, min(1.0, (now - self.last_observed_at) / .24))
        if advance and len(rows) > count and progress < 1:
            return rows[-(count + 1):], progress
        return rows[-count:], 0.0

    def visual_rows(
        self, now: float, count: int = 16, advance: bool = True,
    ) -> tuple[list[OtelLine], float]:
        """Return a continuously scrolling, panel-synchronized trace window."""
        if not self.burn and self.lines:
            self.burn.extend(list(self.lines)[-(count + 1):])
            self.last_visual_tick = now
        streaming = advance and self.active(now)
        if not streaming:
            self.last_visual_tick = now
            return list(self.burn)[-count:], 0.0
        if self.last_visual_tick <= 0:
            self.last_visual_tick = now
        elapsed = max(0.0, now - self.last_visual_tick)
        steps = min(self.hz * 2, int(elapsed * self.hz))
        roots = tuple(self.lines)
        for _ in range(steps):
            self.last_visual_tick += 1 / self.hz
            self.burn.append(self._micro_line(self.last_visual_tick, roots))
        progress = max(0.0, min(1.0, (now - self.last_visual_tick) * self.hz))
        rows = list(self.burn)[-(count + 1):]
        return rows if len(rows) > count else rows[-count:], progress


class Trofeo:
    """PyUSB sender for the reverse-engineered 0416:5408 LY panel protocol."""

    def __init__(self) -> None:
        self.device: Any = None
        self.ep_out = 0x09
        self.ep_in = 0x81
        self.sub = 0
        self.pm = 0
        self.frame_buffer = bytearray()
        self.zeroes = bytes(WRITE_BURST)

    def connect(self) -> None:
        self.close()
        if usb_util is None or usb_find is None:
            raise RuntimeError(
                "PyUSB support is not installed; install usagemax-display[usb]"
            )
        device = usb_find(idVendor=VID, idProduct=PID)
        if device is None:
            raise RuntimeError("Trofeo 0416:5408 not found")
        device.set_configuration()
        usb_util.claim_interface(device, 0)
        interface = device.get_active_configuration()[(0, 0)]
        outs = [
            ep.bEndpointAddress for ep in interface
            if usb_util.endpoint_direction(ep.bEndpointAddress) == usb_util.ENDPOINT_OUT
        ]
        ins = [
            ep.bEndpointAddress for ep in interface
            if usb_util.endpoint_direction(ep.bEndpointAddress) == usb_util.ENDPOINT_IN
        ]
        if outs:
            self.ep_out = outs[0]
        if ins:
            self.ep_in = ins[0]

        # Discard a stale acknowledgement before starting a fresh LY session.
        try:
            device.read(self.ep_in, 64, timeout=100)
        except Exception:
            pass

        request = bytearray(2048)
        request[0] = 0x02
        request[1] = 0xFF
        request[8] = 0x01
        device.write(self.ep_out, request, timeout=2000)
        response = bytes(device.read(self.ep_in, 512, timeout=2000))
        if (
            len(response) < 37
            or response[0] != 0x03
            or response[1] != 0xFF
            or response[8] != 0x01
        ):
            raise RuntimeError(f"Trofeo handshake rejected: {response[:12].hex()}")
        self.pm = 64 + max(1, response[20])
        self.sub = response[22] + 1
        self.device = device

    def send(self, jpeg: bytes) -> None:
        if self.device is None:
            self.connect()
        assert self.device is not None
        # Match oae/sensorpanel's LY framing exactly.  The protocol allocates
        # one extra logical chunk for an exact multiple, then pads the wire
        # transfer to four chunks; the header advertises logical chunks only.
        chunks = len(jpeg) // CHUNK_DATA_SIZE + 1
        padded_chunks = math.ceil(chunks / 4) * 4
        frame_size = padded_chunks * CHUNK_SIZE
        if len(self.frame_buffer) < frame_size:
            self.frame_buffer.extend(bytes(frame_size - len(self.frame_buffer)))
        frame = self.frame_buffer
        for index in range(chunks):
            offset = index * CHUNK_SIZE
            part = jpeg[index * CHUNK_DATA_SIZE:(index + 1) * CHUNK_DATA_SIZE]
            frame[offset] = 0x01
            frame[offset + 1] = 0xFF
            frame[offset + 2:offset + 6] = len(jpeg).to_bytes(4, "little")
            frame[offset + 6:offset + 8] = len(part).to_bytes(2, "little")
            frame[offset + 8] = 0x01
            frame[offset + 9:offset + 11] = chunks.to_bytes(2, "little")
            frame[offset + 11:offset + 13] = index.to_bytes(2, "little")
            frame[offset + 13:offset + 16] = b"\x00\x00\x00"
            frame[offset + 16:offset + 16 + len(part)] = part
            if len(part) < CHUNK_DATA_SIZE:
                tail = CHUNK_DATA_SIZE - len(part)
                frame[offset + 16 + len(part):offset + CHUNK_SIZE] = self.zeroes[:tail]

        padding_start = chunks * CHUNK_SIZE
        while padding_start < frame_size:
            clear_size = min(WRITE_BURST, frame_size - padding_start)
            frame[padding_start:padding_start + clear_size] = self.zeroes[:clear_size]
            padding_start += clear_size

        position = 0
        while position < frame_size:
            remaining = frame_size - position
            write_size = min(WRITE_BURST, remaining)
            self.device.write(
                self.ep_out, frame[position:position + write_size], timeout=5000
            )
            position += write_size
        self.device.read(self.ep_in, 512, timeout=1500)

    def close(self) -> None:
        if self.device is not None:
            try:
                if usb_util is not None:
                    usb_util.release_interface(self.device, 0)
            except Exception:
                pass
            try:
                if usb_util is not None:
                    usb_util.dispose_resources(self.device)
            except Exception:
                pass
        self.device = None


class NativeTrofeo:
    """Length-prefixed bridge to the compiled Rust LY transport."""

    def __init__(self, executable: Path) -> None:
        self.executable = executable
        self.process: subprocess.Popen[bytes] | None = None
        self.device: Any = None
        self.pm = 0
        self.sub = 0
        self.mode = "NATIVE"
        self.last_usb_ms = 0.0

    def connect(self) -> None:
        self.close()
        if not self.executable.is_file():
            raise RuntimeError(f"native transport missing: {self.executable}")
        process = subprocess.Popen(
            [str(self.executable)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0,
        )
        assert process.stdout is not None
        ready = process.stdout.readline().decode("utf-8", "replace").strip()
        if not ready.startswith("READY "):
            process.terminate()
            raise RuntimeError(ready or "native transport did not become ready")
        fields = ready.split()
        if len(fields) != 3:
            process.terminate()
            raise RuntimeError(f"invalid native transport greeting: {ready}")
        self.pm = int(fields[1])
        self.sub = int(fields[2])
        self.process = process
        self.device = process

    def send(self, jpeg: bytes) -> None:
        if self.process is None or self.process.poll() is not None:
            self.connect()
        assert self.process is not None and self.process.stdin is not None
        assert self.process.stdout is not None
        try:
            self.process.stdin.write(len(jpeg).to_bytes(4, "little"))
            self.process.stdin.write(jpeg)
            self.process.stdin.flush()
            response = self.process.stdout.readline().decode("utf-8", "replace").strip()
        except (BrokenPipeError, OSError) as error:
            raise RuntimeError(f"native transport pipe failed: {error}") from error
        if not response.startswith("OK "):
            raise RuntimeError(response or "native transport exited during frame")
        try:
            self.last_usb_ms = int(response.split()[1]) / 1000
        except (IndexError, ValueError):
            self.last_usb_ms = 0.0

    def close(self) -> None:
        process = self.process
        self.process = None
        self.device = None
        if process is None:
            return
        try:
            if process.poll() is None and process.stdin is not None:
                process.stdin.write((0).to_bytes(4, "little"))
                process.stdin.flush()
                process.wait(timeout=1)
        except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
            try:
                process.terminate()
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass


class TrofeoTransport:
    """Prefer the optional native pump, with automatic recovery through PyUSB."""

    def __init__(self, native_path: Path, native_enabled: bool = True) -> None:
        self.native_path = native_path
        self.native_enabled = native_enabled
        self.native_failed = False
        self.active: NativeTrofeo | Trofeo | None = None
        self.mode = "OFFLINE"

    @property
    def device(self) -> Any:
        return self.active.device if self.active is not None else None

    @property
    def connected(self) -> bool:
        return self.device is not None

    @property
    def pm(self) -> int:
        return self.active.pm if self.active is not None else 0

    @property
    def sub(self) -> int:
        return self.active.sub if self.active is not None else 0

    @property
    def detail(self) -> str:
        return f"0416:5408 / PM{self.pm or 65} SUB{self.sub or 3}"

    @property
    def rotation(self) -> int:
        return 0 if self.sub in {2, 3, 4} else 180

    def connect(self) -> None:
        self.close()
        if self.native_enabled and not self.native_failed and self.native_path.is_file():
            native = NativeTrofeo(self.native_path)
            try:
                native.connect()
                self.active = native
                self.mode = native.mode
                return
            except Exception as error:
                self.native_failed = True
                native.close()
                print(f"native transport unavailable; using PyUSB: {error}")
        fallback = Trofeo()
        fallback.connect()
        self.active = fallback
        self.mode = "PYUSB"

    def send(self, jpeg: bytes) -> None:
        if self.active is None:
            self.connect()
        assert self.active is not None
        try:
            self.active.send(jpeg)
        except Exception:
            if not isinstance(self.active, NativeTrofeo):
                raise
            self.native_failed = True
            self.active.close()
            fallback = Trofeo()
            fallback.connect()
            self.active = fallback
            self.mode = "PYUSB"
            fallback.send(jpeg)

    def close(self) -> None:
        if self.active is not None:
            self.active.close()
        self.active = None
        self.mode = "OFFLINE"


class LogReader:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        self.local_session_sources = bool(config.get("local_session_sources", False))
        user_root = Path(os.environ.get("USERPROFILE", str(Path.home())))
        self.paths: dict[str, Path] = {}
        if self.local_session_sources:
            self.paths.update({
                "CODEX": user_root / ".codex" / "log" / "codex-tui.log",
                "CLAUDE": user_root / ".claude" / "daemon.log",
            })
        config_dir = Path(config.get("_config_dir", Path.cwd()))
        # External adapters publish the same small numeric/session contract.
        # The host label and input path are configuration, not a harness list.
        for source, value in config.get("telemetry_files", {}).items():
            self.paths[str(source)] = resolve_path(str(value), config_dir)
        self.tail_bytes = max(256_000, min(4_000_000, int(config.get("telemetry_tail_bytes", 1_000_000))))
        self.tail_lines = max(64, min(8192, int(config.get("telemetry_tail_lines", 2048))))
        self.events: list[Event] = []
        self.cached_paths = dict(self.paths)
        self.last_candidate_scan = 0.0
        self.cached_session_paths: list[tuple[str, Path]] = []
        self.last_session_scan = 0.0
        self.last_poll = time.monotonic()
        self.candidate_scan_seconds = max(10.0, float(config.get("source_scan_interval", 60)))
        self.session_scan_seconds = max(10.0, float(config.get("session_scan_interval", 30)))
        self.file_state: dict[str, tuple[str, int, int]] = {}
        self.tail_cache: dict[
            tuple[str, str],
            tuple[tuple[int, int], tuple[tuple[Event, ...], tuple[LiveUsage, ...], tuple[AgentRelation, ...]]],
        ] = {}
        self.flow: dict[str, float] = {}
        self.live_usage: dict[tuple[str, str, str], LiveUsage] = {}
        self.relations: dict[tuple[str, str, str], AgentRelation] = {}
        self.daily_usages: dict[tuple[str, str], DailyUsage] = {}

    def _candidate_files(self) -> dict[str, Path]:
        if time.monotonic() - self.last_candidate_scan < self.candidate_scan_seconds:
            return self.cached_paths
        result = dict(self.paths)
        if not self.local_session_sources:
            self.cached_paths = result
            self.last_candidate_scan = time.monotonic()
            return result
        user_root = Path(os.environ.get("USERPROFILE", str(Path.home())))
        for source, folder in (
            ("CODEX", user_root / ".codex" / "sessions"),
            ("CLAUDE", user_root / ".claude" / "projects"),
        ):
            try:
                files = [path for path in folder.rglob("*.jsonl") if path.is_file()]
                if files:
                    result[source] = max(files, key=lambda path: path.stat().st_mtime)
            except (OSError, PermissionError):
                pass
        self.cached_paths = result
        self.last_candidate_scan = time.monotonic()
        return result

    def _session_candidates(self) -> list[tuple[str, Path]]:
        if not self.local_session_sources:
            return []
        if time.monotonic() - self.last_session_scan < self.session_scan_seconds:
            return self.cached_session_paths
        user_root = Path(os.environ.get("USERPROFILE", str(Path.home())))
        cutoff = time.time() - 1200
        candidates: list[tuple[float, str, Path]] = []
        for source, folder in (
            ("CODEX", user_root / ".codex" / "sessions"),
            ("CLAUDE", user_root / ".claude" / "projects"),
        ):
            try:
                for path in folder.rglob("*.jsonl"):
                    if not path.is_file():
                        continue
                    modified = path.stat().st_mtime
                    if modified >= cutoff:
                        candidates.append((modified, source, path))
            except (OSError, PermissionError):
                continue
        candidates.sort(key=lambda item: item[0], reverse=True)
        self.cached_session_paths = [(source, path) for _, source, path in candidates[:512]]
        self.last_session_scan = time.monotonic()
        return self.cached_session_paths

    @staticmethod
    def _event_from_payload(source: str, payload: dict[str, Any]) -> tuple[str, str]:
        raw_level = str(payload.get("level", payload.get("severity", "INFO"))).upper()
        level = "WARN" if "WARN" in raw_level else "ERROR" if "ERROR" in raw_level else "INFO"
        if isinstance(payload.get("event"), str):
            return level, redact(str(payload["event"]))

        outer = str(payload.get("type", "")).lower()
        inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        inner_type = str(inner.get("type", "")).lower()
        if outer == "event_msg":
            if inner_type == "token_count":
                return "OK", "TOKENS / COUNTERS UPDATED"
            if inner_type == "item_completed":
                item = inner.get("item") if isinstance(inner.get("item"), dict) else {}
                item_type = str(item.get("type", "task")).replace("Execution", "").upper()
                return level, f"DONE / {item_type}"
            return level, f"EVENT / {inner_type.replace('_', ' ').upper() or 'ACTIVITY'}"
        if outer == "response_item":
            if inner_type in {"custom_tool_call", "function_call"}:
                name = redact(str(inner.get("name", "tool"))).upper()
                return level, f"TOOL / {name}"
            labels = {
                "reasoning": "REASONING / STREAM",
                "message": "RESPONSE / STREAM",
                "custom_tool_call_output": "TOOL / OUTPUT",
            }
            return level, labels.get(inner_type, f"RESPONSE / {inner_type.upper() or 'ITEM'}")
        if outer == "turn_context":
            model = str(payload.get("payload", {}).get("model", "MODEL"))
            return "OK", f"TURN / {model.upper()}"
        if outer in {"user", "human"}:
            return level, "PROMPT / RECEIVED"
        if outer == "assistant":
            return level, "RESPONSE / STREAM"
        if outer in {"progress", "system", "queue-operation"}:
            return level, f"{source} / {outer.replace('-', ' ').upper()}"
        if outer == "token_usage_record":
            return "OK", "TOKENS / SNAPSHOT"
        for key in ("event_name", "name", "method", "command", "status"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return level, redact(value).upper()
        return level, f"{source} / ACTIVITY"

    @staticmethod
    def _usage_from_payload(source: str, payload: dict[str, Any], fallback: float) -> LiveUsage | None:
        if payload.get("kind") == "usage":
            usage = LiveUsage(
                source=source,
                agent=str(payload.get("harness") or payload.get("agent") or "Unknown harness"),
                harness=str(payload.get("harness", "")),
                provider=str(payload.get("provider", "")),
                model=str(payload.get("model", "")),
                session_id=str(payload.get("session_id", "")),
                session_tokens=counter(payload.get("session_tokens", 0)),
                input_tokens=counter(payload.get("input_tokens", 0)),
                output_tokens=counter(payload.get("output_tokens", 0)),
                has_output_counter=isinstance(payload.get("output_tokens"), (int, float)),
                context_window=counter(payload.get("context_window", 0)),
                updated_at=parse_timestamp(payload.get("timestamp"), fallback),
                execution_state=str(payload.get("execution_state") or "unknown"),
            )
            # Adapters must name the output-only basis explicitly. A generic
            # token_rate may include input/context and is not generation speed.
            rate = payload.get("output_tps")
            if isinstance(rate, (int, float)) and not isinstance(rate, bool) and math.isfinite(rate) and rate >= 0:
                usage.token_rate, usage.rate_available = float(rate), True
            return usage
        inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        modern = payload.get("type") == "token_usage_record"
        if modern:
            info = {}
            total = inner.get("thread_token_usage") or {}
        elif payload.get("type") == "event_msg" and inner.get("type") == "token_count":
            info = inner.get("info") or {}
            total = info.get("total_token_usage") or {}
        else:
            return None
        if not total:
            return None
        return LiveUsage(
            source=socket.gethostname().upper() if source == "CODEX" else source,
            agent="CODEX", harness="CODEX",
            model=str(payload.get("_observed_model") or ""),
            session_id=str(inner.get("thread_id") or inner.get("session_id") or "") if modern else "",
            session_tokens=int(float(total.get("total_tokens", 0) or 0)),
            input_tokens=int(float(total.get("input_tokens", 0) or 0)),
            output_tokens=int(float(total.get("output_tokens", 0) or 0)),
            has_output_counter=isinstance(total.get("output_tokens"), (int, float)),
            context_window=int(float(info.get("model_context_window", 0) or 0)),
            updated_at=parse_timestamp(payload.get("timestamp"), fallback),
        )

    def _accept_usage(self, usage: LiveUsage) -> None:
        stream_id = usage.session_id.strip() or "primary"
        key = (usage.source.casefold(), (usage.harness or usage.agent).casefold(), stream_id)
        prior = self.live_usage.get(key)
        if prior is not None and usage.model == "":
            usage.model = prior.model
        if prior is not None and usage.updated_at == prior.updated_at and usage.execution_state in {"completed", "idle", "stopped", "failed"}:
            prior.execution_state = usage.execution_state
        if prior is not None and usage.updated_at <= prior.updated_at:
            # Polling the same source event must not mutate its measured rate.
            # Freshness is applied once, at display time.
            return
        if prior is not None and not usage.rate_available and prior.has_output_counter and usage.has_output_counter and prior.model == usage.model:
            rate = output_rate((prior.updated_at, prior.output_tokens), (usage.updated_at, usage.output_tokens))
            if rate is not None:
                usage.token_rate, usage.rate_available = rate, True
        self.live_usage[key] = usage

    @staticmethod
    def _relation_from_payload(
        source: str, payload: dict[str, Any], fallback: float,
    ) -> AgentRelation | None:
        if payload.get("kind") != "topology":
            return None
        child_id = str(payload.get("child_id", "")).strip()
        parent_id = str(payload.get("parent_id", "")).strip()
        if not child_id or not parent_id:
            return None
        return AgentRelation(
            parent_id=parent_id,
            child_id=child_id,
            parent_name=str(payload.get("parent_name", "Agent")),
            child_name=str(payload.get("child_name", "Agent")),
            parent_model=str(payload.get("parent_model", "")),
            child_model=str(payload.get("child_model", "")),
            status=str(payload.get("status", "OPEN")).upper(),
            observed_at=parse_timestamp(payload.get("timestamp"), 0),
            source=source,
            parent_updated_at=parse_timestamp(payload.get("parent_updated_at"), fallback),
            child_updated_at=parse_timestamp(payload.get("child_updated_at"), fallback),
            harness=str(payload.get("harness", "")),
            parent_harness=str(payload.get("parent_harness", "")),
            child_harness=str(payload.get("child_harness", "")),
            parent_provider=str(payload.get("parent_provider", "")),
            child_provider=str(payload.get("child_provider", "")),
        )

    def _read_tail(
        self, source: str, path: Path,
    ) -> tuple[list[Event], list[LiveUsage], list[AgentRelation]]:
        if not path.exists():
            return [], [], []
        try:
            stat = path.stat()
            cache_key = (source, str(path))
            fingerprint = (stat.st_size, stat.st_mtime_ns)
            cached = self.tail_cache.get(cache_key)
            if cached is not None and cached[0] == fingerprint:
                events, usages, relations = cached[1]
                return list(events), [replace(usage) for usage in usages], list(relations)
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - self.tail_bytes))
                raw = handle.read().decode("utf-8", "replace")
            lines = [line for line in raw.splitlines() if line.strip()][-self.tail_lines:]
            mtime = stat.st_mtime
        except (OSError, PermissionError):
            return [], [], []
        events: list[Event] = []
        usages: dict[tuple[str, str], LiveUsage] = {}
        relations: list[AgentRelation] = []
        latest_model = ""
        parsed: list[tuple[dict[str, Any], float]] = []
        for line in lines:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                clean = redact(line)
                match = re.search(r"\b(ERROR|WARN(?:ING)?|INFO|OK)\b[:\s-]*(.*)", clean, re.I)
                level = "WARN" if match and match.group(1).upper().startswith("WARN") else match.group(1).upper() if match else "INFO"
                message = match.group(2) if match else clean
                events.append(Event(mtime, source, message, level))
                continue
            if not isinstance(payload, dict):
                continue
            timestamp = parse_timestamp(payload.get("timestamp"), mtime)
            parsed.append((payload, timestamp))
            inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
            if payload.get("type") == "turn_context" and isinstance(inner.get("model"), str):
                latest_model = str(inner["model"])
            payload["_observed_model"] = latest_model

        has_modern = any(p.get("type") == "token_usage_record" for p, _ in parsed)
        for payload, timestamp in parsed:
            inner = payload.get("payload") or {}
            if payload.get("type") == "event_msg" and inner.get("type") in {"task_complete", "turn_aborted"}:
                for usage in usages.values():
                    usage.execution_state = "completed" if inner["type"] == "task_complete" else "stopped"
            if has_modern and payload.get("type") == "event_msg" and (payload.get("payload") or {}).get("type") == "token_count":
                continue
            if payload.get("kind") == "daily_usage":
                try:
                    daily = DailyUsage.parse(source, payload)
                    key = (source.casefold(), daily.harness.casefold())
                    prior = self.daily_usages.get(key)
                    if prior is None or daily.updated_at > prior.updated_at:
                        self.daily_usages[key] = daily
                except (KeyError, ValueError, TypeError):
                    pass
                continue
            relation = self._relation_from_payload(source, payload, mtime)
            if relation is not None:
                relations.append(relation)
                continue
            try:
                candidate = self._usage_from_payload(source, payload, mtime)
            except (ValueError, TypeError, OverflowError):
                continue
            if candidate is not None:
                key = ((candidate.harness or candidate.agent).casefold(), candidate.session_id.strip() or "primary")
                prior = usages.get(key)
                if prior is not None and candidate.updated_at <= prior.updated_at:
                    continue
                if prior is not None and not candidate.rate_available and prior.has_output_counter and candidate.has_output_counter and prior.model == candidate.model:
                    rate = output_rate((prior.updated_at, prior.output_tokens), (candidate.updated_at, candidate.output_tokens))
                    if rate is not None:
                        candidate.token_rate, candidate.rate_available = rate, True
                if prior is None or candidate.updated_at >= prior.updated_at:
                    usages[key] = candidate
            level, message = self._event_from_payload(source, payload)
            events.append(Event(timestamp, source, message, level))
        result = (tuple(events[-12:]), tuple(usages.values()), tuple(relations))
        self.tail_cache[cache_key] = (fingerprint, result)
        if len(self.tail_cache) > 96:
            oldest = next(iter(self.tail_cache))
            self.tail_cache.pop(oldest, None)
        return list(result[0]), [replace(usage) for usage in result[1]], list(result[2])

    def poll(
        self,
    ) -> tuple[
        list[Event], dict[str, float | None], dict[str, float],
        dict[str, LiveUsage], tuple[LiveUsage, ...], tuple[AgentRelation, ...],
    ]:
        found = self._candidate_files()
        recent: list[Event] = []
        timestamps: dict[str, float | None] = {}
        now_mono = time.monotonic()
        elapsed = max(0.1, now_mono - self.last_poll)
        self.last_poll = now_mono
        for source, path in found.items():
            try:
                stat = path.stat() if path.exists() else None
                timestamps[source] = stat.st_mtime if stat else None
            except OSError:
                stat = None
                timestamps[source] = None
            if stat is not None:
                current = (str(path), stat.st_size, stat.st_mtime_ns)
                prior = self.file_state.get(source)
                fresh_bytes = 0
                if prior is not None and current[0] == prior[0]:
                    fresh_bytes = max(0, current[1] - prior[1])
                    if current[2] != prior[2] and fresh_bytes == 0:
                        fresh_bytes = min(current[1], 4096)
                self.file_state[source] = current
                instantaneous = fresh_bytes / 1024 / elapsed
                self.flow[source] = self.flow.get(source, 0) * 0.64 + instantaneous * 0.36
            else:
                self.flow[source] = self.flow.get(source, 0) * 0.64
            file_events, usages, relations = self._read_tail(source, path)
            recent.extend(file_events)
            for relation in relations:
                self.relations[(relation.source.casefold(), (relation.child_harness or relation.harness).casefold(), relation.child_id)] = relation
            for usage in usages:
                if not usage.session_id and source in {"CODEX", "CLAUDE"}:
                    usage.session_id = path.stem
                self._accept_usage(usage)
        primary_paths = {str(path) for path in found.values()}
        for source, path in self._session_candidates():
            if str(path) in primary_paths:
                continue
            _, usages, _ = self._read_tail(source, path)
            for usage in usages:
                if not usage.session_id:
                    usage.session_id = path.stem
                self._accept_usage(usage)
        cutoff = time.time() - 1200
        self.live_usage = {
            key: usage for key, usage in self.live_usage.items()
            if usage.updated_at >= cutoff
        }
        dedup: dict[tuple[str, str], Event] = {}
        for event in recent:
            dedup[(event.source, event.message)] = event
        self.events = sorted(dedup.values(), key=lambda event: event.timestamp)[-12:]
        latest_by_source: dict[str, LiveUsage] = {}
        for usage in self.live_usage.values():
            prior = latest_by_source.get(usage.source)
            if prior is None or usage.updated_at > prior.updated_at:
                latest_by_source[usage.source] = usage
        sessions = tuple(sorted(
            self.live_usage.values(), key=lambda usage: usage.updated_at, reverse=True
        ))
        relation_cutoff = time.time() - 3600
        self.relations = {
            key: relation for key, relation in self.relations.items()
            if relation.status == "OPEN" or relation.updated_at >= relation_cutoff
        }
        relations = tuple(sorted(
            self.relations.values(),
            key=lambda relation: (relation.status == "OPEN", relation.updated_at),
            reverse=True,
        ))
        return self.events, timestamps, dict(self.flow), latest_by_source, sessions, relations


class Collector:
    def __init__(self, config: dict[str, Any]) -> None:
        self.reader = LogReader(config)
        self.config = config
        root = Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".codex" / "sessions"
        self.local_day = (
            DayLedgerWorker(root, socket.gethostname().upper())
            if bool(config.get("local_session_sources", False)) and root.is_dir()
            else None
        )
        self.usagemax = UsageMaxClient(config)
        otel_files = config.get(
            "otel_files", ["otel/*.otlp.jsonl", "remote-events/*.otlp.jsonl"]
        )
        if isinstance(otel_files, str):
            otel_files = [otel_files]
        runtime_dir = Path(config.get("runtime_dir", default_data_dir())).expanduser()
        otel_patterns = []
        for value in otel_files:
            otel_patterns.append(str(resolve_path(str(value), runtime_dir)))
        self.otel = OtelFileIngestor(otel_patterns)
        self.otel_receiver: OtlpHttpReceiver | None = None
        self.otel_receiver_version = -1
        self.cached_otel_records: tuple[OtelRecord, ...] = ()
        self.last_otel_file_scan = 0.0
        self.otel_file_scan_interval = max(1.0, float(config.get("otel_file_scan_interval", 10)))
        if bool(config.get("otel_http_enabled", False)):
            output = resolve_path(
                str(config.get("otel_http_file", "otel/http.otlp.jsonl")),
                runtime_dir,
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            self.otel_receiver = OtlpHttpReceiver(
                output,
                str(config.get("otel_http_bind", "127.0.0.1")),
                int(config.get("otel_http_port", 4318)),
            )
        self.last_remote_probe = 0.0
        self.remote_probe_interval = max(30.0, float(config.get("remote_probe_interval", 120)))
        self.remote_probe_running = False
        self.remote_status: dict[str, bool] = {}
        self.wsl_value = "unknown"
        self.last_wsl_probe = 0.0
        self.wsl_probe_interval = max(15.0, float(config.get("wsl_probe_interval", 60)))
        self.nvml_ready = False
        if pynvml is not None:
            try:
                pynvml.nvmlInit()
                self.nvml_ready = True
            except Exception:
                pass

    def live_otel_records(self, scan_files: bool = False) -> tuple[OtelRecord, ...]:
        """Merge live HTTP records without rescanning OTLP files every frame."""
        now_mono = time.monotonic()
        should_scan_files = (
            scan_files
            and now_mono - self.last_otel_file_scan >= self.otel_file_scan_interval
        )
        if should_scan_files:
            self.last_otel_file_scan = now_mono
        file_records = self.otel.scan() if should_scan_files else tuple(self.otel.records)
        receiver_records: tuple[OtelRecord, ...] = ()
        receiver_version = self.otel_receiver_version
        if self.otel_receiver is not None:
            receiver_version, receiver_records = self.otel_receiver.snapshot()
        if (
            not should_scan_files
            and receiver_version == self.otel_receiver_version
            and self.cached_otel_records
        ):
            return self.cached_otel_records
        merged = {
            record.identity: record for record in (*file_records, *receiver_records)
        }
        cutoff = time.time() - 3600
        self.cached_otel_records = tuple(sorted(
            (record for record in merged.values() if record.timestamp >= cutoff),
            key=lambda record: record.timestamp,
        )[-4096:])
        self.otel_receiver_version = receiver_version
        return self.cached_otel_records

    def _remote_probe(self) -> None:
        now = time.time()
        if now - self.last_remote_probe < self.remote_probe_interval or self.remote_probe_running:
            return
        self.last_remote_probe = now
        self.remote_probe_running = True
        threading.Thread(target=self._run_remote_probe, name="remote-probe", daemon=True).start()

    def _run_remote_probe(self) -> None:
        for item in self.config.get("remote_sources", []):
            name = str(item.get("name", "REMOTE")).upper()
            host = str(item.get("host", ""))
            user = str(item.get("user", ""))
            if not host:
                self.remote_status[name] = False
                continue
            target = f"{user}@{host}" if user else host
            try:
                result = subprocess.run(
                    ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=2",
                     "-o", "ConnectionAttempts=1", target, "echo", "ok"],
                    capture_output=True, text=True, timeout=4,
                )
                self.remote_status[name] = result.returncode == 0
            except (OSError, subprocess.SubprocessError):
                self.remote_status[name] = False
        self.remote_probe_running = False

    def _stats(self) -> SystemStats:
        stats = SystemStats(host_name=socket.gethostname().upper())
        if psutil is not None:
            try:
                stats.cpu = psutil.cpu_percent(interval=None)
                memory = psutil.virtual_memory()
                stats.ram = memory.percent
                stats.ram_used = memory.used / (1024**3)
                stats.ram_total = memory.total / (1024**3)
            except Exception:
                pass
        if self.nvml_ready:
            try:
                gpu = pynvml.nvmlDeviceGetHandleByIndex(0)
                util = pynvml.nvmlDeviceGetUtilizationRates(gpu)
                memory = pynvml.nvmlDeviceGetMemoryInfo(gpu)
                stats.gpu = float(util.gpu)
                stats.vram_used = memory.used / (1024**3)
                stats.vram_total = memory.total / (1024**3)
                stats.gpu_temp = float(pynvml.nvmlDeviceGetTemperature(
                    gpu, pynvml.NVML_TEMPERATURE_GPU
                ))
            except Exception:
                pass
        if os.name == "nt" and time.monotonic() - self.last_wsl_probe > self.wsl_probe_interval:
            self.last_wsl_probe = time.monotonic()
            try:
                output = subprocess.run(
                    ["wsl.exe", "-l", "--running"],
                    capture_output=True, text=True, timeout=2,
                ).stdout.replace("\x00", "")
                self.wsl_value = "RUNNING" if "Ubuntu" in output else "IDLE"
            except (OSError, subprocess.SubprocessError):
                self.wsl_value = "unknown"
        stats.wsl = self.wsl_value
        return stats

    def snapshot(self, usb_status: str, usb_detail: str, fps: float) -> Snapshot:
        events, timestamps, flow, live_by_source, live_sessions, relations = self.reader.poll()
        otel_records = self.live_otel_records(scan_files=True)
        self._remote_probe()
        now = time.time()
        stats = self._stats()
        codex_time = timestamps.get("CODEX")
        claude_time = timestamps.get("CLAUDE")
        local_live = live_by_source.get("CODEX")
        agents = [
            Agent(
                "CODEX",
                "ACTIVE" if codex_time and now - codex_time < 120 else "IDLE",
                local_live.model if local_live and local_live.model else "local structured event stream",
                sum(event.source == "CODEX" for event in events),
                sum(event.source == "CODEX" and event.level == "ERROR" for event in events),
                codex_time,
                source="CODEX",
                model=local_live.model if local_live else "",
                token_rate=local_live.token_rate if local_live else 0,
                session_tokens=local_live.session_tokens if local_live else 0,
                flow_kbps=flow.get("CODEX", 0),
            ),
            Agent(
                "CLAUDE",
                "ACTIVE" if claude_time and now - claude_time < 120 else "IDLE",
                "tailing daemon + project events",
                sum(event.source == "CLAUDE" for event in events),
                sum(event.source == "CLAUDE" and event.level == "ERROR" for event in events),
                claude_time,
                flow_kbps=flow.get("CLAUDE", 0),
            ),
            Agent(
                "WSL / UBUNTU",
                "RUNNING" if stats.wsl == "RUNNING" else "IDLE",
                "local Linux runtime",
                0,
                0,
                now if stats.wsl == "RUNNING" else None,
            ),
        ]
        for item in self.config.get("remote_sources", []):
            name = str(item.get("name", "REMOTE")).upper()
            relay_time = timestamps.get(name)
            relay_fresh = relay_time is not None and now - relay_time < 15
            online = self.remote_status.get(name, False) or relay_fresh
            if relay_fresh:
                relay_live = live_by_source.get(name)
                detail = relay_live.model if relay_live and relay_live.model else "encrypted event relay"
            elif online:
                relay_live = live_by_source.get(name)
                detail = "SSH source reachable"
            else:
                relay_live = live_by_source.get(name)
                detail = "not reachable"
            agents.append(Agent(
                name, "RUNNING" if online else "OFFLINE", detail,
                sum(event.source == name for event in events),
                sum(event.source == name and event.level == "ERROR" for event in events),
                relay_time if relay_fresh else now if online else None, name,
                model=relay_live.model if relay_live else "",
                token_rate=relay_live.token_rate if relay_live else 0,
                session_tokens=relay_live.session_tokens if relay_live else 0,
                flow_kbps=flow.get(name, 0),
            ))
        live_candidates = [
            usage for usage in live_by_source.values()
            if usage.session_tokens > 0 and now - usage.updated_at < 180
        ]
        live_usage = max(live_candidates, key=lambda usage: usage.updated_at, default=None)
        local_sessions = {
            (usage.source.casefold(), (usage.harness or usage.agent).casefold(), usage.session_id or usage.model): usage
            for usage in live_sessions
            if usage.session_tokens > 0
        }
        model_tokens: dict[str, int] = {}
        for usage in local_sessions.values():
            model = usage.model or "unknown"
            model_tokens[model] = model_tokens.get(model, 0) + usage.session_tokens
        local_total = sum(usage.session_tokens for usage in local_sessions.values())
        top_model = max(model_tokens, key=model_tokens.get, default="—")
        local_stats = TokenStats(
            login="local",
            total_tokens=local_total,
            sessions=len(local_sessions),
            active_days=1 if local_total else 0,
            top_model=top_model,
            models=tuple(sorted(model_tokens.items(), key=lambda item: item[1], reverse=True)[:4]),
            updated_at=max((usage.updated_at for usage in local_sessions.values()), default=0),
            status="LOCAL",
        )
        return Snapshot(
            agents, events, stats, usb_status, usb_detail, fps,
            local_stats, live_usage, sum(flow.values()), live_sessions,
            otel_records, relations, self.usagemax.snapshot(),
            tuple(self.reader.daily_usages.values()) + ((DailyUsage.parse(socket.gethostname().upper(), self.local_day.value),) if self.local_day and self.local_day.value else ()),
        )

    def close(self) -> None:
        self.usagemax.close()
        if self.local_day is not None:
            self.local_day.close()
        if self.otel_receiver is not None:
            self.otel_receiver.close()
        if self.nvml_ready:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass


PET_ROWS: dict[str, tuple[int, tuple[int, ...]]] = {
    "idle": (0, (280, 110, 110, 140, 140, 320)),
    "running-right": (1, (120, 120, 120, 120, 120, 120, 120, 220)),
    "running-left": (2, (120, 120, 120, 120, 120, 120, 120, 220)),
    "waving": (3, (140, 140, 140, 280)),
    "jumping": (4, (140, 140, 140, 140, 280)),
    "failed": (5, (140, 140, 140, 140, 140, 140, 140, 240)),
    "waiting": (6, (150, 150, 150, 150, 150, 260)),
    "running": (7, (120, 120, 120, 120, 120, 220)),
    "review": (8, (150, 150, 150, 150, 150, 280)),
}


class PetAnimator:
    """Read optional sprite sheets without altering their source art."""

    SPEED_SCALE = {
        "idle": 4.0,
        "waiting": 2.5,
        "review": 2.25,
        "waving": 1.5,
        "jumping": 1.5,
        "failed": 2.0,
        "running": 1.5,
        "running-right": 1.5,
        "running-left": 1.5,
    }

    def __init__(self, atlas_path: Path) -> None:
        self.atlas_path = atlas_path
        self.motion = MotionLibrary(atlas_path.parent / "motion")
        self.frames: dict[str, list[Image.Image]] = {}
        try:
            with Image.open(atlas_path) as source:
                atlas = source.convert("RGBA")
            if atlas.size != (1536, 1872):
                raise ValueError(f"invalid pet atlas size {atlas.size}")
            for state, (row, durations) in PET_ROWS.items():
                self.frames[state] = [
                    atlas.crop(
                        (column * 192, row * 208, (column + 1) * 192, (row + 1) * 208)
                    ).resize((190, 206), Image.Resampling.LANCZOS)
                    for column in range(len(durations))
                ]
        except (OSError, ValueError):
            self.frames = {}

    @property
    def ready(self) -> bool:
        return self.motion.ready or bool(self.frames)

    def frame(self, state: str, now: float) -> Image.Image | None:
        if self.motion.ready:
            return self.motion.frame(state, now)
        if state not in self.frames:
            state = "idle"
        frames = self.frames.get(state)
        row = PET_ROWS.get(state)
        if not frames or row is None:
            return None
        durations = row[1]
        cycle = sum(durations)
        speed_scale = self.SPEED_SCALE.get(state, 2.4)
        cursor = int(now * 1000 / speed_scale) % cycle
        selected = 0
        for index, duration in enumerate(durations):
            if cursor < duration:
                selected = index
                break
            cursor -= duration
        return frames[selected]


class HabitatDeck:
    """A generated environment deck with procedural fallbacks and soft motion."""

    WIDTH = 420
    HEIGHT = 268
    SCENES = (
        ("NEON ROOFTOP", "#090B1B", "#231044", PURPLE, "city"),
        ("MOON SHRINE", "#090D20", "#24162F", MAGENTA, "shrine"),
        ("RAINY ARCADE", "#080E18", "#102B38", CYAN, "arcade"),
        ("SAKURA TRAIN", "#1A0C1E", "#34203B", MAGENTA, "train"),
        ("CYBER DOJO", "#160B0B", "#321A13", ORANGE, "dojo"),
        ("DEEP SPACE", "#050712", "#18103A", PURPLE, "space"),
        ("LO-FI DEN", "#0C1018", "#202636", BLUE, "den"),
        ("QUANTUM GARDEN", "#071611", "#163126", GREEN, "garden"),
        ("NIGHT MARKET", "#180A14", "#342015", AMBER, "market"),
        ("CRYSTAL CAVE", "#080B18", "#20133B", CYAN, "cave"),
        ("CLOUD DECK", "#10172A", "#273659", BLUE, "cloud"),
        ("SUNSET BEACH", "#24101B", "#422318", ORANGE, "beach"),
        ("HACKER CAFE", "#0B1112", "#1A2A26", GREEN, "den"),
        ("SNOW TERMINAL", "#0A1320", "#24394A", CYAN, "train"),
        ("VOLCANO LAB", "#180807", "#3D160C", ORANGE, "cave"),
        ("ABYSSAL CITY", "#040C18", "#092B3A", BLUE, "city"),
        ("GHOST SERVER", "#07110D", "#153025", GREEN, "arcade"),
        ("CANDY CLOUDS", "#211026", "#47254E", MAGENTA, "cloud"),
        ("DATA FOREST", "#051511", "#12362E", CYAN, "garden"),
        ("STORM ROOFTOP", "#09101A", "#162D40", CYAN, "city"),
        ("RETRO BEDROOM", "#170C1D", "#33213B", MAGENTA, "den"),
        ("DESERT RELAY", "#26140A", "#4A2B12", AMBER, "beach"),
        ("ORBITAL LOUNGE", "#070817", "#151F42", BLUE, "space"),
        ("VAPOR POOL", "#1D0C2C", "#34214F", PURPLE, "beach"),
    )

    def __init__(self, background_dir: Path | None = None) -> None:
        self.random = random.Random(time.time_ns() ^ os.getpid())
        self.frames = self._load_generated(background_dir)
        if not self.frames:
            self.frames = [
                (name, self._build(index, top, bottom, accent, motif))
                for index, (name, top, bottom, accent, motif) in enumerate(self.SCENES)
            ]
        self.current = self.random.randrange(len(self.frames))
        self.previous = self.current
        self.switched_at = time.time() - 4
        self.next_switch = time.time() + self.random.uniform(120, 300)
        self.viewport_cache: dict[int, tuple[int, int, Image.Image]] = {}

    def _load_generated(self, background_dir: Path | None) -> list[tuple[str, Image.Image]]:
        if background_dir is None or not background_dir.is_dir():
            return []
        loaded: list[tuple[str, Image.Image]] = []
        for path in sorted(background_dir.iterdir()):
            if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                continue
            try:
                with Image.open(path) as source:
                    art = ImageOps.fit(
                        source.convert("RGB"), (self.WIDTH + 28, self.HEIGHT + 16),
                        method=Image.Resampling.LANCZOS,
                    )
                art = ImageEnhance.Color(art).enhance(1.58)
                art = ImageEnhance.Contrast(art).enhance(1.34)
                art = ImageEnhance.Brightness(art).enhance(1.08)
                loaded.append((path.stem.upper().replace("-", " "), art))
            except (OSError, ValueError):
                continue
        return loaded

    def _viewport(self, index: int, now: float) -> Image.Image:
        art = self.frames[index][1]
        if art.size == (self.WIDTH, self.HEIGHT):
            return art.copy()
        travel_x = max(0, art.width - self.WIDTH)
        travel_y = max(0, art.height - self.HEIGHT)
        phase = now / 60.0 + index * 1.73
        offset_x = int((math.sin(phase) * 0.5 + 0.5) * travel_x)
        offset_y = int((math.cos(phase * 0.71) * 0.5 + 0.5) * travel_y)
        cached = self.viewport_cache.get(index)
        if cached is not None and cached[0] == offset_x and cached[1] == offset_y:
            return cached[2]
        viewport = art.crop((offset_x, offset_y, offset_x + self.WIDTH, offset_y + self.HEIGHT))
        self.viewport_cache[index] = (offset_x, offset_y, viewport)
        return viewport

    @staticmethod
    def _rgb(value: str) -> tuple[int, int, int]:
        value = value.lstrip("#")
        return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]

    @staticmethod
    def _mix(first: tuple[int, int, int], second: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
        return tuple(int(a + (b - a) * amount) for a, b in zip(first, second))  # type: ignore[return-value]

    def _build(
        self, index: int, top: str, bottom: str, accent_hex: str, motif: str
    ) -> Image.Image:
        image = Image.new("RGB", (self.WIDTH, self.HEIGHT), top)
        draw = ImageDraw.Draw(image)
        top_rgb, bottom_rgb = self._rgb(top), self._rgb(bottom)
        accent = self._rgb(accent_hex)
        dim = self._mix(accent, (8, 8, 10), 0.62)
        faint = self._mix(accent, (8, 8, 10), 0.82)
        rng = random.Random(7907 + index * 137)
        for yy in range(self.HEIGHT):
            draw.line((0, yy, self.WIDTH, yy), fill=self._mix(top_rgb, bottom_rgb, yy / (self.HEIGHT - 1)))

        if motif == "city":
            for xx in range(-4, self.WIDTH, 25):
                height = rng.randint(34, 105)
                draw.rectangle((xx, 170 - height, xx + rng.randint(15, 28), 190), fill="#090A10")
                for wy in range(176 - height, 170, 13):
                    if rng.random() > 0.42:
                        draw.rectangle((xx + 5, wy, xx + 8, wy + 4), fill=dim)
            draw.line((0, 190, self.WIDTH, 190), fill=accent, width=2)
        elif motif == "shrine":
            draw.ellipse((286, 28, 354, 96), fill=self._mix(accent, (245, 245, 230), 0.66))
            draw.rectangle((72, 93, 80, 190), fill=dim)
            draw.rectangle((171, 93, 179, 190), fill=dim)
            draw.rectangle((58, 84, 193, 94), fill=accent)
            draw.rectangle((75, 106, 177, 113), fill=dim)
            draw.polygon(((0, 190), (82, 130), (158, 190)), fill="#090C14")
            draw.polygon(((112, 190), (254, 118), (402, 190)), fill="#0A0B13")
        elif motif == "arcade":
            for xx in range(22, 390, 67):
                draw.rectangle((xx, 76, xx + 43, 175), fill="#090A10", outline=dim, width=2)
                draw.rectangle((xx + 7, 87, xx + 36, 118), fill=faint)
                draw.rectangle((xx + 12, 132, xx + 31, 139), fill=accent)
            for yy in range(179, 220, 10):
                draw.line((0, yy, self.WIDTH, yy), fill=faint)
            for xx in range(0, self.WIDTH + 1, 40):
                draw.line((201, 174, xx, 220), fill=faint)
        elif motif == "train":
            draw.rectangle((0, 45, self.WIDTH, 194), fill="#101019", outline=dim, width=3)
            for xx in range(18, 390, 78):
                draw.rounded_rectangle((xx, 69, xx + 56, 145), radius=4, fill="#1C1730", outline=accent)
                draw.line((xx + 28, 70, xx + 28, 144), fill=dim)
            draw.line((0, 176, self.WIDTH, 176), fill=accent, width=4)
        elif motif == "dojo":
            draw.rectangle((118, 34, 284, 143), fill="#171015", outline=dim, width=3)
            draw.ellipse((167, 60, 235, 128), fill=self._mix(accent, (250, 210, 170), 0.72))
            for xx in range(0, self.WIDTH + 1, 58):
                draw.line((201, 145, xx, 220), fill=dim)
            for yy in range(155, 220, 16):
                draw.line((0, yy, self.WIDTH, yy), fill=faint)
        elif motif == "space":
            for _ in range(56):
                xx, yy = rng.randrange(self.WIDTH), rng.randrange(170)
                radius = 1 if rng.random() < 0.87 else 2
                draw.rectangle((xx, yy, xx + radius, yy + radius), fill=dim)
            draw.ellipse((276, 60, 388, 172), fill="#11172F", outline=accent, width=3)
            draw.arc((250, 91, 405, 143), 190, 350, fill=dim, width=3)
        elif motif == "den":
            draw.rectangle((28, 40, 153, 135), fill="#0B1020", outline=dim, width=3)
            draw.line((90, 41, 90, 134), fill=faint)
            draw.line((29, 87, 152, 87), fill=faint)
            draw.rectangle((192, 101, 368, 164), fill="#0A0B11", outline=accent, width=2)
            draw.rectangle((211, 114, 278, 150), fill=faint)
            draw.rectangle((290, 114, 349, 150), fill=dim)
            draw.rectangle((174, 164, 386, 174), fill="#08090D")
        elif motif == "garden":
            draw.ellipse((294, 25, 354, 85), fill=self._mix(accent, (235, 250, 220), 0.75))
            draw.polygon(((0, 177), (72, 112), (156, 176)), fill="#0B1915")
            draw.polygon(((98, 181), (229, 102), (359, 181)), fill="#0C1D18")
            for xx in (36, 116, 310, 372):
                draw.rectangle((xx, 132, xx + 4, 190), fill=dim)
                draw.rounded_rectangle((xx - 7, 126, xx + 11, 146), radius=3, fill=faint, outline=accent)
        elif motif == "market":
            for xx in range(0, self.WIDTH, 84):
                draw.polygon(((xx, 78), (xx + 42, 54), (xx + 84, 78)), fill=dim)
                draw.rectangle((xx + 5, 78, xx + 79, 182), fill="#100B10", outline=faint)
            for xx in range(42, self.WIDTH, 84):
                draw.line((xx, 18, xx, 51), fill=faint)
                draw.ellipse((xx - 6, 47, xx + 6, 59), fill=accent)
        elif motif == "cave":
            for xx in range(10, self.WIDTH, 47):
                height = rng.randint(28, 98)
                draw.polygon(((xx, 198), (xx + 16, 198 - height), (xx + 34, 198)), fill=dim, outline=accent)
            draw.polygon(((0, 0), (90, 0), (47, 62), (142, 0), (0, 0)), fill="#06070C")
            draw.polygon(((402, 0), (312, 0), (352, 72), (264, 0), (402, 0)), fill="#06070C")
        elif motif == "cloud":
            for xx, yy, scale in ((24, 70, 1), (232, 38, 1), (310, 118, 2), (94, 144, 2)):
                shade = self._mix(accent, (225, 235, 255), 0.72)
                draw.ellipse((xx, yy, xx + 42 * scale, yy + 18 * scale), fill=shade)
                draw.ellipse((xx + 15 * scale, yy - 8 * scale, xx + 40 * scale, yy + 14 * scale), fill=shade)
            draw.polygon(((126, 178), (201, 142), (276, 178), (248, 190), (153, 190)), fill="#11172A", outline=accent)
        elif motif == "beach":
            draw.ellipse((294, 39, 360, 105), fill=self._mix(accent, (255, 220, 140), 0.62))
            draw.rectangle((0, 125, self.WIDTH, 220), fill="#101A2C")
            for yy in range(133, 190, 14):
                draw.line((0, yy, self.WIDTH, yy), fill=dim)
            draw.rectangle((0, 190, self.WIDTH, 220), fill="#2A1A17")
            draw.line((60, 186, 78, 92), fill="#0A0B0F", width=6)
            draw.polygon(((76, 98), (23, 77), (72, 111)), fill="#0A0B0F")
            draw.polygon(((75, 98), (124, 67), (83, 112)), fill="#0A0B0F")

        for yy in range(0, self.HEIGHT, 4):
            draw.line((0, yy, self.WIDTH, yy), fill=(0, 0, 0), width=1)
        return image

    def frame(self, now: float) -> tuple[str, Image.Image]:
        if now >= self.next_switch:
            choices = [index for index in range(len(self.frames)) if index != self.current]
            self.previous = self.current
            self.current = self.random.choice(choices)
            self.switched_at = now
            self.next_switch = now + self.random.uniform(120, 300)
        progress = max(0.0, min(1.0, (now - self.switched_at) / 6.0))
        eased = progress * progress * (3 - 2 * progress)
        current_name = self.frames[self.current][0]
        current = self._viewport(self.current, now)
        if eased >= 1:
            scene = current.copy()
        else:
            previous = self._viewport(self.previous, now)
            scene = Image.blend(previous, current, eased)
        ambient = ImageDraw.Draw(scene)
        scan_y = int((now * 4) % self.HEIGHT)
        ambient.line((0, scan_y, self.WIDTH, scan_y), fill=self._mix(self._rgb(PURPLE), (8, 8, 10), 0.86))
        return current_name, scene


class PetBehavior:
    """Translate model activity into a persistent Tamagotchi-style care loop."""

    ROUTINES: dict[str, tuple[tuple[str, float], ...]] = {
        "idle-dream": (("idle", 7.0), ("waiting", 2.2), ("idle", 6.0)),
        "idle-look": (("idle", 4.0), ("review", 2.8), ("waiting", 1.8), ("idle", 5.0)),
        "idle-stretch": (("waiting", 2.5), ("idle", 5.0), ("waving", 1.4), ("idle", 5.0)),
        "idle-focus": (("review", 4.5), ("waiting", 2.0), ("idle", 5.5)),
        "greeting": (("waving", 1.8), ("idle", 1.0), ("waving", 1.4)),
        "snack-dance": (("review", 1.2), ("waving", 1.4), ("jumping", 1.5), ("idle", 1.3)),
        "mega-feast": (("review", 1.4), ("jumping", 1.8), ("waving", 1.5), ("idle", 1.5)),
        "workout": (("running", 2.8), ("waiting", 1.2), ("running-right", 2.8), ("idle", 1.8)),
        "zoomies": (("running-right", 2.3), ("jumping", 1.2), ("running-left", 2.3), ("idle", 1.8)),
        "deep-focus": (("review", 5.0), ("waiting", 2.0), ("idle", 3.0)),
        "digest": (("waiting", 3.5), ("idle", 5.5), ("review", 2.0)),
        "grooming": (("review", 3.0), ("waiting", 1.8), ("idle", 5.0)),
        "catnap": (("waiting", 2.8), ("idle", 9.0), ("waiting", 2.2)),
        "victory": (("jumping", 1.4), ("waving", 1.8), ("idle", 2.0)),
        "cooldown": (("failed", 4.0), ("waiting", 2.5), ("idle", 5.0)),
        "patrol": (("running-right", 3.2), ("waiting", 1.8), ("running-left", 3.2), ("idle", 4.0)),
        "curious": (("review", 3.0), ("waiting", 2.0), ("idle", 5.0)),
        "wake-up": (("idle", 1.8), ("waving", 1.5), ("waiting", 1.2)),
    }
    LOCKED_ROUTINES = {
        "greeting", "snack-dance", "mega-feast", "victory", "cooldown",
        "wake-up", "workout", "zoomies", "patrol", "curious",
        "idle-look", "idle-stretch", "idle-focus", "grooming", "catnap",
    }

    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self.random = random.Random(time.time_ns() ^ (os.getpid() << 7))
        self.food = 72.0
        self.energy = 78.0
        self.bond = 36.0
        self.health = 92.0
        self.focus = 64.0
        self.curiosity = 42.0
        self.composure = 88.0
        self.hygiene = 86.0
        self.xp = 0.0
        self.meals = 0
        self.workouts = 0
        self.sessions = 0
        self.tokens_eaten = 0
        self.care_streak = 0
        self.best_streak = 0
        self.last_active_day = ""
        self.born_at = time.time()
        self.last_update = time.time()
        self.last_saved = 0.0
        self.token_counters: dict[str, int] = {}
        self.active_models: set[str] = set()
        self.activity_live = False
        self.last_error_at = 0.0
        self.last_unrecovered_error = 0.0
        self.last_active_at = time.time()
        self.active_seconds = 0.0
        self.night_seconds = 0.0
        self.tool_calls = 0
        self.completions = 0
        self.recoveries = 0
        self.tools_seen: set[str] = set()
        self.models_seen: set[str] = set()
        self.relics: list[str] = []
        self.latest_relic = ""
        self.last_relic_day = ""
        self.recent_event_keys: deque[str] = deque(maxlen=96)
        self.pending_tool_calls = 0
        self.pending_completion = False
        self.pending_relic_announcement = ""
        self.last_completion_reaction = 0.0
        self.next_tool_reaction = time.time() + self.random.uniform(18, 28)
        self.snack_started = 0.0
        self.snack_until = 0.0
        self.last_snack_at = 0.0
        self.pending_snack_tokens = 0
        self.last_snack = 0
        self.snack_name = "CACHE CHIP"
        self.error_until = 0.0
        self.routine = "idle-dream"
        self.routine_started = time.time()
        self.routine_until = 0.0
        self.next_idle_event = time.time() + self.random.uniform(28, 55)
        self.next_activity_event = time.time() + self.random.uniform(38, 72)
        self.last_level = 0
        self.state = "idle"
        self.state_started = time.time()
        self.action = "DREAMING IN TOKENS"
        self._load()

    @staticmethod
    def _clamp(value: float) -> float:
        return max(0.0, min(100.0, value))

    def _load(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            saved_at = float(raw.get("saved_at", time.time()))
            offline = max(0.0, min(7 * 86400, time.time() - saved_at))
            self.food = self._clamp(float(raw.get("food", self.food)) - offline * 0.0006)
            self.energy = self._clamp(float(raw.get("energy", self.energy)) + offline * 0.003)
            self.bond = self._clamp(float(raw.get("bond", self.bond)))
            self.health = self._clamp(float(raw.get("health", self.health)) + offline * 0.0002)
            self.focus = self._clamp(float(raw.get("focus", self.focus)) - offline * 0.0002)
            self.curiosity = self._clamp(float(raw.get("curiosity", self.curiosity)) - offline * 0.0001)
            self.composure = self._clamp(float(raw.get("composure", self.composure)) + offline * 0.0003)
            self.hygiene = self._clamp(float(raw.get("hygiene", self.hygiene)) - offline * 0.00025)
            self.xp = max(0.0, float(raw.get("xp", self.xp)))
            self.meals = max(0, int(raw.get("meals", self.meals)))
            self.workouts = max(0, int(raw.get("workouts", self.workouts)))
            self.sessions = max(0, int(raw.get("sessions", self.sessions)))
            self.tokens_eaten = max(0, int(raw.get("tokens_eaten", self.tokens_eaten)))
            self.care_streak = max(0, int(raw.get("care_streak", self.care_streak)))
            self.best_streak = max(self.care_streak, int(raw.get("best_streak", self.best_streak)))
            self.last_active_day = str(raw.get("last_active_day", self.last_active_day))
            self.born_at = float(raw.get("born_at", self.born_at))
            self.last_active_at = float(raw.get("last_active_at", self.last_active_at))
            self.active_seconds = max(0.0, float(raw.get("active_seconds", self.active_seconds)))
            self.night_seconds = max(0.0, float(raw.get("night_seconds", self.night_seconds)))
            self.tool_calls = max(0, int(raw.get("tool_calls", self.tool_calls)))
            self.completions = max(0, int(raw.get("completions", self.completions)))
            self.recoveries = max(0, int(raw.get("recoveries", self.recoveries)))
            self.tools_seen = {str(value) for value in raw.get("tools_seen", []) if str(value)}
            self.models_seen = {str(value) for value in raw.get("models_seen", []) if str(value)}
            self.relics = [str(value) for value in raw.get("relics", []) if str(value)][-40:]
            self.latest_relic = str(raw.get("latest_relic", self.latest_relic))
            self.last_relic_day = str(raw.get("last_relic_day", self.last_relic_day))
            self.recent_event_keys.extend(
                str(value) for value in raw.get("recent_event_keys", []) if str(value)
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError, AttributeError):
            pass

    def _save(self, now: float, force: bool = False) -> None:
        if not force and now - self.last_saved < 30:
            return
        payload = {
            "schema": 3,
            "food": round(self.food, 3),
            "energy": round(self.energy, 3),
            "bond": round(self.bond, 3),
            "health": round(self.health, 3),
            "focus": round(self.focus, 3),
            "curiosity": round(self.curiosity, 3),
            "composure": round(self.composure, 3),
            "hygiene": round(self.hygiene, 3),
            "xp": round(self.xp, 3),
            "meals": self.meals,
            "workouts": self.workouts,
            "sessions": self.sessions,
            "tokens_eaten": self.tokens_eaten,
            "care_streak": self.care_streak,
            "best_streak": self.best_streak,
            "last_active_day": self.last_active_day,
            "born_at": self.born_at,
            "last_active_at": self.last_active_at,
            "active_seconds": round(self.active_seconds, 1),
            "night_seconds": round(self.night_seconds, 1),
            "tool_calls": self.tool_calls,
            "completions": self.completions,
            "recoveries": self.recoveries,
            "tools_seen": sorted(self.tools_seen)[-32:],
            "models_seen": sorted(self.models_seen)[-24:],
            "relics": self.relics[-40:],
            "latest_relic": self.latest_relic,
            "last_relic_day": self.last_relic_day,
            "recent_event_keys": list(self.recent_event_keys),
            "saved_at": now,
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_name(f".{self.state_path.name}.{os.getpid()}.tmp")
            temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            os.replace(temporary, self.state_path)
            self.last_saved = now
        except OSError:
            pass

    def _set_routine(self, name: str, action: str, now: float, hold: float = 0.0) -> bool:
        if name not in self.ROUTINES:
            name = "idle-dream"
        changed = name != self.routine
        if changed:
            self.routine = name
            self.routine_started = now
            self.routine_until = now
        self.action = action
        if hold > 0:
            self.routine_until = max(self.routine_until, now + hold)
        return changed

    def _routine_frame(self, now: float) -> str:
        sequence = self.ROUTINES.get(self.routine, self.ROUTINES["idle-dream"])
        total = sum(duration for _, duration in sequence)
        cursor = max(0.0, now - self.routine_started) % total
        for state, duration in sequence:
            if cursor < duration:
                return state
            cursor -= duration
        return sequence[-1][0]

    def _record_care_day(self, now: float) -> None:
        current = datetime.fromtimestamp(now).date()
        current_text = current.isoformat()
        if current_text == self.last_active_day:
            return
        try:
            previous = date.fromisoformat(self.last_active_day)
        except ValueError:
            previous = None
        self.care_streak = self.care_streak + 1 if previous == current - timedelta(days=1) else 1
        self.best_streak = max(self.best_streak, self.care_streak)
        self.last_active_day = current_text

    def _ingest_events(self, snapshot: Snapshot, now: float) -> None:
        known = set(self.recent_event_keys)
        for event in sorted(snapshot.events, key=lambda item: item.timestamp):
            key = f"{event.timestamp:.3f}|{event.source}|{event.level}|{event.message}"
            if key in known:
                continue
            self.recent_event_keys.append(key)
            known.add(key)
            message = event.message.upper()
            tool_match = re.search(r"(?:^| / )TOOL / ([A-Z0-9_.-]+)", message)
            if tool_match:
                self.tool_calls += 1
                self.pending_tool_calls += 1
                self.tools_seen.add(tool_match.group(1)[:24])
                curiosity_gain = 1.05 if self.archetype == "FORAGER" else 0.8
                self.curiosity = self._clamp(self.curiosity + curiosity_gain)
            if event.level == "WARN":
                self.composure = self._clamp(self.composure - 0.8)
            if event.level == "ERROR":
                self.last_error_at = max(self.last_error_at, event.timestamp)
                self.last_unrecovered_error = max(self.last_unrecovered_error, event.timestamp)
                self.composure = self._clamp(self.composure - 7.0)
                self.focus = self._clamp(self.focus - 2.0)
                if abs(now - event.timestamp) < 15:
                    self.error_until = max(self.error_until, now + 2.4)
            if "DONE" in message or "COMPLETE" in message:
                self.completions += 1
                meaningful = "TASK COMPLETE" in message or "AGENTMESSAGE" in message
                self.pending_completion = self.pending_completion or meaningful
                if (
                    self.last_unrecovered_error
                    and event.timestamp >= self.last_unrecovered_error
                    and event.timestamp - self.last_unrecovered_error < 900
                ):
                    self.recoveries += 1
                    self.composure = self._clamp(self.composure + 10.0)
                    self.last_unrecovered_error = 0.0

    def _check_relics(self, now: float) -> None:
        today = datetime.fromtimestamp(now).date().isoformat()
        if today == self.last_relic_day:
            return
        candidates = (
            (self.sessions >= 1, "FIRST SIGNAL"),
            (self.tool_calls >= 1, "TOOL CHARM"),
            (self.completions >= 1, "CLEAN FINISH"),
            (self.recoveries >= 1, "PHOENIX CHIP"),
            (len(self.models_seen) >= 3, "SWARM CREST"),
            (len(self.tools_seen) >= 6, "FORAGER KEY"),
            (self.night_seconds >= 1800, "NIGHT LANTERN"),
            (self.active_seconds >= 3600, "FOCUS GEM"),
            (self.tool_calls >= 100, "CENTURY KEY"),
            (self.care_streak >= 7, "THREAD OF SEVEN"),
        )
        for earned, name in candidates:
            if earned and name not in self.relics:
                self.relics.append(name)
                self.latest_relic = name
                self.last_relic_day = today
                self.pending_relic_announcement = name
                return

    def update(self, snapshot: Snapshot, now: float, *, resting: bool = False) -> None:
        elapsed = max(0.0, min(15.0, now - self.last_update))
        self.last_update = now
        models = active_model_ingest(snapshot, now)
        current_models = {row.model for row in models if row.last_seen and now - row.last_seen < 90}
        recent_signal = any(
            timestamp > 0 and now - timestamp < 20
            for timestamp in (
                *(event.timestamp for event in snapshot.events),
                *(record.timestamp for record in snapshot.otel_records),
                *(relation.updated_at for relation in snapshot.agent_relations),
            )
        )
        active = max(len(current_models), 1 if recent_signal else 0)
        newcomers = current_models - self.active_models
        if active and not self.activity_live:
            self.sessions += 1
        if active:
            self._record_care_day(now)
            self.last_active_at = now
            self.active_seconds += elapsed
            if datetime.fromtimestamp(now).hour >= 22 or datetime.fromtimestamp(now).hour < 6:
                self.night_seconds += elapsed
            self.models_seen.update(current_models)
        self.active_models = current_models
        self.activity_live = active > 0
        self._ingest_events(snapshot, now)
        self._check_relics(now)
        if snapshot.stats.gpu_temp >= 84:
            self.error_until = max(self.error_until, now + 1.2)
            self.composure = self._clamp(self.composure - elapsed * 0.12)

        delta = 0
        for key, live in canonical_usages(snapshot, now).items():
            identity = "\x1f".join(key)
            prior = self.token_counters.get(identity)
            if prior is not None and live.session_tokens >= prior:
                delta += live.session_tokens - prior
            self.token_counters[identity] = live.session_tokens
        live = snapshot.live_usage
        if delta > 0:
            self.xp += math.log10(delta + 1) * 3.4
            self.tokens_eaten += delta
            self.pending_snack_tokens += delta
        if self.pending_snack_tokens > 0 and now - self.last_snack_at >= 24.0:
            snack_delta = self.pending_snack_tokens
            snack_power = min(8.0, 1.5 + math.log10(snack_delta + 1))
            self.pending_snack_tokens = 0
            self.last_snack_at = now
            self.meals += 1
            self.food = self._clamp(self.food + snack_power)
            self.bond = self._clamp(self.bond + min(1.5, snack_power * 0.18))
            self.last_snack = snack_delta
            self.snack_started = now
            self.snack_until = now + (5.2 if snack_delta < 1_000_000 else 6.2)
            if snack_delta < 1_000:
                self.snack_name = "BYTEBERRY"
            elif snack_delta < 100_000:
                self.snack_name = "CACHE CHIP"
            elif snack_delta < 1_000_000:
                self.snack_name = "CONTEXT COOKIE"
            elif snack_delta < 10_000_000:
                self.snack_name = "MEGA MOCHI"
            else:
                self.snack_name = "TOKEN CAKE"
            routine = "mega-feast" if snack_delta >= 1_000_000 else "snack-dance"
            self._set_routine(
                routine, f"EATING {self.snack_name} +{compact_number(snack_delta, 1)}", now,
                self.snack_until - now,
            )

        self.food = self._clamp(self.food - elapsed * 0.0008)
        if resting:
            self.energy = self._clamp(self.energy + elapsed * .18)
        if active:
            night_now = datetime.fromtimestamp(now).hour >= 22 or datetime.fromtimestamp(now).hour < 6
            energy_cost = 0.003 + active * 0.0012
            if self.archetype == "NIGHTWATCHER" and night_now:
                energy_cost *= 0.65
            self.energy = self._clamp(self.energy - elapsed * energy_cost)
            focus_bonus = 4.0 if self.archetype == "ARCHITECT" else 0.0
            focus_target = min(96.0, 52.0 + active * 10.0 + focus_bonus)
            self.focus = max(self.focus, min(88.0, 48.0 + active * 10.0))
            self.focus = self._clamp(self.focus + (focus_target - self.focus) * min(0.08, elapsed / 45))
            self.hygiene = self._clamp(self.hygiene - elapsed * (0.001 + active * 0.0005))
            self.bond = self._clamp(self.bond + elapsed * active * 0.0018)
            composure_gain = 0.006 if self.archetype == "DEBUGGER" else 0.004
            self.composure = self._clamp(self.composure + elapsed * composure_gain)
        else:
            self.energy = self._clamp(self.energy + elapsed * 0.008)
            self.focus = self._clamp(self.focus + (38.0 - self.focus) * min(0.05, elapsed / 90))
            self.curiosity = self._clamp(self.curiosity - elapsed * 0.0006)
            self.composure = self._clamp(self.composure + elapsed * 0.008)
            self.health = self._clamp(self.health + elapsed * 0.0015)

        interruptible_idle = {
            "idle-dream", "idle-look", "idle-stretch", "idle-focus",
            "grooming", "catnap", "digest", "patrol",
        }
        locked = (
            now < self.routine_until
            and self.routine in self.LOCKED_ROUTINES
            and not (active and self.routine in interruptible_idle)
        )
        if now < self.error_until:
            self._set_routine("cooldown", "SIGNAL GLITCH / STABILIZING", now, self.error_until - now)
        elif now < self.snack_until:
            pass
        elif not locked:
            current_level = self.level(snapshot)
            if self.last_level == 0:
                self.last_level = current_level
            if current_level > self.last_level:
                self.last_level = current_level
                self._set_routine("victory", f"LEVEL UP / LV {current_level:02d}", now, 3.0)
            elif self.pending_relic_announcement:
                relic = self.pending_relic_announcement
                self.pending_relic_announcement = ""
                self._set_routine("victory", f"FOUND RELIC / {relic}", now, 4.8)
            elif self.pending_completion and now - self.last_completion_reaction >= 30:
                self.pending_completion = False
                self.last_completion_reaction = now
                self._set_routine("victory", "CLEAN COMPLETION", now, 2.4)
            elif newcomers:
                names = "+".join(sorted(newcomers))
                self.bond = self._clamp(self.bond + min(3, len(newcomers)))
                self._set_routine("greeting", f"HELLO {names}", now, 4.2)
            elif self.pending_tool_calls and now >= self.next_tool_reaction:
                calls = self.pending_tool_calls
                self.pending_tool_calls = 0
                self.next_tool_reaction = now + self.random.uniform(22, 32)
                self._set_routine("curious", f"INVESTIGATING +{calls} TOOLS", now, 8.0)
            elif active >= 2 and self.energy > 18 and now >= self.next_activity_event:
                routine = "zoomies" if active >= 4 and self.random.random() < 0.35 else "workout"
                action = f"MODEL ZOOMIES ×{active}" if routine == "zoomies" else f"SWARM TRAINING ×{active}"
                if self._set_routine(routine, action, now, 7.2):
                    self.workouts += 1
                self.next_activity_event = now + self.random.uniform(48, 90)
            elif active:
                focus_label = models[0].model if models else "AGENT SIGNAL"
                self._set_routine("deep-focus", f"DEEP WORK / {focus_label}", now)
            elif 1 <= datetime.fromtimestamp(now).hour < 7:
                self._set_routine("catnap", "HIBERNATING / QUIET HOURS", now)
            elif now - self.last_active_at >= 600:
                self._set_routine("catnap", "HIBERNATING / SIGNAL QUIET", now)
            elif now - self.last_active_at >= 120:
                self._set_routine("idle-dream", "DOZING / SIGNAL QUIET", now)
            elif self.energy < 24:
                self._set_routine("catnap", "POWER NAP / RECHARGING", now)
            elif self.hygiene < 42:
                self._set_routine("grooming", "SELF-CARE / GROOMING", now)
            elif live is not None and now - live.updated_at < 300:
                self._set_routine("digest", "DIGESTING THE LAST RUN", now)
            elif now >= self.next_idle_event:
                name, action = self.random.choice((
                    ("idle-look", "WATCHING THE SIGNALS"),
                    ("idle-stretch", "TAKING A TINY STRETCH"),
                    ("idle-focus", "QUIETLY STUDYING"),
                    ("patrol", "PATROLLING THE HABITAT"),
                    ("curious", "CURIOUS ABOUT THE QUEUE"),
                    ("grooming", "SELF-CARE / GROOMING"),
                    ("catnap", "CATNAP / SAVING ENERGY"),
                ))
                hold = min(14.0, sum(duration for _, duration in self.ROUTINES[name]))
                self._set_routine(name, action, now, hold)
                self.next_idle_event = now + self.random.uniform(38, 78)
            else:
                self._set_routine("idle-dream", "DREAMING IN TOKENS", now)

        if self.routine == "grooming":
            self.hygiene = self._clamp(self.hygiene + elapsed * 2.2)
        if self.routine == "catnap":
            self.energy = self._clamp(self.energy + elapsed * 0.025)
        next_state = self._routine_frame(now)
        if next_state != self.state:
            self.state = next_state
            self.state_started = now
        self._save(now)

    def close(self) -> None:
        self._save(time.time(), force=True)

    def level(self, snapshot: Snapshot) -> int:
        tokens = max(0, self.tokens_eaten)
        if tokens >= LEVEL_TOKEN_CAP:
            return MAX_PET_LEVEL
        return 1 + int((tokens / LEVEL_TOKEN_CAP) * (MAX_PET_LEVEL - 1))

    def level_progress(self, snapshot: Snapshot) -> float:
        return max(0.0, min(1.0, self.tokens_eaten / LEVEL_TOKEN_CAP))

    def season(self, now: float) -> str:
        age_days = max(0.0, (now - self.born_at) / 86400)
        if age_days < 1:
            return "BOOT"
        if age_days < 7:
            return "CALIBRATED"
        if age_days < 21:
            return "FORM SHIFT"
        return "SEASONED"

    @property
    def archetype(self) -> str:
        if self.active_seconds >= 1800 and self.night_seconds / max(1, self.active_seconds) >= 0.35:
            return "NIGHTWATCHER"
        if self.recoveries >= 3:
            return "DEBUGGER"
        if len(self.tools_seen) >= 6:
            return "FORAGER"
        if len(self.models_seen) >= 3:
            return "ARCHITECT"
        return "OPERATOR"

    @property
    def continuity(self) -> float:
        return self._clamp(20 + self.care_streak * 7 + min(20, self.sessions * 1.5))

    @property
    def care_score(self) -> int:
        values = (self.focus, self.curiosity, self.composure, self.continuity)
        return int(sum(values) / len(values))

    @property
    def mood(self) -> str:
        if self.composure < 35:
            return "RECOVERING"
        if self.energy < 25:
            return "RESTING"
        if self.focus >= 82:
            return "LOCKED IN"
        if self.curiosity >= 75:
            return "CURIOUS"
        if self.care_score >= 82:
            return "THRIVING"
        if self.care_score >= 58:
            return "CONTENT"
        return "CALM"


class Hud:
    width = WIDTH
    height = HEIGHT

    def __init__(
        self, pet_atlas: Path, pet_state: Path, background_dir: Path | None = None,
        otel_hz: int = 30, motion_enabled: bool = True,
        agent_fx_quality: str = "cinematic", companion_name: str = "Companion",
    ) -> None:
        self.ui_title = font(22, bold=True)
        self.ui_body = font(16, bold=True)
        self.ui_small = font(14)
        self.ui_small_bold = font(14, bold=True)
        self.ui_caption = font(12)
        self.nano = font(12, mono=True)
        self.nano_bold = font(12, mono=True, bold=True)
        self.micro = font(14, mono=True, bold=True)
        self.small = font(16, mono=True)
        self.body = font(18, mono=True)
        self.body_bold = font(18, mono=True, bold=True)
        self.label = font(15, mono=True, bold=True)
        self.metric = font(36, bold=True)
        self.metric_small = font(34, bold=True)
        self.model_metric = font(28, mono=True, bold=True)
        self.hero = font(52, bold=True)
        self.brand = font(23, bold=True)
        self.rate_history: deque[float] = deque([0.0] * 72, maxlen=72)
        self.flow_history: deque[float] = deque([0.0] * 72, maxlen=72)
        self.pet = PetAnimator(pet_atlas)
        self.companion_name = redact(companion_name, 24) or "Companion"
        self.habitat_director = HabitatDirector()
        self.pet_behavior = PetBehavior(pet_state)
        self.habitats = HabitatDeck(background_dir)
        self.log_face = font(12, mono=True)
        self.log_bold = font(12, mono=True, bold=True)
        self.agent_log = AgentLog()
        self.otel_tape = OtelBurnTape(otel_hz)
        self.activity_feed = ActivityFeed()
        self.otel_hz_ceiling = max(1, min(30, int(otel_hz)))
        self.motion_enabled = motion_enabled
        quality = str(agent_fx_quality).lower()
        self.agent_fx_ceiling = quality if quality in {"cinematic", "balanced", "lean"} else "cinematic"
        self.agent_fx_quality = self.agent_fx_ceiling
        self.agent_field_plates: dict[tuple[int, int], Image.Image] = {}
        self.agent_core_sprites: dict[tuple[str, bool, bool], Image.Image] = {}
        self.agent_ship_sprites: dict[tuple[str, bool, bool, str], Image.Image] = {}
        self.agent_slot_by_key: dict[str, int] = {}
        self.agent_slot_seen: dict[str, float] = {}
        self.agent_bloom_overlay: Image.Image | None = None
        self.agent_bloom_updated_at = 0.0
        self.overdrive = OverdriveDirector(motion_enabled)
        self.neon_bloom = NeonBloom()
        self.text_tiles: OrderedDict = OrderedDict()
        self.activity_window = ActivityWindow()
        self.deck_updated_at = -1
        self.deck_models = ()
        self.constellation = Constellation()
        self.graph_plates = {}
        self.graph_dots = {}
        self.model_history: dict[str, deque] = {}
        self.pet_motion_state = "idle"
        self.pet_x = 1626.0
        self.pet_move_from = self.pet_x
        self.pet_move_to = self.pet_x
        self.pet_move_started = time.time()
        self.pet_move_duration = 1.0
        self.roster_signature: tuple[str, ...] = ()
        self.roster_offset = 0
        self.roster_changed_at = 0.0
        self._warm_agent_assets()

    def set_runtime_mode(self, mode: str) -> None:
        self.otel_tape.hz = min(self.otel_hz_ceiling, 30)
        if mode == "fixed" or self.agent_fx_ceiling == "lean":
            quality = self.agent_fx_ceiling
        elif mode in {"burst", "active"}:
            quality = "lean"
        elif self.agent_fx_ceiling == "balanced":
            quality = "balanced"
        else:
            quality = "cinematic"
        if quality != self.agent_fx_quality:
            self.agent_fx_quality = quality
            self.agent_bloom_overlay = None
            self.agent_bloom_updated_at = 0.0

    def close(self) -> None:
        self.pet_behavior.close()

    @staticmethod
    def _model_color(model: str) -> str:
        return model_color(model)

    def _pet_stage_x(self, state: str, now: float) -> int:
        progress = max(0.0, min(1.0, (now - self.pet_move_started) / self.pet_move_duration))
        eased = progress * progress * (3 - 2 * progress)
        current = self.pet_move_from + (self.pet_move_to - self.pet_move_from) * eased
        if state != self.pet_motion_state:
            self.pet_motion_state = state
            self.pet_x = current
            target: float | None = None
            if state == "running-right":
                target = 1736.0
            elif state == "running-left":
                target = 1518.0
            elif state == "running":
                target = 1518.0 if current > 1627 else 1736.0
            if target is not None:
                self.pet_move_from = current
                self.pet_move_to = target
                self.pet_move_started = now
                self.pet_move_duration = 3.2
                progress = 0.0
                current = self.pet_move_from
        self.pet_x = current
        return int(round(current))

    def render(self, snapshot: Snapshot, now: float) -> Image.Image:
        rate = combined_token_rate(snapshot, now)
        if int(now) != self.deck_updated_at:
            self.activity_window.update(snapshot, now, rate)
            self.deck_models = model_loads(active_model_ingest(snapshot, now), now)
            self.constellation = session_graph(snapshot, now)
            for row in self.deck_models:
                self.model_history.setdefault(row.model, deque(maxlen=48)).append(row.rate)
            if len(self.model_history) > 48:
                keep = {row.model for row in self.deck_models}
                self.model_history = {key: value for key, value in self.model_history.items() if key in keep}
            self.deck_updated_at = int(now)
        self.rate_history.append(max(0.0, rate))
        self.flow_history.append(max(0.0, snapshot.flow_kbps))
        self.pet_behavior.update(snapshot, now, resting=self.habitat_director.resting)
        self.agent_log.update(snapshot.events)
        self.overdrive.update(snapshot.events, snapshot.otel_records, now)

        image = Image.new("RGB", (WIDTH, HEIGHT), BG)
        self.pet_sprite = None
        draw = CachedDraw(image, self.text_tiles)
        for xx in range(0, WIDTH, 80):
            draw.line((xx, 44, xx, 420), fill=GRID_SOFT)
        for yy in range(44, 421, 44):
            draw.line((0, yy, WIDTH, yy), fill=GRID_SOFT)
        draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), outline=GRID)
        self._header(draw, snapshot, now)
        self._aggregate(draw, snapshot, now)
        self._live(image, draw, snapshot, now)
        self._pet_panel(image, draw, snapshot, now)
        self._trace(draw, snapshot, now)
        self.overdrive.paint(draw, now, self.micro, WIDTH, HEIGHT)
        # The pink perimeter remains outside all information and never sweeps
        # across type or artwork. Bloom is kept separate from the USB governor.
        draw.rectangle((1, 1, WIDTH - 2, HEIGHT - 2), outline="#AF176B", width=2)
        neon_corners(draw, (2, 2, WIDTH - 3, HEIGHT - 3), MAGENTA, 34)
        image = self.neon_bloom.apply(image, now)
        # Draw the current pose after cached bloom: a previous silhouette must
        # never linger in the glow layer while the character moves.
        if self.pet_sprite is not None:
            sprite, position = self.pet_sprite
            image.paste(sprite, position, sprite)
        return image

    def _header(self, draw: ImageDraw.ImageDraw, snapshot: Snapshot, now: float) -> None:
        draw.rectangle((0, 0, WIDTH, 44), fill="#05060C")
        draw.line((14, 43, WIDTH-14, 43), fill="#27303D")
        for x, top in ((17, 14), (23, 19), (29, 14), (35, 10)):
            draw.line((x, top, x, 29), fill=TEXT, width=3)
        draw.line((14, 33, 38, 33), fill=BRAND_ORANGE, width=2)
        draw.text((49, 7), "Usage", fill=TEXT, font=self.brand)
        draw.text((48 + text_width(draw, "Usage", self.brand), 7), "Max", fill=BRAND_ORANGE, font=self.brand)
        leader = self.deck_models[0] if self.deck_models else None
        color = self._model_color(leader.model) if leader else MUTED
        draw.text((217, 5), "Model output / observed" if leader and leader.rate > 0 else "Most recent model",
                  fill=MUTED, font=self.ui_caption)
        draw.text((217, 22), fit_identifier(draw, leader.model if leader else "Waiting for activity", self.ui_small_bold, 290),
                  fill=TEXT, font=self.ui_small_bold)
        leader_value = f"{compact_number(leader.rate)}/s" if leader and leader.rate > 0 else age_text(leader.updated_at if leader else None)
        draw.text((604, 12), leader_value, fill=color, font=self.brand, anchor="ra")
        draw.text((637, 5), "Observed tokens / 60s", fill=MUTED, font=self.ui_caption)
        draw.text((637, 21), f"{'~' if self.activity_window.partial else '+'}{compact_number(self.activity_window.tokens)}", fill=GREEN, font=self.body_bold)
        self._tiny_spark(draw, tuple(self.activity_window.samples), (806, 13, 987, 34), GREEN)
        draw.text((1021, 5), "Activity / 60s", fill=MUTED, font=self.ui_caption)
        draw.text((1021, 21), str(self.activity_window.events_per_minute(now)), fill=CYAN, font=self.body_bold)
        stats = snapshot.stats
        draw.text((1250, 5), "GPU load", fill=MUTED, font=self.ui_caption)
        draw.text((1250, 21), f"{stats.gpu:.0f}%" if stats.vram_total else "—", fill=CYAN, font=self.body_bold)
        draw.text((1470, 5), "GPU temperature", fill=MUTED, font=self.ui_caption)
        draw.text((1470, 21), f"{stats.gpu_temp:.0f}°C" if stats.gpu_temp else "—",
                  fill=AMBER if stats.gpu_temp >= 75 else GREEN, font=self.body_bold)
        for x in (193, 619, 1003, 1232, 1452, 1738):
            draw.line((x, 10, x, 34), fill=GRID_SOFT)
        draw.text((1898, 9), time.strftime("%H:%M:%S", time.localtime(now)), fill=TEXT, font=self.brand, anchor="ra")

    def _tiny_spark(self, draw, values, box, color):
        left, top, right, bottom = box
        draw.line((left, bottom, right, bottom), fill=GRID_SOFT)
        maximum = max(values, default=0)
        if not maximum or len(values) < 2:
            return
        points = [(left + round(i * (right - left) / (len(values) - 1)),
                   bottom - round(max(0, value) / maximum * (bottom - top))) for i, value in enumerate(values)]
        draw.line(points, fill=NEON_DIM.get(color, GRID_SOFT), width=4)
        draw.line(points, fill=color, width=1)
        x, y = points[-1]
        draw.ellipse((x - 2, y - 2, x + 2, y + 2), fill=TEXT)

    def _stat_cell(
        self, draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], label: str,
        value: str, color: str = TEXT, small: bool = False,
    ) -> None:
        x, y, right, bottom = box
        draw.rectangle(box, fill="#03060D")
        draw.line((x+16, bottom-2, right-16, bottom-2), fill="#26303E")
        if x:
            draw.line((x, y+13, x, bottom-17), fill="#222C39")
        draw.text((x + 16, y + 10), label.capitalize(), fill=MUTED, font=self.ui_small)
        available = right - x - 32
        face = self.metric_small if small or text_width(draw, value, self.metric) > available else self.metric
        safe_value = fit_text(draw, value, face, available)
        draw.text((x + 16, bottom - 12), safe_value,
                  fill=color if "SPEED" in label else TEXT, font=face, anchor="ls")

    def _usage_hero(
        self, draw: ImageDraw.ImageDraw, label: str, tokens: int,
        side_label: str, side_value: str, side_color: str,
    ) -> None:
        draw.rectangle((0, 45, 399, 144), fill="#03060D")
        draw.text((16, 61), label.capitalize(), fill=MUTED, font=self.ui_small)
        value = compact_number(tokens, 2)
        face = self.hero if text_width(draw, value, self.hero) <= 216 else self.metric
        draw.text((16, 126), value, fill=TEXT, font=face, anchor="ls")
        draw.line((248, 62, 248, 126), fill="#27303D")
        draw.text((384, 64), fit_text(draw, side_label, self.nano_bold, 128),
                  fill=MUTED, font=self.nano_bold, anchor="ra")
        side_face = self.metric_small if text_width(draw, side_value, self.metric_small) <= 129 else self.small
        draw.text((384, 126), fit_text(draw, side_value, side_face, 128),
                  fill=side_color, font=side_face, anchor="rs")
        draw.line((16, 141, 384, 141), fill="#27303D")

    def _aggregate(self, draw: ImageDraw.ImageDraw, snapshot: Snapshot, now: float | None = None) -> None:
        now = time.time() if now is None else now
        today = live_day_total(snapshot.daily_usages, now)
        daily_harnesses = {row.harness.casefold() for row in snapshot.daily_usages
                          if live_day_total((row,), now) is not None}
        today_label = "TODAY UTC / LIVE CODEX" if daily_harnesses == {"codex"} else "TODAY UTC / LIVE FEED"
        streams = canonical_usages(snapshot, now).values()
        rate_ready = any(observed_output_rate(u, now) is not None for u in streams)
        rate = combined_token_rate(snapshot, now)
        rate_value = f"{compact_number(rate, 1)}/s" if rate_ready or rate > 0 else "—"
        if snapshot.usagemax_stats is not None:
            self._usagemax_panel(draw, snapshot.usagemax_stats, now, rate_value, today, today_label)
            return
        stats = snapshot.token_stats or TokenStats()
        models = active_model_ingest(snapshot, now)
        rate = combined_token_rate(snapshot, now)
        distinct = len({row.model for row in models})
        self._usage_hero(draw, "LOCAL SESSION TOKENS", stats.total_tokens,
                         "ACCOUNT SYNC", "PENDING", BRAND_ORANGE)
        cells = (
            ("OUTPUT / S · EST.", rate_value, MAGENTA),
            ("OBSERVED SESSIONS", f"{len(models):02d}", CYAN),
            ("PARALLEL MODELS", f"{distinct:02d}", GREEN),
            (today_label, compact_number(today, 1) if today is not None else "—", PURPLE),
        )
        for index, (label, value, color) in enumerate(cells):
            x, y = (index % 2) * 200, 145 + (index // 2) * 80
            self._stat_cell(draw, (x, y, x + 200, y + 80), label, value, color, True)
        draw.rectangle((0, 306, 399, 419), fill="#03060D")
        draw.text((19, 315), "Live throughput", fill=MUTED, font=self.ui_small)
        draw.text((382, 315), "LOCAL", fill=MUTED, font=self.nano_bold, anchor="ra")
        self._pulse_chart(draw, tuple(self.rate_history), (19, 342, 382, 391), MAGENTA)
        draw.text((19, 399), fit_text(draw, stats.top_model.upper(), self.nano_bold, 363),
                  fill=PURPLE, font=self.nano_bold)

    def _usagemax_panel(self, draw: ImageDraw.ImageDraw, stats: UsageMaxStats,
                        now: float | None = None, rate_value: str = "—",
                        live_today: int | None = None, today_label: str = "TODAY · LIVE HOSTS") -> None:
        """Account momentum; local activity alone still owns animation and pet input."""
        now = time.time() if now is None else now
        week = stats.window_tokens(now, 7)
        month = stats.window_tokens(now, 28)
        today = stats.today_tokens(now)
        if today is not None:
            today_label = "TODAY'S TOKENS / UTC"
        else:
            today = live_today
            if today is None:
                today_label = "TODAY UTC / NO DATA"
        cost_label = {
            "reported": "REPORTED USD", "estimated": "ESTIMATED USD",
            "api-equivalent": "API EQUIV. USD", "mixed": "MIXED COST USD",
        }.get(stats.cost_basis, "USD / UNKNOWN")
        cost = (f"${stats.total_spend:.2f}" if stats.total_spend < 1000 else
                f"${compact_number(stats.total_spend, 1)}") if stats.total_spend is not None else "—"
        self._usage_hero(draw, "LIFETIME TOKENS", stats.total_tokens, cost_label, cost, BRAND_ORANGE)
        cells = (
            ("7D TOKENS / REPORTED", compact_number(sum(v for v in week if v is not None), 1) if week and any(v is not None for v in week) else "—", MAGENTA),
            ("PEAK REPORTED / 28D", compact_number(max(v for v in month if v is not None), 1) if month and any(v is not None for v in month) else "—", CYAN),
            ("OUTPUT / S · EST.", rate_value, GREEN),
            (today_label, compact_number(today, 1) if today is not None else "—", PURPLE),
        )
        for index, (label, value, color) in enumerate(cells):
            x, y = (index % 2) * 200, 145 + (index // 2) * 80
            self._stat_cell(draw, (x, y, x + 200, y + 80), label, value, color, True)
        draw.rectangle((0, 306, 399, 419), fill="#03060D")
        draw.text((16, 315), "Token history / 28 days", fill=MUTED, font=self.ui_small)
        draw.text((384, 316), "UTC", fill=MUTED, font=self.nano_bold, anchor="ra")
        if month is None:
            draw.text((16, 358), "DAILY DATA UNAVAILABLE", fill=MUTED, font=self.micro)
        else:
            self._pulse_chart(draw, month, (16, 342, 384, 391), MAGENTA)
        if stats.top_model:
            label = "Top cost" if stats.top_model_metric == "spend" else "Top tokens" if stats.top_model_metric == "tokens" else "Account model"
            identity = fit_identifier(draw, f"{label}  {stats.top_model}", self.ui_caption, 368)
            draw.text((16, 399), identity, fill=MUTED, font=self.ui_caption)

    def _pulse_chart(self, draw: ImageDraw.ImageDraw, values: tuple[float, ...],
                     box: tuple[int, int, int, int], color: str) -> None:
        left, top, right, bottom = box
        maximum = max((v for v in values if v is not None), default=0) or 1
        for y in (top, (top + bottom) // 2, bottom):
            draw.line((left, y, right, y), fill=GRID_SOFT)
        slot = (right - left) / max(1, len(values))
        for index, value in enumerate(values):
            x = round(left + index * slot)
            end = max(x, round(left + (index + 1) * slot) - 4)
            if value is None:
                draw.line((x, bottom-5, end, bottom-5), fill=AMBER)
                continue
            height = max(1, round(value / maximum * (bottom - top)))
            if value <= 0:
                draw.line((x, bottom, end, bottom), fill=GRID)
                continue
            tone = BRAND_ORANGE if index == len(values) - 1 else color
            draw.rectangle((x, bottom - height, end, bottom), fill=NEON_DIM[tone])
            draw.rectangle((x, bottom - height, end, bottom - height + min(3, height)), fill=tone)
            draw.line((x, bottom - height, x, bottom), fill=tone)

    def _daily_bars(
        self, draw: ImageDraw.ImageDraw, stats: TokenStats, box: tuple[int, int, int, int]
    ) -> None:
        x, y, right, bottom = box
        rows = list(stats.daily)[-28:]
        if not rows:
            draw.line((x, bottom, right, bottom), fill=GRID)
            draw.text(((x + right) // 2, y + 10), "WAITING FOR LOCAL AGENT ACTIVITY", fill=MUTED,
                      font=self.micro, anchor="ma")
            return
        maximum = max(float(row.get("tokens", 0)) for row in rows) or 1
        slot = (right - x) / 28
        by_date = {str(row.get("date")): row for row in rows}
        first_day = date.today() - timedelta(days=27)
        for index in range(28):
            key = (first_day + timedelta(days=index)).isoformat()
            row = by_date.get(key, {"tokens": 0, "models": {}})
            total = float(row.get("tokens", 0))
            height = max(1, int((total / maximum) * (bottom - y))) if total else 1
            xx = int(x + index * slot + 2)
            bar_right = max(xx + 2, int(x + (index + 1) * slot - 2))
            models = row.get("models", {}) if isinstance(row.get("models"), dict) else {}
            cursor = bottom
            if total > 0 and models:
                for model, tokens in sorted(models.items(), key=lambda item: item[1], reverse=True):
                    segment = max(1, int(height * float(tokens) / total))
                    draw.rectangle((xx, max(y, cursor - segment), bar_right, cursor),
                                   fill=self._model_color(str(model)))
                    cursor -= segment
                    if cursor <= y:
                        break
            else:
                draw.rectangle((xx, bottom - 1, bar_right, bottom), fill=GRID)

    def _live(self, image, draw, snapshot, now) -> None:
        records = snapshot.otel_records
        if not any(0 <= now-r.timestamp < 300 for r in records):
            records = tuple(OtelRecord(e.timestamp, e.source, e.message, kind="LOG", host=e.source,
                            status="ERR" if e.level in {"ERROR", "ERR"} else "WRN" if e.level in {"WARN", "WRN"} else "OK")
                            for e in snapshot.events)
        self.activity_feed.update(records, now)
        self._agent_space(image, draw, snapshot, (), now, (AGGREGATE_RIGHT, 44, LIVE_SPLIT, 420))
        self._otel_log(image, draw, now, (LIVE_SPLIT, 44, PET_LEFT, 420))

    def _flow_link(
        self, draw: ImageDraw.ImageDraw, start: tuple[int, int], end: tuple[int, int],
        color: str, active: bool, now: float, phase: float, intensity: float = 0.0,
    ) -> None:
        dim = NEON_DIM.get(color, GRID_SOFT) if active else GRID_SOFT
        bend = max(6, min(24, abs(end[1] - start[1]) // 3))
        control = ((start[0] + end[0]) / 2, min(start[1], end[1]) - bend)

        def point(progress: float) -> tuple[int, int]:
            inverse = 1.0 - progress
            return (
                int(inverse * inverse * start[0] + 2 * inverse * progress * control[0] + progress * progress * end[0]),
                int(inverse * inverse * start[1] + 2 * inverse * progress * control[1] + progress * progress * end[1]),
            )

        route = [point(step / 16) for step in range(17)]
        draw.line(route, fill=dim, width=5 if active else 2, joint="curve")
        draw.line(route, fill=color if active else GRID, width=1, joint="curve")
        if not active:
            return
        energy = max(0.2, min(1.0, intensity))
        speed = 0.13 + 0.14 * energy
        packet_count = 2 if energy >= 0.72 else 1
        for packet_index in range(packet_count):
            packet_phase = phase + packet_index / packet_count
            progress = (now * speed + packet_phase) % 1 if self.motion_enabled else 0.55
            for trail_index, lag in enumerate((0.105, 0.07, 0.035), start=1):
                trail_progress = max(0.0, progress - lag)
                tx, ty = point(trail_progress)
                radius = max(1, 4 - trail_index)
                draw.ellipse((tx - radius, ty - radius, tx + radius, ty + radius), fill=dim)
            x, y = point(progress)
            draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=dim)
            draw.ellipse((x - 2, y - 2, x + 2, y + 2), fill=TEXT)

    @staticmethod
    def _mix_rgb(first: tuple[int, int, int], second: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
        return tuple(
            int(first[index] + (second[index] - first[index]) * amount)
            for index in range(3)
        )

    def _agent_core_sprite(self, color: str, active: bool, parent: bool) -> Image.Image:
        effective = color if active else GRID
        key = (effective, active, parent)
        cached = self.agent_core_sprites.get(key)
        if cached is not None:
            return cached
        scale = 3
        logical_size = 64 if parent else 54
        size = logical_size * scale
        center = size // 2
        radius = (13 if parent else 10) * scale
        accent = ImageColor.getrgb(effective)
        dark = (3, 10, 17)
        sprite = Image.new("RGBA", (size, size), (0, 0, 0, 0))

        glow_layer = Image.new("RGBA", sprite.size, (0, 0, 0, 0))
        glow = ImageDraw.Draw(glow_layer)
        glow_radius = radius + (9 if parent else 7) * scale
        glow.ellipse(
            (center - glow_radius, center - glow_radius, center + glow_radius, center + glow_radius),
            fill=(*accent, 86 if active else 28),
        )
        glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(6 * scale))
        sprite = Image.alpha_composite(sprite, glow_layer)

        body = ImageDraw.Draw(sprite)
        if parent:
            body.ellipse(
                (center - radius - 9 * scale, center - radius // 2,
                 center + radius + 9 * scale, center + radius // 2),
                outline=(*accent, 150 if active else 75), width=scale,
            )
        else:
            diamond = (
                (center, center - radius - 7 * scale),
                (center + radius + 7 * scale, center),
                (center, center + radius + 7 * scale),
                (center - radius - 7 * scale, center),
            )
            body.line((*diamond, diamond[0]), fill=(*accent, 140 if active else 65), width=scale)

        for ring in range(radius, 0, -scale):
            depth = 1 - ring / radius
            tone = self._mix_rgb(dark, accent, 0.16 + depth * (0.62 if active else 0.18))
            body.ellipse((center - ring, center - ring, center + ring, center + ring), fill=(*tone, 255))
        body.ellipse(
            (center - radius, center - radius, center + radius, center + radius),
            outline=(*accent, 255 if active else 150), width=2 * scale,
        )
        body.arc(
            (center - radius + 3 * scale, center - radius + 3 * scale,
             center + radius - 3 * scale, center + radius - 3 * scale),
            205, 322, fill=(242, 251, 255, 240 if active else 130), width=2 * scale,
        )
        highlight = max(2 * scale, radius // 4)
        body.ellipse(
            (center - radius // 3 - highlight, center - radius // 3 - highlight,
             center - radius // 3 + highlight, center - radius // 3 + highlight),
            fill=(245, 253, 255, 235 if active else 130),
        )
        sprite = sprite.resize((logical_size, logical_size), Image.Resampling.LANCZOS)
        self.agent_core_sprites[key] = sprite
        return sprite

    def _agent_ship_sprite(
        self, color: str, active: bool, carrier: bool, facing: str,
    ) -> Image.Image:
        effective = color if active else GRID
        key = (effective, active, carrier, facing)
        cached = self.agent_ship_sprites.get(key)
        if cached is not None:
            return cached
        scale = 3
        # The swarm viewport has fifty fixed bays. These stay deliberately
        # small so density adds visual energy instead of forcing a relayout.
        logical_size = (38, 18) if carrier else (24, 14)
        width, height = (value * scale for value in logical_size)
        center_y = height // 2
        accent = ImageColor.getrgb(effective)
        sprite = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        glow_layer = Image.new("RGBA", sprite.size, (0, 0, 0, 0))
        glow = ImageDraw.Draw(glow_layer)

        if carrier:
            hull = (
                (2 * scale, center_y), (8 * scale, 3 * scale),
                (27 * scale, 2 * scale), (36 * scale, center_y),
                (27 * scale, height - 2 * scale), (8 * scale, height - 3 * scale),
            )
            glow.polygon(hull, fill=(*accent, 92 if active else 28))
        else:
            hull = (
                (2 * scale, center_y), (8 * scale, 3 * scale),
                (22 * scale, center_y), (8 * scale, height - 3 * scale),
            )
            glow.polygon(hull, fill=(*accent, 82 if active else 24))
        glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(4 * scale))
        sprite = Image.alpha_composite(sprite, glow_layer)
        body = ImageDraw.Draw(sprite)

        body.polygon(hull, fill=(4, 12, 20, 248), outline=(*accent, 255 if active else 145))
        if carrier:
            inner = (
                (8 * scale, center_y), (13 * scale, 5 * scale),
                (26 * scale, 4 * scale), (32 * scale, center_y),
                (26 * scale, height - 4 * scale), (13 * scale, height - 5 * scale),
            )
            body.polygon(inner, fill=(*self._mix_rgb((3, 10, 18), accent, 0.18 if active else 0.06), 255),
                         outline=(*accent, 190 if active else 90))
            body.polygon(
                ((14 * scale, center_y), (18 * scale, 5 * scale),
                 (26 * scale, center_y), (18 * scale, height - 5 * scale)),
                fill=(*self._mix_rgb((7, 17, 26), accent, 0.48 if active else 0.12), 255),
                outline=(238, 250, 255, 210 if active else 90),
            )
            body.line((7 * scale, center_y, 2 * scale, center_y),
                      fill=(*accent, 255 if active else 110), width=2 * scale)
            body.line((11 * scale, 4 * scale, 11 * scale, height - 4 * scale),
                      fill=(*accent, 130 if active else 55), width=scale)
            body.line((28 * scale, 4 * scale, 28 * scale, height - 4 * scale),
                      fill=(*accent, 150 if active else 55), width=scale)
        else:
            body.polygon(
                ((7 * scale, center_y), (11 * scale, 4 * scale),
                 (19 * scale, center_y), (11 * scale, height - 4 * scale)),
                fill=(*self._mix_rgb((5, 13, 20), accent, 0.42 if active else 0.1), 255),
                outline=(240, 251, 255, 190 if active else 80),
            )
            body.line((7 * scale, center_y, 2 * scale, center_y),
                      fill=(*accent, 240 if active else 100), width=2 * scale)

        sprite = sprite.resize(logical_size, Image.Resampling.LANCZOS)
        if facing == "left":
            sprite = sprite.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        self.agent_ship_sprites[key] = sprite
        return sprite

    def _warm_agent_assets(self) -> None:
        field_size = (LIVE_SPLIT - AGGREGATE_RIGHT - 2, TOPOLOGY_BOTTOM - 84)
        self._agent_field_plate(field_size)
        for color in PALETTE:
            self._agent_core_sprite(color, True, False)
            self._agent_core_sprite(color, True, True)
        self._agent_core_sprite(GRID, False, False)
        self._agent_core_sprite(GRID, False, True)

    def _agent_core(
        self, image: Image.Image, draw: ImageDraw.ImageDraw, position: tuple[int, int], label: str,
        model: str, color: str, active: bool, parent: bool, now: float, phase: float,
        intensity: float = 0.0,
    ) -> None:
        x, y = position
        radius = 13 if parent else 10
        drift = int(round(math.sin(now * 0.7 + phase) * 1.5)) if self.motion_enabled and active else 0
        y += drift
        core_color = color if active else GRID
        halo = NEON_DIM.get(color, GRID_SOFT) if active else GRID_SOFT
        draw.ellipse((x - radius - 10, y + radius - 1, x + radius + 10, y + radius + 8), fill="#020508")
        draw.line((x, y + radius, x, y + radius + 8), fill=halo, width=2)
        if active:
            orbit_radius = radius + 13
            orbit_phase = int((now * 24 + phase * 57) % 360) if self.motion_enabled else 210
            orbit_extent = 64 + int(max(0.0, min(1.0, intensity)) * 88)
            draw.arc(
                (x - orbit_radius, y - orbit_radius // 2,
                 x + orbit_radius, y + orbit_radius // 2),
                orbit_phase, orbit_phase + orbit_extent, fill=core_color, width=1,
            )
        sprite = self._agent_core_sprite(color, active, parent)
        image.paste(sprite, (x - sprite.width // 2, y - sprite.height // 2), sprite)
        if active:
            orbit = now * (0.42 + 0.28 * max(0.0, min(1.0, intensity))) + phase
            satellite_x = int(x + math.cos(orbit) * (radius + 13))
            satellite_y = int(y + math.sin(orbit) * (radius // 2 + 5))
            draw.ellipse((satellite_x - 2, satellite_y - 2,
                          satellite_x + 2, satellite_y + 2), fill=TEXT)
            if intensity >= 0.65:
                sweep = int((now * 34 + phase * 57) % 360)
                draw.arc((x - radius - 8, y - radius - 8, x + radius + 8, y + radius + 8),
                         sweep, sweep + 72, fill=core_color, width=2)
        prefix = "ROOT" if parent else "SUB"
        draw.text((x, y + radius + 10), fit_text(draw, f"{prefix}/{label.upper()}", self.nano_bold, 116),
                  fill=core_color if active else MUTED, font=self.nano_bold, anchor="ma", stroke_width=1, stroke_fill="#010407")
        if model:
            draw.text((x, y + radius + 24), fit_text(draw, model.upper().replace("GPT-", ""), self.nano, 108),
                      fill=MUTED if active else GRID, font=self.nano, anchor="ma",
                      stroke_width=1, stroke_fill="#010407")

    def _agent_field_plate(self, size: tuple[int, int]) -> Image.Image:
        cached = self.agent_field_plates.get(size)
        if cached is not None:
            return cached
        width, height = size
        scale = 2
        scene_size = (width * scale, height * scale)
        plate = Image.new("RGB", scene_size, "#02070C")
        paint = ImageDraw.Draw(plate)
        for y in range(scene_size[1]):
            depth = y / max(1, scene_size[1] - 1)
            paint.line(
                (0, y, scene_size[0], y),
                fill=(2 + int(3 * depth), 6 + int(11 * depth), 10 + int(19 * depth)),
            )
        vignette = Image.new("RGBA", scene_size, (0, 0, 0, 0))
        shade = ImageDraw.Draw(vignette)
        edge_width = max(1, scene_size[0] // 5)
        for x in range(edge_width):
            alpha = int(88 * (1 - x / edge_width) ** 2)
            shade.line((x, 0, x, scene_size[1]), fill=(0, 0, 0, alpha))
            shade.line((scene_size[0] - x - 1, 0, scene_size[0] - x - 1, scene_size[1]),
                       fill=(0, 0, 0, alpha))

        shafts = Image.new("RGBA", scene_size, (0, 0, 0, 0))
        shaft_draw = ImageDraw.Draw(shafts)
        horizon = 26 * scale
        vanishing = (scene_size[0] // 2, horizon)
        shaft_draw.polygon(
            ((vanishing[0] - 26 * scale, horizon), (vanishing[0] - 150 * scale, scene_size[1]),
             (vanishing[0] - 72 * scale, scene_size[1]), (vanishing[0] - 8 * scale, horizon)),
            fill=(37, 244, 255, 10),
        )
        shaft_draw.polygon(
            ((vanishing[0] + 18 * scale, horizon), (vanishing[0] + 92 * scale, scene_size[1]),
             (vanishing[0] + 176 * scale, scene_size[1]), (vanishing[0] + 40 * scale, horizon)),
            fill=(195, 141, 255, 9),
        )
        plate = Image.alpha_composite(plate.convert("RGBA"), vignette).convert("RGB")
        plate = Image.alpha_composite(plate.convert("RGBA"), shafts).convert("RGB")
        paint = ImageDraw.Draw(plate)
        randomizer = random.Random(5408)
        for _ in range(68):
            x = randomizer.randrange(8 * scale, max(9 * scale, scene_size[0] - 8 * scale))
            y = randomizer.randrange(5 * scale, max(6 * scale, scene_size[1] - 8 * scale))
            if randomizer.random() < 0.74 and y > horizon + 18 * scale:
                continue
            tone = randomizer.choice(("#244559", "#305C70", "#316A72", "#47385F"))
            radius = 1 if randomizer.random() < 0.84 else 2
            paint.ellipse((x - radius, y - radius, x + radius, y + radius), fill=tone)

        atmosphere = Image.new("RGB", scene_size, "#000000")
        glow = ImageDraw.Draw(atmosphere)
        glow.ellipse((vanishing[0] - 200 * scale, horizon - 26 * scale,
                      vanishing[0] + 200 * scale, horizon + 26 * scale), fill="#073C49")
        glow.rectangle((vanishing[0] - 2 * scale, 0, vanishing[0] + 2 * scale, scene_size[1]),
                       fill="#082531")
        atmosphere = atmosphere.filter(ImageFilter.GaussianBlur(18 * scale))
        plate = ImageChops.screen(plate, atmosphere)
        paint = ImageDraw.Draw(plate)

        emissive = Image.new("RGB", scene_size, "#000000")
        emission = ImageDraw.Draw(emissive)
        portal_box = (
            vanishing[0] - 210 * scale, horizon - 92 * scale,
            vanishing[0] + 210 * scale, horizon + 104 * scale,
        )
        emission.arc(portal_box, 194, 346, fill="#0A6974", width=3 * scale)
        emission.line((0, horizon, scene_size[0], horizon), fill="#063845", width=2 * scale)
        emissive_glow = emissive.filter(ImageFilter.GaussianBlur(5 * scale))
        plate = ImageChops.screen(plate, emissive_glow)
        paint = ImageDraw.Draw(plate)
        paint.arc(portal_box, 194, 346, fill="#1A6A77", width=scale)
        paint.arc(
            (vanishing[0] - 170 * scale, horizon - 72 * scale,
             vanishing[0] + 170 * scale, horizon + 84 * scale),
            196, 344, fill="#3D2C59", width=scale,
        )
        paint.line((0, horizon, scene_size[0], horizon), fill="#135566", width=scale)

        pylon_color = "#112B3E"
        for side in (-1, 1):
            outer = vanishing[0] + side * (width // 2 - 20) * scale
            inner = vanishing[0] + side * (width // 2 - 74) * scale
            paint.polygon(
                ((outer, scene_size[1]), (inner, scene_size[1]),
                 (vanishing[0] + side * 190 * scale, horizon + 6 * scale),
                 (vanishing[0] + side * 212 * scale, horizon + 6 * scale)),
                outline=pylon_color,
            )

        paint.line((16 * scale, scene_size[1] - 2 * scale,
                    scene_size[0] - 16 * scale, scene_size[1] - 2 * scale),
                   fill="#183047", width=scale)
        for index in range(9):
            x = 18 * scale + int((scene_size[0] - 36 * scale) * index / 8)
            color = "#15374B" if index in {3, 4, 5} else "#10273A"
            paint.line((x, scene_size[1] - 2 * scale, vanishing[0], horizon),
                       fill=color, width=scale)
        for depth in (0.12, 0.26, 0.43, 0.62, 0.82):
            projected = depth * depth
            y = int(horizon + (scene_size[1] - horizon) * projected)
            inset = int((1 - projected) * scene_size[0] * 0.43)
            paint.line((inset, y, scene_size[0] - inset, y), fill="#112E43", width=scale)
        for ring_depth in (0.48, 0.70, 0.90):
            projected = ring_depth * ring_depth
            y = int(horizon + (scene_size[1] - horizon) * projected)
            half = int(42 * scale * ring_depth)
            height_ring = max(scale, int(5 * scale * ring_depth))
            paint.ellipse((vanishing[0] - half, y - height_ring,
                           vanishing[0] + half, y + height_ring),
                          outline="#12485A", width=scale)
        for y in range(horizon + 8 * scale, scene_size[1], 4 * scale):
            paint.line((0, y, scene_size[0], y), fill="#050E17", width=scale)
        plate = ImageEnhance.Color(plate).enhance(1.5)
        plate = ImageEnhance.Contrast(plate).enhance(1.2)
        plate = plate.resize(size, Image.Resampling.LANCZOS)
        self.agent_field_plates[size] = plate
        return plate

    def _agent_field_bloom(
        self, image: Image.Image, box: tuple[int, int, int, int], now: float,
    ) -> None:
        if self.agent_fx_quality == "lean":
            return
        left, top, right, bottom = box
        crop_box = (left + 1, top + 40, right - 1, bottom)
        source = image.crop(crop_box)
        effects_hz = 10 if self.agent_fx_quality == "cinematic" else 6
        refresh = (
            self.agent_bloom_overlay is None
            or self.agent_bloom_overlay.size != source.size
            or now - self.agent_bloom_updated_at >= 1 / effects_hz
        )
        if refresh:
            fx_size = (max(1, source.width // 3), max(1, source.height // 3))
            effects_source = source.resize(fx_size, Image.Resampling.BILINEAR)
            mask = effects_source.convert("L").point(BLOOM_THRESHOLD_LUT)
            emissive = ImageChops.multiply(
                effects_source, Image.merge("RGB", (mask, mask, mask))
            )
            radius = 0.9 if self.agent_fx_quality == "cinematic" else 0.6
            bloom = emissive.filter(ImageFilter.GaussianBlur(radius))
            if self.agent_fx_quality == "cinematic":
                bloom = ImageChops.screen(
                    bloom, emissive.filter(ImageFilter.GaussianBlur(1.9))
                )
            self.agent_bloom_overlay = bloom.resize(source.size, Image.Resampling.BILINEAR)
            self.agent_bloom_updated_at = now
        assert self.agent_bloom_overlay is not None
        graded = ImageChops.screen(source, self.agent_bloom_overlay)
        image.paste(Image.blend(source, graded, 0.42), crop_box)

    @staticmethod
    def _agent_seed(value: str) -> int:
        """Return a repeatable small hash without Python's randomized hash()."""
        result = 2166136261
        for byte in value.encode("utf-8", errors="ignore"):
            result ^= byte
            result = (result * 16777619) & 0xFFFFFFFF
        return result

    @staticmethod
    def _swarm_slot_position(
        slot: int, box: tuple[int, int, int, int],
    ) -> tuple[int, int]:
        """Project one of fifty permanent bays into the wide flight deck."""
        left, top, right, _bottom = box
        row, column = divmod(max(0, min(49, slot)), 10)
        arena_top = top + 40
        row_y = (17, 47, 78, 110, 143)[row]
        inset = (70, 50, 32, 18, 8)[row]
        usable = right - left - 2 * inset - 2
        x = left + 1 + inset + int(round(usable * column / 9))
        return x, arena_top + row_y

    def _assign_swarm_slots(
        self, nodes: list[dict[str, Any]], now: float,
    ) -> list[dict[str, Any]]:
        """Keep agent placement stable and dock new children near their parent."""
        current = {str(node["key"]) for node in nodes}
        for key in current:
            self.agent_slot_seen[key] = now
        for key in tuple(self.agent_slot_by_key):
            if key not in current and now - self.agent_slot_seen.get(key, 0.0) > 45:
                self.agent_slot_by_key.pop(key, None)
                self.agent_slot_seen.pop(key, None)

        ranked = sorted(
            nodes,
            key=lambda node: (
                bool(node.get("active")),
                float(node.get("updated", 0.0)),
                str(node["key"]) in self.agent_slot_by_key,
            ),
            reverse=True,
        )[:50]
        selected = {str(node["key"]) for node in ranked}
        assigned_selected = sum(key in self.agent_slot_by_key for key in selected)
        unassigned = len(selected) - assigned_selected
        free_slots = 50 - len({
            slot for slot in self.agent_slot_by_key.values() if 0 <= slot < 50
        })
        if unassigned > free_slots:
            stale = sorted(
                (key for key in self.agent_slot_by_key if key not in selected),
                key=lambda key: self.agent_slot_seen.get(key, 0.0),
            )
            for key in stale[:unassigned - free_slots]:
                self.agent_slot_by_key.pop(key, None)
                self.agent_slot_seen.pop(key, None)
        occupied = {
            slot: key for key, slot in self.agent_slot_by_key.items()
            if 0 <= slot < 50
        }

        pending = sorted(
            (node for node in ranked if str(node["key"]) not in self.agent_slot_by_key),
            key=lambda node: (not bool(node.get("carrier")), -float(node.get("updated", 0.0))),
        )
        for node in pending:
            key = str(node["key"])
            parent_key = str(node.get("parent") or "")
            parent_slot = self.agent_slot_by_key.get(parent_key)
            seed = self._agent_seed(key)
            if parent_slot is not None:
                parent_row, parent_column = divmod(parent_slot, 10)
                candidates = sorted(
                    range(50),
                    key=lambda slot: (
                        abs(slot // 10 - parent_row) + abs(slot % 10 - parent_column),
                        (slot - seed) % 50,
                    ),
                )
            else:
                start = seed % 50
                candidates = [(start + offset * 17) % 50 for offset in range(50)]
            chosen = next((slot for slot in candidates if slot not in occupied), None)
            if chosen is None:
                continue
            self.agent_slot_by_key[key] = chosen
            occupied[chosen] = key

        return sorted(
            (node for node in ranked if str(node["key"]) in self.agent_slot_by_key),
            key=lambda node: self.agent_slot_by_key[str(node["key"])],
        )

    def _constellation_plate(self, size):
        if size in self.graph_plates:
            return self.graph_plates[size]
        width, height = size
        # Low-resolution light field is baked once; all lettering stays native.
        small = Image.new("RGB", (width // 4, height // 4))
        pixels = small.load()
        for y in range(small.height):
            for x in range(small.width):
                nx, ny = x / small.width, y / small.height
                violet = math.exp(-((nx - .3) ** 2 * 14 + (ny - .35) ** 2 * 9))
                pink = math.exp(-((nx - .78) ** 2 * 22 + (ny - .7) ** 2 * 12))
                pixels[x, y] = (int(3 + 12 * violet + 10 * pink), int(4 + 4 * violet), int(11 + 19 * violet + 9 * pink))
        plate = small.resize(size, Image.Resampling.BICUBIC)
        paint = ImageDraw.Draw(plate)
        rng = random.Random(809)
        for index in range(105):
            x, y = rng.randrange(10, width - 10), rng.randrange(5, height - 5)
            tone = "#5F527E" if index % 9 == 0 else "#26283E"
            paint.point((x, y), fill=tone)
            if index % 19 == 0:
                paint.line((x - 2, y, x + 2, y), fill=tone)
                paint.line((x, y - 2, x, y + 2), fill=tone)
        for i in range(10):
            x = round(width * i / 9)
            paint.line((width // 2, height // 2, x, height), fill="#151425")
        for y in (height - 8, height - 24, height - 46):
            paint.line((0, y, width, y), fill="#191528")
        self.graph_plates[size] = plate
        return plate

    def _constellation_dot(self, color, active, parent):
        key = (color, active, parent)
        if key in self.graph_dots:
            return self.graph_dots[key]
        scale, size = 3, 24
        sprite = Image.new("RGBA", (size * scale, size * scale))
        glow = ImageDraw.Draw(sprite)
        rgb = ImageColor.getrgb(color)
        center = size * scale // 2
        if active:
            glow.ellipse((center - 6 * scale, center - 6 * scale, center + 6 * scale, center + 6 * scale), fill=(*rgb, 115))
            sprite = sprite.filter(ImageFilter.GaussianBlur(3 * scale))
        paint = ImageDraw.Draw(sprite)
        radius = (5 if parent else 3) * scale
        if parent:
            paint.polygon(((center, center - radius - 3), (center + radius + 3, center),
                           (center, center + radius + 3), (center - radius - 3, center)),
                          fill=(*rgb, 230 if active else 30), outline=(*rgb, 255))
        else:
            paint.ellipse((center - radius, center - radius, center + radius, center + radius),
                          fill=(*rgb, 230 if active else 25), outline=(*rgb, 255 if active else 130), width=scale)
        if active:
            paint.ellipse((center - scale, center - scale, center + scale, center + scale), fill="#FFFFFF")
        sprite = sprite.resize((size, size), Image.Resampling.LANCZOS)
        self.graph_dots[key] = sprite
        return sprite

    def _map_ribbon(self, draw, start, end, color, thickness, active, now, phase, focus=False):
        """A translucent membership ribbon; packet motion requires fresh activity."""
        a, b = start, end
        dx = (b[0] - a[0]) * .52
        def point(t):
            u = 1 - t
            return (round(u*u*u*a[0] + 3*u*u*t*(a[0]+dx) + 3*u*t*t*(b[0]-dx) + t*t*t*b[0]),
                    round(u*u*u*a[1] + 3*u*u*t*a[1] + 3*u*t*t*b[1] + t*t*t*b[1]))
        route = [point(i/28) for i in range(29)]
        rgb = ImageColor.getrgb(color)
        def shade(amount):
            return tuple(round(v * amount) for v in rgb)
        draw.line(route, fill=shade(.2 if active else .1), width=thickness + 5, joint="curve")
        draw.line(route, fill=shade(.4 if active else .22), width=thickness, joint="curve")
        draw.line(route, fill=shade(1 if focus else .78 if active else .44), width=1, joint="curve")
        if active and self.motion_enabled:
            progress = (now * .23 + phase) % 1
            tail = [point(max(0, progress - .09 + i * .009)) for i in range(11)]
            draw.line(tail, fill=shade(.9 if focus else .68), width=2, joint="curve")
            x, y = point(progress)
            draw.ellipse((x-1, y-1, x+1, y+1), fill=TEXT)

    def _agent_space(self, image, draw, snapshot, models, now, box) -> None:
        left, top, right, bottom = box
        graph = self.constellation
        canvas = "#03060D"
        narrow = right - left < 1000
        model_x = 300 if narrow else 434
        model_label_x = model_x + 16
        model_width = 210 if narrow else 224
        agent_x = right - left - 192 if narrow else 714
        draw.rectangle(box, fill=canvas)
        draw.line((left, top + 14, left, bottom - 12), fill="#27303D")
        draw.text((left + 16, top + 12), "Execution map", fill=TEXT, font=self.ui_title)
        nodes = graph.nodes
        recent = sum(node.active for node in nodes)
        summary = f"{recent} recent · {len(nodes)} agents · {len(graph.clusters)} models"
        draw.text((right - 28, top + 18), summary, fill=MUTED, font=self.ui_small, anchor="ra")
        draw.ellipse((right - 20, top + 24, right - 15, top + 29), fill=GREEN if recent else GRID)
        layout = flow_layout(graph, now, width=right-left)
        body_top = top + 77
        draw.line((left+16, top+64, right-16, top+64), fill="#252D40")
        for x, title in ((left + 16, "HARNESS / HOST"), (left + model_x, "MODEL / OUTPUT EST."), (left + agent_x, "AGENTS")):
            draw.text((x, top + 48), title, fill="#96A9C0", font=self.ui_caption)
        draw.text((right - 16, top + 48), f"{sum(len(lane.nodes) for lane in layout.lanes)} / {len(nodes)} shown",
                  fill="#96A9C0", font=self.ui_caption, anchor="ra")
        points = {node.key: (round(left + x), round(body_top + y)) for lane in layout.lanes for node, x, y in lane.nodes}
        shown_nodes = {node.key: node for lane in layout.lanes for node, _, _ in lane.nodes}
        keys = sorted(shown_nodes)
        selected = shown_nodes[keys[int(now // 6) % len(keys)]] if keys else None
        # Four named source anchors; overflow is an explicitly counted aggregate.
        sources = list(layout.sources[:4])
        if len(layout.sources) > 4:
            sources.append(((f"+{len(layout.sources)-4} harness/host pairs", "Grouped sources"),
                            tuple(node for _, group in layout.sources[4:] for node in group)))
        source_y = [body_top + (i+.5)*240/max(1,len(sources)) for i in range(len(sources))]
        for source_index, ((harness, host), members) in enumerate(sources):
            sy = round(source_y[source_index])
            per_model = {}
            for node in members:
                per_model.setdefault(node.model_key, []).append(node)
            for lane in layout.lanes:
                group = per_model.get(lane.cluster.key, ())
                if not group:
                    continue
                cy = round(body_top + lane.center)
                color = self._model_color(lane.cluster.model)
                thickness = max(3, min(22, round(3 + math.sqrt(len(group)) * 2)))
                self._map_ribbon(draw, (left + 180, sy), (left + model_x - 4, cy), color, thickness,
                                 any(node.active for node in group), now, source_index * .21 + lane.center / 400,
                                 selected is not None and selected in group)
        # Model membership fans outward. A model is a grouping, never a parent.
        for lane in layout.lanes:
            cy = round(body_top + lane.center)
            color = self._model_color(lane.cluster.model)
            for index, (node, x, y) in enumerate(lane.nodes):
                self._map_ribbon(draw, (left + model_x + 6, cy), (round(left + x), round(body_top + y)), color,
                                 2, node.active, now, index * .17 + lane.center / 300, node == selected)
        # Only real delegation uses the fine dotted overlay.
        parents = {start for start, _ in graph.edges}
        visible_edges = 0
        for start, end in graph.edges:
            if start not in points or end not in points:
                continue
            visible_edges += 1
            a, b = points[start], points[end]
            accent = "#F1DCA6" if selected and selected.key in (start,end) else "#657283"
            route = []
            for i in range(33):
                t = i/32
                route.append((round(a[0]*(1-t)+b[0]*t + math.sin(math.pi*t)*25), round(a[1]*(1-t)+b[1]*t)))
            for i in range(0, 31, 3):
                draw.line(route[i:i+2], fill=accent, width=1)
            draw.line(delegation_arrow(route[-2], b), fill=accent)
        # Clear type sits above the light paths, all at a readable native scale.
        for source_index, ((harness, host), members) in enumerate(sources):
            sy = round(source_y[source_index])
            active = sum(node.active for node in members)
            draw.rounded_rectangle((left+16, sy-22, left+181, sy+22), radius=4, fill="#0D141F")
            draw.line((left+179, sy-17, left+179, sy+17), fill=TEXT if active else GRID, width=3)
            draw.text((left+28, sy-18), fit_identifier(draw, harness, self.ui_small_bold, 134), fill=TEXT, font=self.ui_small_bold)
            source_meta = f"{host} · {len(members)} agents"
            draw.text((left+28, sy+3), fit_text(draw, source_meta, self.ui_caption, 136), fill="#AABCD2", font=self.ui_caption)
        for lane in layout.lanes:
            cy = round(body_top + lane.center)
            cluster = lane.cluster
            color = self._model_color(cluster.model)
            # Precision port, model name, and one compact counter line.
            sprite = self._constellation_dot(color, any(node.active for node in cluster.nodes), True)
            image.paste(sprite, (left+model_x-12, cy-12), sprite)
            label = (cluster.provider + "/" if cluster.provider else "") + cluster.model
            compact = len(layout.lanes) > 4
            label_face = self.ui_small_bold if compact else self.ui_body
            name_y, meta_y, rail_y = ((cy-18, cy+2, cy+19) if compact else (cy-24, cy+5, cy+25))
            draw.rectangle((left+model_label_x, name_y, left+model_label_x+model_width, name_y+19), fill=canvas)
            draw.text((left+model_label_x, name_y), fit_identifier(draw, label, label_face, model_width-2), fill=TEXT, font=label_face)
            rate = f"{compact_number(cluster.rate)} tok/s" if cluster.rate is not None else "No counters"
            meta = f"{rate}  ·  {len(cluster.nodes)} agents"
            meta_face = self.ui_caption if compact else self.ui_small
            draw.rectangle((left+model_label_x, meta_y, left+model_label_x+model_width, meta_y+16), fill=canvas)
            draw.text((left+model_label_x, meta_y), fit_text(draw, meta, meta_face, model_width-3), fill=color, font=meta_face)
            # The complete population remains visible as a density rail while
            # individual names page. Rail cells aggregate only above 100 nodes.
            total = len(cluster.nodes)
            groups = min(100, total)
            for i in range(groups):
                start, end = i*total//groups, (i+1)*total//groups
                hot = any(node.active for node in cluster.nodes[start:end])
                rail_width = model_width - 26
                xx = left + model_label_x + i * rail_width / groups
                draw.line((round(xx), rail_y, round(xx + max(0, rail_width/groups-2)), rail_y), fill=color if hot else "#2A3545", width=2)
        for lane in layout.lanes:
            for node, x, y in lane.nodes:
                px, py = round(left+x), round(body_top+y)
                color = self._model_color(node.model)
                focused = node == selected
                face = self.ui_small_bold if focused else self.ui_small
                name = node.name if node.name and node.name != "Agent" else node.session[-8:]
                edge = min(right-21, px+171)
                label = fit_identifier(draw, name, face, edge-px-64)
                draw.rectangle((px+9, py-8, edge, py+9), fill="#151D2B" if focused else canvas)
                sprite = self._constellation_dot(color, node.active, node.key in parents)
                image.paste(sprite, (px-12, py-12), sprite)
                if focused:
                    draw.ellipse((px-8,py-8,px+8,py+8), outline=TEXT)
                draw.text((px+12, py-9), label, fill=TEXT if node.active or focused else "#BAC8DA", font=face)
                if node.rate:
                    rate = f"{compact_number(node.rate)}/s"
                else:
                    rate = "—" if node.rate is None else "0/s"
                draw.text((edge-3, py-8), rate, fill=color if node.rate else "#96A9C0", font=self.ui_caption, anchor="ra")
        if not nodes:
            draw.text((left+model_x, top+165), "Ready for activity", fill=TEXT, font=self.ui_title)
            draw.text((left+model_x, top+202), "Your agents appear here as they report.", fill=MUTED, font=self.ui_small)
        rail = bottom - 41
        draw.line((left+16, rail-10, right-16, rail-10), fill="#202B39")
        if selected:
            name = selected.name if selected.name and selected.name != "Agent" else f"Session {selected.session[-8:]}"
            draw.text((left+16, rail), fit_text(draw, name, self.ui_body, 164 if narrow else 208), fill=TEXT, font=self.ui_body)
            identity = f"{selected.harness}  /  {selected.model}"
            draw.text((left+(208 if narrow else 252), rail+2), fit_identifier(draw, identity, self.ui_small, 280 if narrow else 388), fill=MUTED, font=self.ui_small)
            tokens = f"{compact_number(selected.tokens)} session tokens" if selected.tokens is not None else "Counters pending"
            detail = f"{tokens}   ·   {age_text(selected.updated_at)} ago"
            draw.text((right-16, rail+2), fit_text(draw, detail, self.ui_small, 218 if narrow else 340), fill=MUTED, font=self.ui_small, anchor="ra")
        legend = f"Glow = recent update   ·   Links {visible_edges}/{len(graph.edges)}"
        if layout.pages > 1:
            legend += f"   ·   Models {layout.page+1}/{layout.pages}"
        if any(lane.pages > 1 for lane in layout.lanes):
            legend += "   ·   Names rotate"
        draw.text((left+16, bottom-15), fit_text(draw, legend, self.ui_caption, right-left-49), fill="#96A9C0", font=self.ui_caption)


    def _swarm_flight_deck(
        self, image: Image.Image, draw: ImageDraw.ImageDraw, snapshot: Snapshot,
        models: list[ModelActivity], now: float, box: tuple[int, int, int, int],
    ) -> None:
        """Render a fixed fifty-bay fleet without population-driven reflow."""
        left, top, right, bottom = box
        arena_top = top + 40
        plate_size = (right - left - 2, bottom - arena_top)
        image.paste(self._agent_field_plate(plate_size), (left + 1, arena_top))
        draw.rectangle((left + 1, top + 1, right - 1, arena_top - 1), fill="#030B12")
        draw.text((left + 18, top + 7), "SWARM FLIGHT DECK", fill=CYAN, font=self.label)

        relations = sorted(
            snapshot.agent_relations,
            key=lambda relation: (relation.status == "OPEN", relation.updated_at),
            reverse=True,
        )
        model_rates: dict[str, float] = {}
        for row in models:
            key = row.model.lower()
            model_rates[key] = max(model_rates.get(key, 0.0), max(0.0, row.token_rate))
        max_model_rate = max(model_rates.values(), default=0.0)

        def energy(model: str) -> float:
            if max_model_rate <= 0:
                return 0.35
            ratio = model_rates.get(model.lower(), 0.0) / max_model_rate
            return max(0.22, min(1.0, math.sqrt(ratio)))

        nodes_by_key: dict[str, dict[str, Any]] = {}

        def add_node(
            key: str, name: str, model: str, updated: float, active: bool,
            carrier: bool, parent: str = "", rate: float = 0.0,
        ) -> None:
            prior = nodes_by_key.get(key)
            if prior is None:
                nodes_by_key[key] = {
                    "key": key,
                    "name": name or "AGENT",
                    "model": model,
                    "updated": updated,
                    "active": active,
                    "carrier": carrier,
                    "parent": parent,
                    "rate": rate,
                }
                return
            if updated >= float(prior["updated"]):
                prior["name"] = name or prior["name"]
                prior["model"] = model or prior["model"]
                prior["updated"] = updated
            prior["active"] = bool(prior["active"] or active)
            prior["carrier"] = bool(prior["carrier"] or carrier)
            prior["rate"] = max(float(prior["rate"]), rate)
            if parent:
                prior["parent"] = parent

        for relation in relations:
            parent_key = f"agent:{relation.source}:{relation.parent_id}"
            child_key = f"agent:{relation.source}:{relation.child_id}"
            add_node(
                parent_key, relation.parent_name, relation.parent_model,
                relation.parent_updated_at,
                bool(relation.parent_updated_at and now - relation.parent_updated_at < 120),
                True,
            )
            add_node(
                child_key, relation.child_name, relation.child_model,
                relation.child_updated_at,
                bool(
                    relation.status == "OPEN"
                    and relation.child_updated_at
                    and now - relation.child_updated_at < 120
                ),
                False, parent_key,
            )

        # Reconcile the model/session stream into the topology once. Additional
        # sessions of the same model remain independent carriers.
        claimed: set[str] = set()
        for index, row in enumerate(models):
            match = next((
                node for node in nodes_by_key.values()
                if str(node["key"]) not in claimed
                and str(node.get("model", "")).lower() == row.model.lower()
            ), None)
            active = bool(row.last_seen and now - row.last_seen < 120)
            if match is not None:
                match["rate"] = max(float(match["rate"]), row.token_rate)
                match["active"] = bool(match["active"] or active)
                match["updated"] = max(float(match["updated"]), row.last_seen or 0.0)
                claimed.add(str(match["key"]))
                continue
            session_key = row.session or f"MODEL-{index:02d}"
            add_node(
                f"session:{row.model}:{session_key}", session_key, row.model,
                row.last_seen or 0.0, active, True, rate=row.token_rate,
            )

        nodes = self._assign_swarm_slots(list(nodes_by_key.values()), now)
        node_by_key = {str(node["key"]): node for node in nodes}
        wing_palette = (CYAN, PURPLE, MAGENTA, ORANGE, BLUE, GREEN)

        def wing_color(node: dict[str, Any]) -> str:
            family = str(node.get("parent") or node["key"])
            return wing_palette[self._agent_seed(family) % len(wing_palette)]

        child_counts: dict[str, int] = {}
        for node in nodes:
            parent_key = str(node.get("parent") or "")
            if parent_key in node_by_key:
                child_counts[parent_key] = child_counts.get(parent_key, 0) + 1
        active_count = sum(bool(node["active"]) for node in nodes)
        live_rate = sum(max(0.0, row.token_rate) for row in models)
        open_count = sum(relation.status == "OPEN" for relation in relations)
        overflow = max(0, len(nodes_by_key) - 50)
        status = f"{len(nodes):02d}/50 BAYS · {active_count:02d} HOT · {open_count:02d} DOCKS"
        if overflow:
            status += f" · +{overflow}"
        draw.text(
            (right - 16, top + 8), status, fill=GREEN if active_count else MUTED,
            font=self.nano_bold, anchor="ra",
        )
        hot = sorted(
            nodes,
            key=lambda node: (float(node["rate"]), float(node["updated"])),
            reverse=True,
        )
        hot_names = " · ".join(str(node["name"]).upper() for node in hot[:3])
        draw.text(
            (left + 18, top + 25),
            fit_text(draw, f"HOT // {hot_names or 'AWAITING LIVE SESSIONS'}", self.nano_bold, 410),
            fill=TEXT if nodes else MUTED, font=self.nano_bold,
        )
        draw.text(
            (right - 16, top + 25), f"THRUST {compact_number(live_rate, 1)}/S",
            fill=CYAN if live_rate > 0 else GRID, font=self.nano_bold, anchor="ra",
        )
        neon_line(draw, (left, top + 39, right, top + 39), CYAN, 1)

        positions = [self._swarm_slot_position(slot, box) for slot in range(50)]
        occupied_slots = {
            self.agent_slot_by_key[str(node["key"])] for node in nodes
        }
        for lane in range(5):
            route = [positions[lane * 10 + column] for column in range(10)]
            lane_color = "#12394A" if lane in {1, 2, 3} else "#0E2D3E"
            draw.line(route, fill="#071C29", width=5)
            draw.line(route, fill=lane_color, width=1)
            draw.text(
                (left + 8, route[0][1] - 5), f"{lane + 1}",
                fill="#27566A", font=self.nano_bold,
            )
        for slot, (x, y) in enumerate(positions):
            pad = "#17485A" if slot in occupied_slots else "#0C2736"
            draw.ellipse((x - 12, y + 6, x + 12, y + 10), outline=pad)
            draw.line((x - 5, y + 8, x + 5, y + 8), fill=pad)

        for index, node in enumerate(nodes):
            slot = self.agent_slot_by_key[str(node["key"])]
            x, y = positions[slot]
            active = bool(node["active"])
            carrier = bool(node["carrier"])
            color = wing_color(node)
            intensity = max(
                energy(str(node["model"])),
                min(1.0, float(node["rate"]) / max(1.0, max_model_rate)),
            )
            facing = "right" if x < (left + right) // 2 else "left"
            if active:
                tail = -1 if facing == "right" else 1
                pulse = (
                    now * (2.2 + intensity * 2.6) + index * 0.37
                ) % 1 if self.motion_enabled else 0.5
                tail_x = x + tail * (16 if carrier else 10)
                trail = int(4 + pulse * (7 + intensity * 5))
                draw.line(
                    (tail_x, y, tail_x + tail * trail, y),
                    fill=NEON_DIM.get(color, GRID_SOFT), width=3,
                )
                draw.line(
                    (tail_x, y, tail_x + tail * max(2, trail - 3), y),
                    fill=color, width=1,
                )
                if now - float(node["updated"]) < 12:
                    phase = (
                        now * 0.42 + index * 0.11
                    ) % 1 if self.motion_enabled else 0.5
                    radius = 11 + int(phase * 9)
                    draw.arc(
                        (x - radius, y - radius // 2, x + radius, y + radius // 2),
                        195, 345, fill=NEON_DIM.get(color, GRID_SOFT), width=1,
                    )
            sprite = self._agent_ship_sprite(color, active, carrier, facing)
            image.paste(sprite, (x - sprite.width // 2, y - sprite.height // 2), sprite)
            if carrier:
                draw.line(
                    (x - 7, y + 10, x + 7, y + 10),
                    fill=color if active else GRID,
                )
                attached = child_counts.get(str(node["key"]), 0)
                shown = min(5, attached)
                for pip in range(shown):
                    pip_x = x + (pip - (shown - 1) / 2) * 5
                    draw.ellipse(
                        (int(pip_x) - 1, y - 12, int(pip_x) + 1, y - 10),
                        fill=color if active else GRID,
                    )
                if attached and active and self.motion_enabled:
                    orbit = now * 0.75 + index * 0.53
                    satellite_x = int(x + math.cos(orbit) * 23)
                    satellite_y = int(y + math.sin(orbit) * 7)
                    draw.ellipse(
                        (satellite_x - 1, satellite_y - 1,
                         satellite_x + 1, satellite_y + 1),
                        fill=TEXT,
                    )
            elif node.get("parent"):
                # A bracket, drone silhouette, and shared wing hue encode the
                # attachment without drawing cross-field graph lines.
                draw.line((x - 7, y + 8, x - 7, y + 11, x + 7, y + 11, x + 7, y + 8),
                          fill=color if active else GRID, width=1)

        self._agent_field_bloom(image, box, now)

    def _otel_log(self, image, draw, now, box) -> None:
        left, top, right, bottom = box
        feed = self.activity_feed
        draw.rectangle(box, fill="#080C16")
        draw.line((left, top+14, left, bottom-12), fill="#30394D")
        x, edge = left+16, right-16
        fresh = feed.latest_at > 0 and 0 <= now-feed.latest_at < 15
        tone = RED if feed.errors else CYAN if fresh else MUTED
        draw.text((x, top+12), "Activity", fill=TEXT, font=self.ui_title)
        draw.ellipse((edge-53, top+23, edge-48, top+28), fill=tone)
        draw.text((edge, top+18), "Live" if fresh else "Quiet", fill=tone, font=self.ui_small, anchor="ra")
        draw.text((x, top+44), str(feed.count), fill=TEXT, font=self.metric_small)
        draw.text((x, top+83), "events / 60s", fill=MUTED, font=self.ui_caption)
        # Every bar is one source-time second. Fractional movement is smooth,
        # while bar heights and colors come exclusively from actual records.
        chart_left, chart_right = left+120, edge
        chart_bottom = top+87
        width = chart_right-chart_left
        bins = {}
        for record in feed.records:
            if 0 <= now-record.timestamp < 60:
                second = int(record.timestamp)
                bins[second] = bins.get(second, 0)+1
        peak = max(bins.values(), default=1)
        draw.line((chart_left, chart_bottom, chart_right, chart_bottom), fill=GRID)
        for second, value in bins.items():
            xx = chart_right-int((now-second)/60*width)
            if xx < chart_left:
                continue
            height = max(2, round(value/peak*32))
            draw.rectangle((xx, chart_bottom-height, min(chart_right, xx+2), chart_bottom-1), fill=CYAN)
        draw.text((chart_left, top+90), "-60s", fill=MUTED, font=self.nano)
        draw.text((chart_right, top+90), "now", fill=MUTED, font=self.nano, anchor="ra")
        for index, (key, label, color) in enumerate(LANES):
            y = top+116+index*23
            timestamps = feed.lanes[key]
            draw.text((x, y-5), label, fill=color if timestamps else MUTED, font=self.ui_caption)
            track_left, track_right = x+67, edge-30
            draw.line((track_left, y+2, track_right, y+2), fill="#20283A")
            for stamp in timestamps[-80:]:
                age = now-stamp
                if not 0 <= age < 12:
                    continue
                xx = track_right-(age/12)*(track_right-track_left)
                # A short comet trail follows each real event through its lane.
                draw.line((max(track_left, xx-9), y+2, xx, y+2), fill=NEON_DIM.get(color, GRID), width=3)
                draw.ellipse((int(xx)-1, y, int(xx)+2, y+3), fill=color)
            draw.text((edge, y-6), str(len(timestamps)), fill=color if timestamps else MUTED, font=self.nano_bold, anchor="ra")
        draw.line((x, top+202, edge, top+202), fill="#253249")
        groups = feed.visible_groups(now)
        if not groups:
            draw.text((x, top+221), "Ready for the next action", fill=TEXT, font=self.ui_small_bold)
            draw.text((x, top+246), "Agent signals appear here as they arrive.", fill=MUTED, font=self.ui_caption)
        for index, group in enumerate(groups):
            y = top+211+index*38
            age = max(0, now-group.timestamp)
            highlighted = age < 3
            if highlighted:
                draw.rectangle((x-5, y-3, edge+5, y+32), fill="#152235")
            draw.rectangle((x-5, y, x-3, y+26), fill=group.color if age < 60 else GRID)
            title = group.title + (f" ×{group.count}" if group.count > 1 else "")
            draw.text((x+4, y-2), fit_text(draw, title, self.ui_small_bold, edge-x-48), fill=TEXT, font=self.ui_small_bold)
            elapsed = "now" if age < 1 else f"{int(age)}s" if age < 60 else f"{int(age//60)}m"
            draw.text((edge, y), elapsed, fill=group.color if age < 15 else MUTED, font=self.nano, anchor="ra")
            context = group.model or group.service or "Agent"
            if group.host:
                context += " · "+group.host
            if group.session_id:
                context += " · "+group.session_id[-8:]
            if group.duration_ms is not None:
                context += f" · {group.duration_ms}ms"
            draw.text((x+4, y+17), fit_text(draw, context, self.ui_caption, edge-x-4), fill="#94A8C2", font=self.ui_caption)

    def _rate_chart(self, draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], color: str) -> None:
        x, y, right, bottom = box
        values = list(self.rate_history)
        if max(values, default=0) <= 0:
            values = list(self.flow_history)
            color = CYAN
        maximum = max(values, default=1) or 1
        slot = (right - x) / len(values)
        draw.line((x, bottom, right, bottom), fill=GRID)
        draw.line((x, y + 22, right, y + 22), fill=GRID_SOFT)
        draw.line((x, y + 44, right, y + 44), fill=GRID_SOFT)
        for index, value in enumerate(values):
            height = int((value / maximum) * (bottom - y))
            xx = int(x + index * slot)
            draw.rectangle((xx, bottom - max(1, height), max(xx + 2, int(xx + slot - 2)), bottom),
                           fill=color if value > 0 else GRID_SOFT)

    def _pet_panel(self, image, draw, snapshot, now) -> None:
        behavior = self.pet_behavior
        draw.rectangle((1480, 44, 1919, 419), fill="#080510")
        draw.text((1496, 56), self.companion_name, fill=TEXT, font=self.ui_title)
        mood = "Napping" if self.habitat_director.resting else behavior.mood.capitalize()
        draw.text((1560, 61), mood, fill=MUTED, font=self.ui_small)
        draw.text((1901, 60), f"Level {behavior.level(snapshot):02d}", fill=MUTED, font=self.ui_small_bold, anchor="ra")
        habitat = (1490, 86, 1910, 354)
        scene, frame = self.habitats.frame(now)
        image.paste(frame, habitat[:2])
        # Orbital rings and motes animate continuously at the display cadence.
        center_x, center_y = 1700, 302
        energy = min(1.0, math.log10(combined_token_rate(snapshot, now) + 1) / 4)
        draw.ellipse((center_x - 88, center_y - 19, center_x + 88, center_y + 19), outline=NEON_DIM[MAGENTA], width=3)
        phase = int(now * (28 + 22 * energy)) % 360
        draw.arc((center_x - 88, center_y - 19, center_x + 88, center_y + 19), phase, phase + 140, fill=MAGENTA, width=2)
        draw.arc((center_x - 102, center_y - 25, center_x + 102, center_y + 25), 360 - phase, 500 - phase, fill=CYAN, width=1)
        for index in range(10):
            phase = now * (0.07 + index % 3 * 0.012) + index * .63
            px = int(1700 + math.sin(phase * .8) * (115 + index * 6))
            py = int(315 - ((phase % 1) * 177))
            tone = MAGENTA if index % 2 else CYAN
            draw.ellipse((px - 1, py - 1, px + 1, py + 1), fill=tone)
        if self.pet.motion.ready:
            pose = self.habitat_director.sample(now, behavior.routine, behavior.routine_started,
                                               scene, self.motion_enabled, behavior.energy)
            sprite = self.pet.motion.presentation(pose, now)
            if sprite is not None:
                sprite_x,sprite_y = pose.placement(habitat,sprite.size,
                    self.pet.motion.anchor_x(pose.state,pose.elapsed))
                self.pet_sprite = (sprite,(sprite_x,sprite_y))
            routine = pose.label
        else:
            sprite = self.pet.frame(behavior.state, max(0, now-behavior.state_started))
            if sprite is not None:
                old_x = self._pet_stage_x(behavior.state,now)
                sprite_x = round(1498+(old_x-1518)/218*214)
                self.pet_sprite = (sprite,(sprite_x,112))
            routine = behavior.routine.capitalize().replace("-"," ")
        neon_corners(draw,habitat,MAGENTA,16)
        draw.rectangle((1498,325,1902,349),fill="#080510")
        draw.text((1507,330),fit_text(draw,routine,self.ui_small,245),fill=TEXT,font=self.ui_small)
        draw.text((1894,330),f"Energy {behavior.energy:.0f}%",fill=MUTED,font=self.ui_small,anchor="ra")
        meters = (("FOCUS", behavior.focus, CYAN), ("CURIOSITY", behavior.curiosity, PURPLE),
                  ("CALM", behavior.composure, GREEN), ("CARE", behavior.care_score, MAGENTA))
        for index, (label, value, color) in enumerate(meters):
            x = 1496 + index * 104
            draw.text((x, 365), label.capitalize(), fill=MUTED, font=self.ui_caption)
            draw.text((x, 382), f"{value:.0f}", fill=TEXT, font=self.ui_body)
            draw.rectangle((x, 405, x + 89, 408), fill=GRID_SOFT)
            length = int(89 * max(0, min(100, value)) / 100)
            if length:
                draw.rectangle((x, 405, x + length, 408), fill=color)

    def _pet_mini_meter(
        self, draw: ImageDraw.ImageDraw, x: int, y: int, label: str, value: float,
        color: str, width: int = 120,
    ) -> None:
        draw.text((x, y), label, fill=MUTED, font=self.nano_bold)
        draw.text((x + width, y), f"{value:02.0f}", fill=TEXT, font=self.nano, anchor="ra")
        draw.rectangle((x, y + 16, x + width, y + 21), fill=GRID_SOFT)
        fill_width = int(width * max(0.0, min(100.0, value)) / 100)
        if fill_width:
            draw.rectangle((x, y + 16, x + fill_width, y + 21), fill=NEON_DIM.get(color, color))
            draw.line((x, y + 18, x + fill_width, y + 18), fill=color, width=2)

    def _trace(self, draw, snapshot, now) -> None:
        draw.rectangle((0, 420, WIDTH - 1, 461), fill="#05060C")
        draw.line((15, 420, WIDTH - 16, 420), fill="#27303D")
        stats = snapshot.stats
        cells = (
            (16, 184, "CPU", f"{stats.cpu:.0f}%" if stats.ram_total else "—", stats.cpu, CYAN),
            (216, 384, "RAM", f"{stats.ram_used:.1f}/{stats.ram_total:.0f} GiB" if stats.ram_total else "—", stats.ram, PURPLE),
            (416, 584, "VRAM", f"{stats.vram_used:.1f}/{stats.vram_total:.0f} GiB" if stats.vram_total else "—",
             stats.vram_used / stats.vram_total * 100 if stats.vram_total else 0, MAGENTA),
        )
        for x, edge, label, value, percent, color in cells:
            draw.text((x, 430), label, fill=MUTED, font=self.ui_small)
            draw.text((edge, 429), value, fill=TEXT, font=self.ui_small, anchor="ra")
            draw.line((x, 452, edge, 452), fill=GRID_SOFT, width=2)
            length = int((edge - x) * max(0, min(100, percent)) / 100)
            if length:
                draw.line((x, 452, x + length, 452), fill=color, width=2)
        for x in (200, 400, 600):
            draw.line((x, 429, x, 453), fill=GRID_SOFT)
        fresh = [u for u in canonical_usages(snapshot, now).values() if 0 <= now-u.updated_at < 45]
        hosts = {u.source for u in fresh}
        harnesses = {u.harness or u.agent for u in fresh}
        scope = (f"Live feed: {len(hosts)} host{'s' if len(hosts) != 1 else ''} / {len(harnesses)} harness{'es' if len(harnesses) != 1 else ''}"
                 f" · {len(fresh)} recent sessions") if fresh else "Live feed: no recent token counters"
        if len(hosts) == 1 and len(harnesses) == 1:
            scope = f"Live feed: {next(iter(hosts))} / {next(iter(harnesses))} · {len(fresh)} recent sessions"
        draw.text((616, 430), fit_text(draw, scope, self.ui_small, 610), fill=CYAN if fresh else MUTED, font=self.ui_small)
        account = snapshot.usagemax_stats
        status = f"Account: {age_text(account.last_sync_at)} old" if account and account.last_sync_at else "Account: awaiting sync"
        if account and account.status != "SYNCED":
            status += f" · {account.status.lower()}"
        draw.text((1260, 430), fit_text(draw, status, self.ui_small, 375), fill=AMBER if account and account.status != "SYNCED" else MUTED, font=self.ui_small)
        draw.text((1904, 430), f"Hardware: {stats.host_name or 'local host'}", fill=MUTED, font=self.ui_small, anchor="ra")


def load_config(path: Path | None) -> dict[str, Any]:
    """Load settings and resolve data paths without writing into the install tree."""

    config_path = Path(path).expanduser().resolve() if path else None
    config_dir = config_path.parent if config_path else Path.cwd()
    config: dict[str, Any] = {
        "interval": 1 / 30,
        "max_fps": 30,
        "metrics_interval": 2.0,
        "local_session_sources": False,
        "usagemax_enabled": False,
        "usagemax_base_url": "https://usagemax.com",
        "usagemax_handle": "",
        "usagemax_poll_seconds": 120,
        "usagemax_timeout_seconds": 4,
        "idle_metrics_interval": 30.0,
        "otel_hz": 30,
        "adaptive_refresh": True,
        "burst_fps": 30,
        "active_fps": 30,
        "idle_fps": 30,
        "sleep_fps": 30,
        "burst_seconds": 2,
        "burst_rearm_seconds": 10,
        "active_window_seconds": 30,
        "sleep_after_seconds": 60,
        "frame_headroom": 0.82,
        "jpeg_quality": 34,
        "jpeg_quality_burst": 28,
        "jpeg_quality_active": 34,
        "jpeg_quality_idle": 34,
        "source_scan_interval": 60,
        "session_scan_interval": 30,
        "telemetry_files": {},
        "telemetry_tail_bytes": 1_000_000,
        "telemetry_tail_lines": 2048,
        "otel_file_scan_interval": 10,
        "remote_probe_interval": 120,
        "wsl_probe_interval": 60,
        "pet_atlas": "assets/pet/spritesheet.webp",
        "pet_state": "pet-state.json",
        "pet_backgrounds": "assets/pet/backgrounds",
        "companion_name": "Companion",
        "transport": "trofeo",
        "renderer": "usagemax",
        "native_transport_enabled": False,
        "native_transport": "",
        "otel_files": [],
        "otel_http_enabled": False,
        "otel_http_bind": "127.0.0.1",
        "otel_http_port": 4318,
        "otel_http_file": "otel/http.otlp.jsonl",
        "motion_enabled": False,
        "agent_fx_quality": "balanced",
        "panel_protection": True,
        "night_dimming": False,
        "night_start_hour": 23,
        "night_end_hour": 7,
        "night_brightness": 0.62,
        "rest_brightness": 1.0,
        "rest_palette_enabled": False,
        "rest_pixel_shift": 2,
        "rest_pixel_shift_seconds": 60,
        "rest_palette_seconds": 900,
        "remote_sources": [],
    }
    if config_path:
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Could not read configuration file {config_path}: {error}") from error
        if not isinstance(loaded, dict):
            raise ValueError("configuration root must be a JSON object")
        config.update(loaded)

    runtime_value = config.get("runtime_dir")
    runtime_dir = (
        resolve_path(str(runtime_value), config_dir)
        if runtime_value
        else default_data_dir()
    )
    config["runtime_dir"] = str(runtime_dir)
    config["_config_dir"] = str(config_dir)
    config["pet_atlas"] = str(resolve_path(str(config.get("pet_atlas", "")), config_dir))
    config["pet_state"] = str(resolve_path(str(config.get("pet_state", "pet-state.json")), runtime_dir))
    config["pet_backgrounds"] = str(resolve_path(str(config.get("pet_backgrounds", "")), config_dir))
    if config.get("native_transport"):
        config["native_transport"] = str(resolve_path(str(config["native_transport"]), config_dir))
    return config


class JpegEncoder:
    """Reuse the TurboJPEG-backed Pillow output buffer across frames."""

    def __init__(self) -> None:
        self.output = io.BytesIO()

    def encode(self, image: Image.Image, rotation: int = 0, quality: int = 80) -> bytes:
        # SUB 2/3/4 is the native mount; other variants use the vendor's
        # 180-degree encode baseline.
        wire_image = (
            image.transpose(Image.Transpose.ROTATE_180)
            if rotation == 180
            else image
        )
        quality = max(10, min(95, int(quality)))
        while quality >= 10:
            self.output.seek(0)
            self.output.truncate()
            wire_image.save(
                self.output, format="JPEG", quality=quality,
                optimize=False, subsampling=2,
            )
            payload = self.output.getvalue()
            if len(payload) <= MAX_JPEG_BYTES or quality == 10:
                return payload
            quality -= 5
        return payload


def encode_jpeg(image: Image.Image, rotation: int = 0, quality: int = 80) -> bytes:
    """Compatibility helper for one-shot previews and tests."""
    return JpegEncoder().encode(image, rotation, quality)


def create_hud_renderer(config: dict[str, Any]) -> Hud:
    """Create the built-in UsageMax renderer from resolved configuration."""

    return Hud(
        Path(str(config.get("pet_atlas", ""))),
        Path(str(config.get("pet_state", default_data_dir() / "pet-state.json"))),
        Path(str(config.get("pet_backgrounds", ""))),
        int(config.get("otel_hz", 30)),
        bool(config.get("motion_enabled", False)),
        str(config.get("agent_fx_quality", "balanced")),
        str(config.get("companion_name", "Companion")),
    )


def create_trofeo_transport(config: dict[str, Any]) -> TrofeoTransport:
    """Create the built-in reverse-engineered Trofeo 0416:5408 USB sink."""

    native_value = str(config.get("native_transport", ""))
    native_path = Path(native_value) if native_value else Path()
    return TrofeoTransport(
        native_path, bool(config.get("native_transport_enabled", False))
    )


def run(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config) if args.config else None)
    if args.agent_fx_quality:
        config["agent_fx_quality"] = args.agent_fx_quality
    if args.renderer:
        config["renderer"] = args.renderer
    if args.transport:
        config["transport"] = args.transport
    max_fps = max(1.0, min(240.0, float(config.get("max_fps", MAX_PANEL_FPS))))
    interval = float(args.interval or config.get("interval", 1 / max_fps))
    configured_fps = min(max_fps, 1 / max(1 / max_fps, interval))
    metrics_interval = max(interval, float(config.get("metrics_interval", 1.0)))
    idle_metrics_interval = max(
        metrics_interval, float(config.get("idle_metrics_interval", 5.0))
    )
    collector = Collector(config)
    try:
        renderer_name = str(config.get("renderer", "usagemax"))
        renderer = (
            create_hud_renderer(config)
            if renderer_name == "usagemax"
            else load_plugin("usagemax_display.renderers", renderer_name, config)
        )
        renderer = validate_renderer(renderer)
        transport_name = str(config.get("transport", "trofeo"))
        transport = (
            create_trofeo_transport(config)
            if transport_name == "trofeo"
            else load_plugin("usagemax_display.frame_sinks", transport_name, config)
        )
        transport = validate_frame_sink(transport)
    except (PluginLoadError, TypeError, ValueError) as error:
        print(f"display plugin error: {error}", file=sys.stderr)
        if "renderer" in locals() and callable(getattr(renderer, "close", None)):
            renderer.close()
        collector.close()
        return 2
    governor = FrameGovernor(config, configured_fps)
    if args.fixed_fps:
        governor.enabled = False
    encoder = JpegEncoder()
    protector = PanelProtector(config)
    frame_count = 0
    last_frame = time.monotonic()
    next_frame = last_frame
    next_metrics = 0.0
    snapshot: Snapshot | None = None
    smoothed_fps = 0.0
    last_report_mode = ""
    if args.preview:
        print(f"preview mode: {args.preview}")

    try:
        while True:
            started = time.monotonic()
            fps = 1 / max(0.001, started - last_frame) if frame_count else 1 / max(0.001, interval)
            last_frame = started
            smoothed_fps = fps if smoothed_fps <= 0 else smoothed_fps * 0.82 + fps * 0.18
            if not transport.connected and not args.preview:
                try:
                    transport.connect()
                except Exception as error:
                    print(f"waiting for {transport.mode}: {error}")
                    time.sleep(3)
                    continue
            detail = transport.detail
            metrics_refreshed = snapshot is None or started >= next_metrics
            if metrics_refreshed:
                snapshot = collector.snapshot(
                    "DISPLAY LINK LIVE" if transport.connected else "PREVIEW MODE",
                    detail, smoothed_fps,
                )
            else:
                snapshot.fps = smoothed_fps
                snapshot.usb_status = "DISPLAY LINK LIVE" if transport.connected else "PREVIEW MODE"
                snapshot.usb_detail = detail
                snapshot.otel_records = collector.live_otel_records()
            wall_now = time.time()
            plan = governor.select(snapshot, wall_now, started)
            set_runtime_mode = getattr(renderer, "set_runtime_mode", None)
            if callable(set_runtime_mode):
                set_runtime_mode(plan.mode)
            if metrics_refreshed:
                cadence = (
                    metrics_interval
                    if plan.mode in {"burst", "active", "fixed"}
                    else idle_metrics_interval
                )
                next_metrics = started + cadence
            if transport.connected:
                snapshot.usb_status = f"{transport.mode} · {plan.mode.upper()} {plan.fps:.0f}FPS"
            render_started = time.monotonic()
            if args.test_color:
                colors = {
                    "red": (255, 0, 0),
                    "green": (0, 255, 0),
                    "blue": (0, 0, 255),
                    "white": (255, 255, 255),
                    "black": (0, 0, 0),
                }
                image = Image.new("RGB", (renderer.width, renderer.height), colors[args.test_color])
            else:
                image = renderer.render(snapshot, wall_now)
                if not isinstance(image, Image.Image):
                    raise TypeError("renderer must return a Pillow Image")
                if image.size != (renderer.width, renderer.height):
                    raise ValueError(
                        "renderer output size must match its declared width and height"
                    )
            if not args.test_color:
                image = protector.apply(image, plan.mode, wall_now)
            if args.preview:
                image.save(args.preview, format="JPEG")
                print(f"wrote {args.preview}")
                return 0
            rendered_at = time.monotonic()
            payload = encoder.encode(image, transport.rotation, plan.jpeg_quality)
            encoded_at = time.monotonic()
            try:
                transport.send(payload)
            except Exception as error:
                failed_at = time.monotonic()
                governor.observe(
                    failed_at - render_started, failed_at - encoded_at, failed_at, failed=True
                )
                print(f"frame send failed; reconnecting: {error}")
                transport.close()
                time.sleep(1)
                continue
            sent_at = time.monotonic()
            governor.observe(sent_at - render_started, sent_at - encoded_at, sent_at)
            frame_count += 1
            if frame_count == 1 and args.snapshot:
                try:
                    capture = Path(args.snapshot)
                    capture.parent.mkdir(parents=True, exist_ok=True)
                    capture.with_suffix(".jpg").write_bytes(payload)
                    image.save(capture.with_suffix(".png"))
                    print(f"captured acknowledged frame: {capture}")
                except OSError as error:
                    print(f"frame capture unavailable: {error}")
            mode_changed = plan.mode != last_report_mode
            if frame_count == 1 or frame_count % 60 == 0 or mode_changed:
                last_report_mode = plan.mode
                print(
                    f"frame={frame_count} bytes={len(payload)} "
                    f"hz={smoothed_fps:.1f}/{plan.fps:.1f} mode={plan.mode} "
                    f"q={plan.jpeg_quality} render={(rendered_at - render_started) * 1000:.1f}ms "
                    f"encode={(encoded_at - rendered_at) * 1000:.1f}ms "
                    f"send={(sent_at - encoded_at) * 1000:.1f}ms "
                    f"display={transport.detail} mode={transport.mode}"
                )
            if args.once:
                return 0
            if args.frames and frame_count >= args.frames:
                return 0
            next_frame += 1 / plan.fps
            current = time.monotonic()
            if next_frame < current:
                next_frame = current
            time.sleep(max(0, next_frame - current))
    except KeyboardInterrupt:
        return 0
    finally:
        transport.close()
        collector.close()
        renderer.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render telemetry dashboards to a display transport"
    )
    parser.add_argument("--config", help="optional JSON configuration path")
    parser.add_argument("--interval", type=float, help="frame interval in seconds")
    parser.add_argument(
        "--preview", metavar="PATH", nargs="?", const="preview.jpg",
        help="render one JPEG locally without connecting to a display (default: preview.jpg)",
    )
    parser.add_argument(
        "--transport", help="override the built-in transport or installed frame sink plugin name",
    )
    parser.add_argument(
        "--renderer", help="override the built-in renderer or installed renderer plugin name",
    )
    parser.add_argument("--snapshot", metavar="PATH", help="save the first USB-acknowledged frame and wire JPEG")
    parser.add_argument("--once", action="store_true", help="send one frame and exit")
    parser.add_argument("--frames", type=int, help="send a fixed frame count, then exit")
    parser.add_argument(
        "--fixed-fps", action="store_true",
        help="disable adaptive pacing for a short capability benchmark",
    )
    parser.add_argument(
        "--test-color",
        choices=("red", "green", "blue", "white", "black"),
        help="send a solid diagnostic frame",
    )
    parser.add_argument(
        "--agent-fx-quality", choices=("cinematic", "balanced", "lean"),
        help="override orchestration-field effects quality for a benchmark",
    )
    args = parser.parse_args()
    try:
        return run(args)
    except (OSError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
