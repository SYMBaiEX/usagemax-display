"""Entry-point discovery and loading for installed display plugins."""

from __future__ import annotations

from collections.abc import Mapping
from importlib.metadata import EntryPoint, entry_points
from typing import Any


class PluginLoadError(RuntimeError):
    """Raised when an installed plugin cannot be uniquely loaded or created."""


def load_plugin(
    group: str,
    name: str,
    config: Mapping[str, Any] | None = None,
) -> Any:
    """Load and instantiate one named installed entry point.

    Plugins are published using standard Python package entry points. The
    entry point's ``value`` must resolve to a callable factory accepting one
    configuration mapping and returning the plugin instance, for example::

        [project.entry-points."usagemax_display.frame_sinks"]
        my_panel = "my_panel:make_sink"

    ``make_sink(config)`` is called once and its result is returned. The
    configuration mapping is empty when omitted. Exactly one entry point must
    match both ``group`` and ``name``; missing or duplicate registrations raise
    :class:`PluginLoadError` with a concise diagnostic. Import and factory
    failures are wrapped so callers can handle plugin startup failures through
    one public exception type.
    """

    if not group or not group.strip():
        raise ValueError("plugin entry-point group must be a non-empty string")
    if not name or not name.strip():
        raise ValueError("plugin entry-point name must be a non-empty string")

    try:
        matches = [ep for ep in entry_points().select(group=group, name=name)]
    except Exception as exc:
        raise PluginLoadError(
            f"Could not discover plugin {name!r} in entry-point group {group!r}."
        ) from exc

    if not matches:
        raise PluginLoadError(
            f"No plugin named {name!r} is installed in entry-point group {group!r}."
        )
    if len(matches) > 1:
        locations = ", ".join(_entry_point_target(ep) for ep in matches)
        raise PluginLoadError(
            f"Plugin name {name!r} is registered {len(matches)} times in entry-point "
            f"group {group!r}: {locations}. Choose a unique entry-point name."
        )

    entry_point = matches[0]
    try:
        factory = entry_point.load()
    except Exception as exc:
        raise PluginLoadError(
            f"Could not import plugin {name!r} from entry-point group {group!r}."
        ) from exc
    if not callable(factory):
        raise PluginLoadError(
            f"Plugin {name!r} in entry-point group {group!r} must resolve to a "
            "callable factory accepting config."
        )

    try:
        return factory(config if config is not None else {})
    except Exception as exc:
        raise PluginLoadError(
            f"Factory for plugin {name!r} in entry-point group {group!r} failed."
        ) from exc


def _entry_point_target(entry_point: EntryPoint) -> str:
    """Return a compact target string for duplicate-name diagnostics."""

    return entry_point.value or "<unknown target>"
