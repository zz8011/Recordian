from __future__ import annotations

from pathlib import Path

from recordian.audio_feedback import default_beep_path, resolve_beep_path, resolve_sound_path


def test_default_beep_path_points_to_assets() -> None:
    path = default_beep_path()
    assert str(path).endswith("assets/wake-beep.wav")


def test_resolve_beep_path_prefers_custom(tmp_path: Path) -> None:
    custom = tmp_path / "beep.wav"
    custom.write_bytes(b"dummy")
    assert resolve_beep_path(custom) == custom


def test_resolve_sound_path_prefers_custom(tmp_path: Path) -> None:
    custom = tmp_path / "on.mp3"
    custom.write_bytes(b"dummy")
    assert resolve_sound_path(cue="on", custom_path=custom) == custom


def test_resolve_sound_path_fallback_to_legacy(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.wav"
    legacy.write_bytes(b"dummy")
    assert resolve_sound_path(cue="off", legacy_beep_path=legacy) == legacy
