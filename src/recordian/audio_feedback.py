from __future__ import annotations

from pathlib import Path
from shutil import which
import subprocess
import wave


def default_beep_path() -> Path:
    project_root = Path(__file__).parent.parent.parent
    return project_root / "assets" / "wake-beep.wav"


def default_sound_on_path() -> Path:
    project_root = Path(__file__).parent.parent.parent
    return project_root / "assets" / "wake-on.mp3"


def default_sound_off_path() -> Path:
    project_root = Path(__file__).parent.parent.parent
    return project_root / "assets" / "wake-off.mp3"


def resolve_beep_path(custom_path: str | Path | None) -> Path | None:
    if custom_path:
        path = Path(custom_path).expanduser()
        if path.exists():
            return path
    fallback = default_beep_path()
    if fallback.exists():
        return fallback
    return None


def resolve_sound_path(
    *,
    cue: str,
    custom_path: str | Path | None = None,
    legacy_beep_path: str | Path | None = None,
) -> Path | None:
    if custom_path:
        path = Path(custom_path).expanduser()
        if path.exists():
            return path

    if legacy_beep_path:
        legacy = Path(legacy_beep_path).expanduser()
        if legacy.exists():
            return legacy

    if cue == "on":
        fallback = default_sound_on_path()
    elif cue == "off":
        fallback = default_sound_off_path()
    else:
        fallback = default_beep_path()

    if fallback.exists():
        return fallback
    return None


def _play_with_external_player(path: Path) -> bool:
    candidates: list[list[str]] = []
    if which("ffplay"):
        candidates.append(["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)])
    if which("paplay"):
        candidates.append(["paplay", str(path)])
    if which("mpg123"):
        candidates.append(["mpg123", "-q", str(path)])

    for cmd in candidates:
        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:
            continue
    return False


def _play_wav_nonblocking(path: Path) -> bool:
    try:
        import numpy as np
        import sounddevice as sd
    except Exception:
        return False

    try:
        with wave.open(str(path), "rb") as wf:
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            sample_rate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
    except Exception:
        return False

    if sampwidth != 2 or channels < 1:
        return False

    data = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)

    try:
        sd.play(data, sample_rate, blocking=False)
        return True
    except Exception:
        return False


def play_sound(
    *,
    cue: str,
    custom_path: str | Path | None = None,
    legacy_beep_path: str | Path | None = None,
) -> bool:
    path = resolve_sound_path(cue=cue, custom_path=custom_path, legacy_beep_path=legacy_beep_path)
    if path is None:
        return False

    if path.suffix.lower() == ".wav":
        # WAV 优先使用内置播放，延迟更低。
        try:
            import numpy as np  # noqa: F401
            import sounddevice as sd  # noqa: F401
            return _play_wav_nonblocking(path)
        except Exception:
            pass

    return _play_with_external_player(path)


def play_beep(custom_path: str | Path | None = None) -> bool:
    """Backward-compat helper for old callers."""
    return play_sound(cue="on", custom_path=custom_path)
