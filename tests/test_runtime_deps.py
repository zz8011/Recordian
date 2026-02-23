import os

from recordian import runtime_deps


def test_ensure_ffmpeg_available_use_existing(monkeypatch) -> None:
    monkeypatch.setattr(runtime_deps.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setenv("PATH", "/usr/bin")

    path = runtime_deps.ensure_ffmpeg_available()

    assert path == "/usr/bin/ffmpeg"
    assert os.environ["PATH"] == "/usr/bin"


def test_ensure_ffmpeg_available_fallback_imageio(monkeypatch, tmp_path) -> None:
    ffmpeg_bin = tmp_path / "ffmpeg-linux-x86_64-v7.0.2"
    ffmpeg_bin.write_text("", encoding="utf-8")

    class _FakeImageio:
        @staticmethod
        def get_ffmpeg_exe() -> str:
            return str(ffmpeg_bin)

    monkeypatch.setattr(runtime_deps.shutil, "which", lambda _: None)
    monkeypatch.setattr(runtime_deps, "_load_imageio_ffmpeg", lambda: _FakeImageio)
    monkeypatch.setattr(runtime_deps.Path, "home", lambda *args, **kwargs: tmp_path)
    monkeypatch.setenv("PATH", "/usr/local/bin")
    path = runtime_deps.ensure_ffmpeg_available()

    expected_shim = tmp_path / ".cache" / "recordian" / "bin" / "ffmpeg"
    assert path == str(expected_shim)
    assert expected_shim.exists()
    assert os.environ["PATH"].split(os.pathsep)[0] == str(expected_shim.parent)
