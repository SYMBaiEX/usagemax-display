from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from usagemax_display.display import run


def test_preview_writes_an_image_without_usb(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"runtime_dir":"state","local_session_sources":false,'
        '"usagemax_enabled":false,"otel_http_enabled":false,'
        '"motion_enabled":false,"pet_atlas":"","pet_backgrounds":""}',
        encoding="utf-8",
    )
    output = tmp_path / "preview.jpg"
    args = SimpleNamespace(
        config=str(config_path),
        interval=None,
        agent_fx_quality=None,
        renderer=None,
        transport=None,
        fixed_fps=False,
        preview=str(output),
        test_color=None,
        snapshot=None,
        once=False,
        frames=None,
    )

    assert run(args) == 0
    with Image.open(output) as image:
        assert image.size == (1920, 462)
        assert image.format == "JPEG"
