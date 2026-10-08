"""Stable, application-independent interfaces for display plugins.

Implementations should accept opaque application snapshots and keep any
application-specific translation inside their own package. A frame is an
opaque image object produced by a renderer; sinks are responsible for sending
that frame to their target display.
"""

from __future__ import annotations

from typing import Any, Protocol, cast


class FrameSink(Protocol):
    """A connected display target that accepts rendered frames.

    ``connect`` acquires the target and may raise an implementation-specific
    exception if it is unavailable. The host converts rendered Pillow images
    to JPEG bytes before calling ``send``. ``close`` releases resources and
    should be safe to call during cleanup.

    The properties expose concise runtime status for a host application:
    ``connected`` reports whether the target is currently usable, ``mode`` is
    a short backend/status label, ``detail`` is a human-readable description,
    and ``rotation`` is the target's preferred image rotation in degrees.
    """

    @property
    def connected(self) -> bool:
        """Whether the sink currently has an active display connection."""

    @property
    def mode(self) -> str:
        """Short backend or connection-mode label."""

    @property
    def detail(self) -> str:
        """Human-readable target or connection description."""

    @property
    def rotation(self) -> int:
        """Preferred frame rotation in degrees, normally 0 or 180."""

    def connect(self) -> None:
        """Acquire or establish the display connection."""

    def send(self, frame: bytes) -> None:
        """Transfer one encoded JPEG frame to the display."""

    def close(self) -> None:
        """Release the display connection and associated resources."""


class Renderer(Protocol):
    """Render an application snapshot into an opaque frame.

    ``snapshot`` is intentionally typed as ``Any`` so plugins do not need to
    import application internals. Hosts should document the snapshot fields
    they provide; a renderer should read only the fields it needs. ``now`` is
    the wall-clock timestamp in seconds used for time-dependent rendering.
    The returned Pillow image is converted to JPEG before it is passed to a
    :class:`FrameSink`.
    """

    @property
    def width(self) -> int:
        """Output frame width in pixels."""

    @property
    def height(self) -> int:
        """Output frame height in pixels."""

    def render(self, snapshot: Any, now: float) -> Any:
        """Render ``snapshot`` at wall-clock time ``now`` and return a frame."""

    def close(self) -> None:
        """Release renderer resources."""


def validate_frame_sink(value: Any) -> FrameSink:
    """Check a sink's public shape before the render loop starts."""

    for method in ("connect", "send", "close"):
        if not callable(getattr(value, method, None)):
            raise TypeError(f"frame sink must provide callable {method}()")
    for attribute in ("connected", "mode", "detail", "rotation"):
        if not hasattr(value, attribute):
            raise TypeError(f"frame sink must provide {attribute!r}")
    if not isinstance(value.connected, bool):
        raise TypeError("frame sink connected property must be a bool")
    if not isinstance(value.mode, str) or not isinstance(value.detail, str):
        raise TypeError("frame sink mode and detail properties must be strings")
    if value.rotation not in {0, 180}:
        raise TypeError("frame sink rotation must be 0 or 180")
    return cast(FrameSink, value)


def validate_renderer(value: Any) -> Renderer:
    """Check a renderer's public shape before the render loop starts."""

    if not callable(getattr(value, "render", None)):
        raise TypeError("renderer must provide callable render(snapshot, now)")
    if not callable(getattr(value, "close", None)):
        raise TypeError("renderer must provide callable close()")
    for attribute in ("width", "height"):
        dimension = getattr(value, attribute, None)
        if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension <= 0:
            raise TypeError(f"renderer {attribute} must be a positive integer")
    return cast(Renderer, value)
