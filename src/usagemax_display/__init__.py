"""Public plugin interfaces for UsageMax Display.

Hardware backends and renderers can implement the protocols in :mod:`api` and
be discovered through the entry-point helpers in :mod:`plugins`.
"""

from .api import FrameSink, Renderer, validate_frame_sink, validate_renderer
from .plugins import PluginLoadError, load_plugin

__all__ = [
    "FrameSink",
    "PluginLoadError",
    "Renderer",
    "load_plugin",
    "validate_frame_sink",
    "validate_renderer",
]
