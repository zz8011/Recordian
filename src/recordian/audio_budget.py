"""Bound untrusted uploaded audio before it reaches a model."""
from __future__ import annotations

import io
import subprocess
import wave


def validate_wav(data: bytes, *, max_seconds: float = 120) -> None:
    try:
        with wave.open(io.BytesIO(data), 'rb') as wav:
            frames = wav.getnframes()
            rate = wav.getframerate()
            if not 8000 <= rate <= 192000 or not 1 <= wav.getnchannels() <= 8 or not 1 <= wav.getsampwidth() <= 4:
                raise ValueError('unsupported WAV parameters')
            if frames <= 0 or frames / rate > max_seconds or frames * wav.getnchannels() > 16_000_000:
                raise ValueError('audio exceeds the server duration/sample budget')
            # Count real payload bytes in bounded blocks; a forged data chunk
            # header must not make the model read a truncated WAV as valid.
            expected = frames * wav.getnchannels() * wav.getsampwidth()
            seen = 0
            while chunk := wav.readframes(4096):
                seen += len(chunk)
            if seen != expected:
                raise ValueError('truncated WAV payload')
    except (wave.Error, EOFError) as exc:
        raise ValueError('a valid uncompressed WAV file is required') from exc


def decode_pcm16(data: bytes, *, max_seconds: int = 120) -> bytes:
    """Decode a bounded prefix, rejecting over-budget audio rather than truncating."""
    proc = subprocess.run(['ffmpeg', '-v', 'error', '-protocol_whitelist', 'pipe', '-i', 'pipe:0',
                           '-t', str(max_seconds + 1), '-f', 's16le', '-ar', '16000', '-ac', '1', 'pipe:1'],
                          input=data, capture_output=True, timeout=60)
    if proc.returncode or not proc.stdout:
        raise ValueError('audio cannot be decoded')
    if len(proc.stdout) > 16000 * 2 * max_seconds:
        raise ValueError('audio exceeds the server duration/sample budget')
    return proc.stdout
