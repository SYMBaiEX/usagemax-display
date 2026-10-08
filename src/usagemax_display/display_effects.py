"""Event cinematics and long-running panel protection for the Trofeo HUD."""

from __future__ import annotations

import math
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter


class CachedDraw:
    """Reuse text tiles across frames and the scrolling tape, with bounded memory."""

    def __init__(self, image: Image.Image, cache: OrderedDict) -> None:
        self.image = image
        self.draw = ImageDraw.Draw(image)
        self.cache = cache

    def __getattr__(self, name):
        return getattr(self.draw, name)

    def text(self, xy, text, fill=None, font=None, **kwargs):
        # Pillow composites outline and fill separately; preserve that exact
        # antialiasing path for the few outlined graph labels.
        if kwargs.get("stroke_width", 0):
            return self.draw.text(xy, text, fill=fill, font=font, **kwargs)
        key = (text, fill, font, tuple(sorted(kwargs.items())))
        cached = self.cache.get(key)
        if cached is None:
            box = self.draw.textbbox((0, 0), text, font=font,
                                     **{k: v for k, v in kwargs.items() if k != "stroke_fill"})
            left, top, right, bottom = box
            tile = Image.new("RGBA", (max(1, right - left), max(1, bottom - top)))
            ImageDraw.Draw(tile).text((-left, -top), text, fill=fill, font=font, **kwargs)
            cached = (tile, left, top)
            self.cache[key] = cached
            if len(self.cache) > 2048:
                self.cache.popitem(last=False)
        else:
            self.cache.move_to_end(key)
        tile, left, top = cached
        self.image.paste(tile, (int(xy[0]) + left, int(xy[1]) + top), tile)


