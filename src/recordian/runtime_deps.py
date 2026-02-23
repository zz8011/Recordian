from __future__ import annotations

import os
from pathlib import Path
import shutil


def _load_imageio_ffmpeg():
    try:
        import imageio_ffmpeg
    except Exception:
        return None
    return imageio_ffmpeg


def ensure_ffmpeg_available() -> str | None:
    """Ensure ffmpeg can be found in PATH.

    Strategy:
    1) Reuse existing system/user ffmpeg if already present.
    2) Fallback to imageio-ffmpeg bundled binary and prepend its folder to PATH.
    """
    existing = shutil.which("ffmpeg")
    if existing:
        return existing

    imageio_ffmpeg = _load_imageio_ffmpeg()
    if imageio_ffmpeg is None:
        return None

    ffmpeg_exe = Path(imageio_ffmpeg.get_ffmpeg_exe())
    if not ffmpeg_exe.exists():
        return None

    shim_dir = Path.home() / ".cache" / "recordian" / "bin"
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim_path = shim_dir / "ffmpeg"
    if not shim_path.exists():
        try:
            shim_path.symlink_to(ffmpeg_exe)
        except OSError:
            # Fallback for filesystems that do not support symlink.
            shim_path.write_bytes(ffmpeg_exe.read_bytes())
            shim_path.chmod(0o755)

    ffmpeg_dir = str(shim_dir)
    current_path = os.environ.get("PATH", "")
    path_parts = current_path.split(os.pathsep) if current_path else []
    if ffmpeg_dir not in path_parts:
        os.environ["PATH"] = ffmpeg_dir + (os.pathsep + current_path if current_path else "")
    return str(shim_path)
