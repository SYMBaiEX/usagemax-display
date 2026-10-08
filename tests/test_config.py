import json
from pathlib import Path

from usagemax_display.display import load_config
from usagemax_display.paths import default_data_dir


def test_default_config_uses_safe_opt_in_boundaries() -> None:
    config = load_config(None)

    assert config["local_session_sources"] is False
    assert config["usagemax_enabled"] is False
    assert config["otel_http_enabled"] is False
    assert config["otel_http_bind"] == "127.0.0.1"
    assert config["remote_sources"] == []
    assert config["runtime_dir"] == str(default_data_dir())


def test_config_relative_paths_are_resolved_without_install_tree_writes(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"runtime_dir": "state", "telemetry_files": {"Example": "logs/events.jsonl"}}),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config["runtime_dir"] == str(tmp_path / "state")
    assert config["pet_state"] == str(tmp_path / "state" / "pet-state.json")
    assert config["_config_dir"] == str(tmp_path)
