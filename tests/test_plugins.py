from typing import Any

import pytest

from usagemax_display import (
    PluginLoadError,
    load_plugin,
    plugins,
    validate_frame_sink,
    validate_renderer,
)


class EntryPoint:
    def __init__(self, name: str, factory: Any) -> None:
        self.name = name
        self.value = f"example:{name}"
        self.factory = factory

    def load(self) -> Any:
        return self.factory


class EntryPoints(list):
    def select(self, *, group: str, name: str) -> list[EntryPoint]:
        return [item for item in self if item.name == name]


def test_load_plugin_calls_unique_factory_with_config(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[dict[str, int]] = []
    monkeypatch.setattr(
        plugins,
        "entry_points",
        lambda: EntryPoints([EntryPoint("sample", lambda config: observed.append(config) or object())]),
    )

    result = load_plugin("example.group", "sample", {"frames": 3})

    assert result is not None
    assert observed == [{"frames": 3}]


def test_load_plugin_rejects_missing_or_duplicate_names(monkeypatch: pytest.MonkeyPatch) -> None:
    entries = EntryPoints([EntryPoint("same", lambda config: object()), EntryPoint("same", lambda config: object())])
    monkeypatch.setattr(plugins, "entry_points", lambda: entries)

    with pytest.raises(PluginLoadError, match="registered 2 times"):
        load_plugin("example.group", "same")
    with pytest.raises(PluginLoadError, match="No plugin named"):
        load_plugin("example.group", "missing")


def test_plugin_contract_validators_reject_incomplete_implementations() -> None:
    with pytest.raises(TypeError, match="positive integer"):
        validate_renderer(type("BadRenderer", (), {"width": 0, "height": 10, "render": lambda *a: None, "close": lambda *a: None})())

    class BadSink:
        connected = False
        mode = "test"
        detail = "test"
        rotation = 90

        def connect(self) -> None:
            pass

        def send(self, frame: bytes) -> None:
            pass

        def close(self) -> None:
            pass

    with pytest.raises(TypeError, match="rotation must be 0 or 180"):
        validate_frame_sink(BadSink())