class NeonBloom:
    """Low-resolution, chroma-only bloom; white type retains its crisp edge."""

    def __init__(self) -> None:
        self.updated_at = 0.0
        self.overlay: Image.Image | None = None
        # Bloom only strong color accents; dark surfaces must stay dark.
        self.chroma_lut = tuple(min(175, max(0, (value - 65) * 3)) for value in range(256))

    def apply(self, image: Image.Image, now: float) -> Image.Image:
        if self.overlay is None or now - self.updated_at >= 1 / 12:
            small = image.resize((image.width // 4, image.height // 4), Image.Resampling.BILINEAR)
            red, green, blue = small.split()
            high = ImageChops.lighter(ImageChops.lighter(red, green), blue)
            low = ImageChops.darker(ImageChops.darker(red, green), blue)
            mask = ImageChops.subtract(high, low).point(self.chroma_lut)
            emission = ImageChops.multiply(small, Image.merge("RGB", (mask, mask, mask)))
            halo = emission.filter(ImageFilter.GaussianBlur(1.35))
            self.overlay = halo.resize(image.size, Image.Resampling.BILINEAR)
            self.updated_at = now
        return ImageChops.screen(image, self.overlay)


@dataclass(frozen=True)
class OverdriveState:
    kind: str = ""
    label: str = ""
    started_at: float = 0.0
    duration: float = 0.0

    def progress(self, now: float) -> float:
        if not self.kind or self.duration <= 0:
            return 1.0
        return max(0.0, min(1.0, (now - self.started_at) / self.duration))


class OverdriveDirector:
    """Turn genuine agent lifecycle events into brief, non-blocking sequences."""

    PRIORITY = {"LAUNCH": 1, "COMPLETE": 2, "FAULT": 3}

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.state = OverdriveState()
        self.seen_order: deque[tuple[Any, ...]] = deque(maxlen=512)
        self.seen: set[tuple[Any, ...]] = set()

    def _remember(self, key: tuple[Any, ...]) -> bool:
        if key in self.seen:
            return False
        if len(self.seen_order) == self.seen_order.maxlen:
            self.seen.discard(self.seen_order[0])
        self.seen_order.append(key)
        self.seen.add(key)
        return True

    @staticmethod
    def _classify(message: str, status: str) -> tuple[str, str] | None:
        text = message.upper()
        if status in {"ERROR", "ERR"} or any(word in text for word in ("FAILED", "FAULT", "EXCEPTION")):
            return "FAULT", "RECOVERY LINK"
        if any(word in text for word in ("DONE", "COMPLETE", "COMPLETED", "SUCCESS")):
            return "COMPLETE", "MISSION CLEAR"
        if any(word in text for word in ("PROMPT", "TURN", "LAUNCH", "START", "SPAWN", "AGENT.RUN")):
            return "LAUNCH", "AGENT ONLINE"
        return None

    def update(self, events: list[Any], otel_records: tuple[Any, ...], now: float) -> OverdriveState:
        if not self.enabled:
            return self.state
        candidates: list[tuple[int, float, str, str]] = []
        for event in events:
            timestamp = float(getattr(event, "timestamp", 0) or 0)
            key = ("event", timestamp, getattr(event, "source", ""), getattr(event, "message", ""))
            fresh = now - timestamp <= 8
            if not self._remember(key) or not fresh:
                continue
            classified = self._classify(str(getattr(event, "message", "")), str(getattr(event, "level", "")))
            if classified:
                kind, label = classified
                candidates.append((self.PRIORITY[kind], timestamp, kind, label))
        for record in otel_records:
            timestamp = float(getattr(record, "timestamp", 0) or 0)
            key = ("otel", getattr(record, "identity", (timestamp, getattr(record, "name", ""))))
            fresh = now - timestamp <= 8
            if not self._remember(key) or not fresh:
                continue
            classified = self._classify(str(getattr(record, "name", "")), str(getattr(record, "status", "")))
            if classified:
                kind, label = classified
                candidates.append((self.PRIORITY[kind], timestamp, kind, label))
        if candidates:
            _, _, kind, label = max(candidates)
            durations = {"LAUNCH": 1.4, "COMPLETE": 1.7, "FAULT": 2.0}
            current_active = self.state.progress(now) < 1
            if not current_active or self.PRIORITY[kind] >= self.PRIORITY.get(self.state.kind, 0):
                self.state = OverdriveState(kind, label, now, durations[kind])
        return self.state

    def paint(self, draw: ImageDraw.ImageDraw, now: float, label_font: Any, width: int, height: int) -> None:
        progress = self.state.progress(now)
        if progress >= 1:
            return
        palette = {
            "LAUNCH": (255, 36, 153),
            "COMPLETE": (202, 255, 74),
            "FAULT": (255, 77, 112),
        }
        color = palette[self.state.kind]
        dim = tuple(max(8, channel // 4) for channel in color)
        pulse = 0.55 + 0.45 * math.sin(progress * math.pi * 5)
        rail = tuple(int(channel * pulse) for channel in color)

        if self.state.kind == "LAUNCH":
            draw.rectangle((2, 2, width - 3, height - 3), outline=dim, width=6)
            draw.rectangle((2, 2, width - 3, height - 3), outline=rail, width=2)
        elif self.state.kind == "COMPLETE":
            inset = int((1 - progress) * width * 0.32)
            draw.line((inset, 45, width - inset, 45), fill=rail, width=3)
            draw.line((inset, height - 43, width - inset, height - 43), fill=rail, width=3)
        else:
            draw.rectangle((1, 1, width - 2, height - 2), outline=rail, width=3)



class PanelProtector:
    """Apply software dimming, quiet palette drift, and tiny rest-only pixel shifts."""

    PALETTES = ((18, 58, 72), (46, 24, 72), (16, 68, 61), (58, 22, 46))
    SHIFTS = ((0, 0), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))

    def __init__(self, config: dict[str, Any]) -> None:
        self.enabled = bool(config.get("panel_protection", True))
        self.night_dimming = bool(config.get("night_dimming", False))
        self.night_start = int(config.get("night_start_hour", 23)) % 24
        self.night_end = int(config.get("night_end_hour", 7)) % 24
        self.night_brightness = max(0.2, min(1.0, float(config.get("night_brightness", 0.62))))
        self.rest_brightness = max(0.3, min(1.0, float(config.get("rest_brightness", 1.0))))
        self.rest_palette_enabled = bool(config.get("rest_palette_enabled", False))
        self.shift_pixels = max(0, min(4, int(config.get("rest_pixel_shift", 2))))
        self.shift_interval = max(30, int(config.get("rest_pixel_shift_seconds", 60)))
        self.palette_interval = max(300, int(config.get("rest_palette_seconds", 900)))
        self.last_mode = ""

    def is_night(self, now: float) -> bool:
        hour = datetime.fromtimestamp(now).hour
        if self.night_start == self.night_end:
            return True
        if self.night_start < self.night_end:
            return self.night_start <= hour < self.night_end
        return hour >= self.night_start or hour < self.night_end

    def apply(self, image: Image.Image, mode: str, now: float) -> Image.Image:
        if not self.enabled:
            return image
        result = image
        if mode == "rest":
            if self.rest_palette_enabled:
                palette = self.PALETTES[int(now // self.palette_interval) % len(self.PALETTES)]
                tint = Image.new("RGB", result.size, palette)
                result = Image.blend(result, tint, 0.075)
                result = ImageEnhance.Color(result).enhance(0.82)
            if self.shift_pixels:
                dx, dy = self.SHIFTS[int(now // self.shift_interval) % len(self.SHIFTS)]
                dx *= self.shift_pixels
                dy *= self.shift_pixels
                shifted = Image.new("RGB", result.size, (3, 7, 12))
                shifted.paste(result, (dx, dy))
                result = shifted
        brightness = 1.0
        if mode == "rest":
            brightness *= self.rest_brightness
        if self.night_dimming and self.is_night(now):
            brightness *= self.night_brightness
        if brightness < 0.999:
            result = ImageEnhance.Brightness(result).enhance(brightness)
        self.last_mode = f"{mode}:{'night' if self.is_night(now) else 'day'}"
        return result
