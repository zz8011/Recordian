from pathlib import Path

import pytest

from recordian import tray_menu


@pytest.mark.parametrize(
    "status,group",
    [
        ("idle", "idle"),
        ("starting", "preparing"),
        ("warming", "preparing"),
        ("recording", "recording"),
        ("processing", "processing"),
        ("busy", "processing"),
        ("error", "error"),
        ("stopped", "stopped"),
        ("unknown", "idle"),
    ],
)
def test_real_statuses_resolve_packaged_final_artwork(status, group):
    path = tray_menu.get_logo_path(status)
    assert path.name == f"recordian-tray-{group}-color-32.png"
    assert path.is_file()
    assert path.parent == Path(tray_menu.__file__).parent / "ui_assets"


def test_missing_state_falls_back_to_idle_then_app_icon(tmp_path, monkeypatch):
    monkeypatch.setattr(tray_menu, "UI_ASSETS_DIR", tmp_path)
    idle = tmp_path / "recordian-tray-idle-color-32.png"
    idle.write_bytes(b"fixture")
    assert tray_menu.get_logo_path("processing") == idle
    idle.unlink()
    app_icon = tmp_path / "recordian-app-64.png"
    app_icon.write_bytes(b"fixture")
    assert tray_menu.get_logo_path("error") == app_icon


def test_tray_redraws_only_when_visual_group_changes(monkeypatch):
    from types import SimpleNamespace

    icons = []
    monkeypatch.setattr(tray_menu, "sync_appindicator_preset_submenu", lambda app: None)
    monkeypatch.setattr(tray_menu, "status_summary_label", lambda state, config: "status")
    app = SimpleNamespace(
        indicator=SimpleNamespace(set_icon=icons.append),
        _glib=SimpleNamespace(idle_add=lambda fn: fn()),
        state=SimpleNamespace(status="starting", last_run=SimpleNamespace(text="")),
        _get_cached_config=lambda: {},
    )
    for state in ("starting", "warming", "processing", "busy"):
        app.state.status = state
        tray_menu.update_tray_menu(app)
    assert [Path(p).name for p in icons] == [
        "recordian-tray-preparing-color-32.png",
        "recordian-tray-processing-color-32.png",
    ]
