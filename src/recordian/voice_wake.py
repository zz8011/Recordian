from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from shutil import which
import subprocess
import sys
import threading
import time
from typing import Callable


EventCallback = Callable[[dict[str, object]], None]


@dataclass(slots=True)
class WakeModelConfig:
    encoder: str
    decoder: str
    joiner: str
    tokens: str
    provider: str = "cpu"
    num_threads: int = 2
    sample_rate: int = 16000
    tokens_type: str = "ppinyin"
    keywords_file: str = ""


@dataclass(slots=True)
class WakeRuntimeConfig:
    enabled: bool
    prefixes: list[str]
    names: list[str]
    cooldown_s: float
    keyword_score: float
    keyword_threshold: float
    auto_name_variants: bool = True
    auto_prefix_variants: bool = True
    allow_name_only: bool = True
    owner_verify_enabled: bool = False
    owner_profile_path: str = "~/.config/recordian/owner_voice_profile.json"
    owner_sample_path: str = ""
    owner_threshold: float = 0.72
    owner_window_s: float = 1.6


def normalize_tokens_type(value: str) -> str:
    token = value.strip().lower()
    if token in {"char", "cjkchar"}:
        # Legacy value "char" maps to sherpa's "cjkchar".
        # For current Chinese KWS preset we prefer ppinyin by default.
        return "ppinyin"
    if token in {"ppinyin", "fpinyin", "bpe", "cjkchar+bpe", "phone+ppinyin"}:
        return token
    return "ppinyin"


_COMMON_PREFIX_HOMOPHONE_MAP: dict[str, list[str]] = {
    "嘿": ["嗨", "黑"],
    "嗨": ["嘿", "海"],
}


def _expand_wake_name_variants(name: str) -> list[str]:
    """Return normalized name list; tone-level variants are expanded at token stage."""
    normalized = name.strip()
    if not normalized:
        return []
    return [normalized]


def _expand_wake_prefix_variants(prefix: str) -> list[str]:
    normalized = prefix.strip()
    if not normalized:
        return []
    variants: list[str] = [normalized]
    for idx, ch in enumerate(normalized):
        for rep in _COMMON_PREFIX_HOMOPHONE_MAP.get(ch, []):
            candidate = normalized[:idx] + rep + normalized[idx + 1 :]
            if candidate and candidate not in variants:
                variants.append(candidate)
    return variants


def build_wake_phrases(
    prefixes: list[str],
    names: list[str],
    *,
    auto_name_variants: bool = False,
    auto_prefix_variants: bool = False,
    allow_name_only: bool = False,
) -> list[str]:
    base_prefixes = [p.strip() for p in prefixes if p.strip()]
    normalized_prefixes: list[str] = []
    for base in base_prefixes:
        if auto_prefix_variants:
            for candidate in _expand_wake_prefix_variants(base):
                if candidate not in normalized_prefixes:
                    normalized_prefixes.append(candidate)
        elif base not in normalized_prefixes:
            normalized_prefixes.append(base)
    base_names = [n.strip() for n in names if n.strip()]
    normalized_names: list[str] = []
    for base in base_names:
        if auto_name_variants:
            for candidate in _expand_wake_name_variants(base):
                if candidate not in normalized_names:
                    normalized_names.append(candidate)
        elif base not in normalized_names:
            normalized_names.append(base)
    phrases: list[str] = []
    if allow_name_only:
        for name in normalized_names:
            if name and name not in phrases:
                phrases.append(name)
    for prefix in normalized_prefixes:
        for name in normalized_names:
            phrase = f"{prefix}{name}"
            if phrase and phrase not in phrases:
                phrases.append(phrase)
    return phrases


def _normalize_list(value: object, *, fallback: list[str]) -> list[str]:
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",") if item.strip()]
        return items or list(fallback)
    if isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value if str(item).strip()]
        return items or list(fallback)
    return list(fallback)


def _normalize_tone_token(token: str) -> str:
    normalized = token.strip().lower()
    if not normalized:
        return ""
    try:
        from pypinyin.contrib.tone_convert import to_normal

        converted = to_normal(normalized).strip().lower()
        return converted or normalized
    except Exception:
        return normalized


def _load_tone_variant_groups(tokens_path: Path) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    try:
        content = tokens_path.read_text(encoding="utf-8")
    except Exception:
        return groups
    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue
        token = line.split()[0].strip()
        if not token or token.startswith("<"):
            continue
        key = _normalize_tone_token(token)
        if not key:
            continue
        bucket = groups.setdefault(key, [])
        if token not in bucket:
            bucket.append(token)
    return {key: values for key, values in groups.items() if len(values) > 1}


def _expand_row_with_tone_variants(
    token_row: list[str],
    *,
    tone_groups: dict[str, list[str]],
    max_rows: int = 8,
    max_positions: int = 3,
    max_alts_per_pos: int = 2,
) -> list[list[str]]:
    rows: list[list[str]] = [list(token_row)]
    if not tone_groups:
        return rows

    mutable_positions: list[tuple[int, list[str]]] = []
    for idx, token in enumerate(token_row):
        key = _normalize_tone_token(token)
        alternatives = [candidate for candidate in tone_groups.get(key, []) if candidate != token]
        if alternatives:
            mutable_positions.append((idx, alternatives[:max_alts_per_pos]))

    for idx, alternatives in mutable_positions[:max_positions]:
        base_rows = list(rows)
        for base in base_rows:
            for alt in alternatives:
                candidate = list(base)
                candidate[idx] = alt
                if candidate in rows:
                    continue
                rows.append(candidate)
                if len(rows) >= max_rows:
                    return rows
    return rows


def ensure_keywords_file(
    *,
    phrases: list[str],
    tokens_path: Path,
    tokens_type: str,
    score: float,
    threshold: float,
    cache_dir: Path,
    auto_tone_variants: bool = True,
) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    raw_path = cache_dir / "keywords_raw.txt"
    out_path = cache_dir / "keywords.txt"

    raw_lines = [f"{phrase} @{phrase} :{score:.2f} #{threshold:.2f}" for phrase in phrases]
    raw_header = f"# tokens_type={tokens_type} auto_tone_variants={int(bool(auto_tone_variants))}"
    raw_content = "\n".join([raw_header, *raw_lines]) + "\n"

    def _is_cache_valid() -> bool:
        if not raw_path.exists() or not out_path.exists():
            return False
        try:
            if raw_path.read_text(encoding="utf-8") != raw_content:
                return False
            out_lines = [
                line.strip()
                for line in out_path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
        except Exception:  # noqa: BLE001
            return False
        if len(out_lines) < len(raw_lines):
            return False
        return all(" @" in line and " :" in line and " #" in line for line in out_lines)

    if _is_cache_valid():
        return out_path

    raw_path.write_text(raw_content, encoding="utf-8")

    # Prefer python API: avoids PATH issues when app is launched by desktop entry.
    try:
        from sherpa_onnx.utils import text2token

        encoded = text2token(
            texts=phrases,
            tokens=str(tokens_path),
            tokens_type=tokens_type,
        )
        if len(encoded) == len(phrases):
            tone_groups: dict[str, list[str]] = {}
            if bool(auto_tone_variants) and tokens_type in {"ppinyin", "fpinyin"}:
                tone_groups = _load_tone_variant_groups(tokens_path)
            output_lines: list[str] = []
            seen_lines: set[str] = set()
            for phrase, row in zip(phrases, encoded):
                token_items = [str(item).strip() for item in row if str(item).strip()]
                if not token_items:
                    raise RuntimeError("empty_token_row")
                token_rows = _expand_row_with_tone_variants(token_items, tone_groups=tone_groups)
                for tokens_row in token_rows:
                    line = f"{' '.join(tokens_row)} @{phrase} :{score:.2f} #{threshold:.2f}"
                    if line in seen_lines:
                        continue
                    output_lines.append(line)
                    seen_lines.add(line)
            out_path.write_text("\n".join(output_lines) + "\n", encoding="utf-8")
            if _is_cache_valid():
                return out_path
    except Exception:
        pass

    # Fallback to CLI for environments where python API is unavailable.
    cli_bin = which("sherpa-onnx-cli")
    if cli_bin is None:
        candidate = Path(sys.executable).parent / "sherpa-onnx-cli"
        if candidate.exists():
            cli_bin = str(candidate)

    if cli_bin is None:
        raise RuntimeError(
            "关键词转换失败：未找到 sherpa-onnx-cli，且 python API 不可用。"
            " 请安装 sherpa-onnx 和 pypinyin。"
        )

    cmd = [
        cli_bin,
        "text2token",
        "--tokens",
        str(tokens_path),
        "--tokens-type",
        tokens_type,
        str(raw_path),
        str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not _is_cache_valid():
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(
            "关键词转换失败，请检查 tokens_type 与依赖（ppinyin 需要 pypinyin）。"
            f" detail={detail}"
        )
    return out_path


class VoiceWakeService:
    def __init__(
        self,
        *,
        model: WakeModelConfig,
        runtime: WakeRuntimeConfig,
        on_wake: Callable[[str], None],
        on_event: EventCallback,
        cache_dir: Path,
    ) -> None:
        self.model = model
        self.runtime = runtime
        self.on_wake = on_wake
        self.on_event = on_event
        self.cache_dir = cache_dir

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_trigger = 0.0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="recordian-voice-wake")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def _emit(self, payload: dict[str, object]) -> None:
        payload.setdefault("event", "log")
        self.on_event(payload)

    def _check_model_files(self) -> None:
        required = [
            Path(self.model.encoder),
            Path(self.model.decoder),
            Path(self.model.joiner),
            Path(self.model.tokens),
        ]
        missing = [str(p) for p in required if not p.exists()]
        if missing:
            raise FileNotFoundError(
                "语音唤醒模型文件缺失: " + ", ".join(missing)
            )

    def _resolve_keywords_file(self) -> Path:
        explicit = self.model.keywords_file.strip()
        if explicit:
            p = Path(explicit).expanduser()
            if not p.exists():
                raise FileNotFoundError(f"keywords_file 不存在: {p}")
            return p

        phrases = build_wake_phrases(
            self.runtime.prefixes,
            self.runtime.names,
            auto_name_variants=bool(getattr(self.runtime, "auto_name_variants", True)),
            auto_prefix_variants=bool(getattr(self.runtime, "auto_prefix_variants", True)),
            allow_name_only=bool(getattr(self.runtime, "allow_name_only", True)),
        )
        if not phrases:
            raise RuntimeError("唤醒词为空，请检查 wake_prefix/wake_name 配置。")
        preview = ", ".join(phrases[:8])
        self._emit({"message": f"voice_wake_phrases count={len(phrases)} preview={preview}"})

        return ensure_keywords_file(
            phrases=phrases,
            tokens_path=Path(self.model.tokens),
            tokens_type=self.model.tokens_type,
            score=self.runtime.keyword_score,
            threshold=self.runtime.keyword_threshold,
            cache_dir=self.cache_dir,
            auto_tone_variants=bool(getattr(self.runtime, "auto_name_variants", True)),
        )

    def _run(self) -> None:
        try:
            import numpy as np
            import sherpa_onnx
            import sounddevice as sd
        except Exception as exc:  # noqa: BLE001
            self._emit({"message": f"voice_wake_disabled: {type(exc).__name__}: {exc}"})
            return

        try:
            self._check_model_files()
            keywords_file = self._resolve_keywords_file()
        except Exception as exc:  # noqa: BLE001
            self._emit({"message": f"voice_wake_setup_failed: {exc}"})
            return

        try:
            spotter = sherpa_onnx.KeywordSpotter(
                tokens=self.model.tokens,
                encoder=self.model.encoder,
                decoder=self.model.decoder,
                joiner=self.model.joiner,
                keywords_file=str(keywords_file),
                num_threads=self.model.num_threads,
                provider=self.model.provider,
            )
            stream = spotter.create_stream()
        except Exception as exc:  # noqa: BLE001
            self._emit({"message": f"voice_wake_init_failed: {exc}"})
            return

        samples_per_read = int(self.model.sample_rate * 0.1)
        owner_verify_enabled = bool(getattr(self.runtime, "owner_verify_enabled", False))
        owner_threshold = min(0.99, max(0.0, float(getattr(self.runtime, "owner_threshold", 0.72))))
        owner_window_s = max(0.6, float(getattr(self.runtime, "owner_window_s", 1.6)))
        owner_noise_suppression = int(getattr(self.runtime, "owner_noise_suppression", 1))
        owner_noise_suppression = max(0, min(2, owner_noise_suppression))  # Clamp to 0-2
        owner_embeddings: list[list[float]] | None = None
        _extract_speaker_embedding = None
        _cosine_similarity = None
        if owner_verify_enabled:
            try:
                from .speaker_verify import (
                    cosine_similarity,
                    enroll_speaker_profile_from_wav,
                    extract_speaker_embedding,
                    load_speaker_profile,
                )

                profile_path = Path(str(getattr(self.runtime, "owner_profile_path", "~/.config/recordian/owner_voice_profile.json"))).expanduser()
                sample_path_raw = str(getattr(self.runtime, "owner_sample_path", "")).strip()
                sample_path = Path(sample_path_raw).expanduser() if sample_path_raw else None

                profile = load_speaker_profile(profile_path)
                if profile is None and sample_path is not None and sample_path.exists():
                    profile = enroll_speaker_profile_from_wav(
                        sample_path=sample_path,
                        profile_path=profile_path,
                        target_rate=self.model.sample_rate,
                    )
                    self._emit({"message": f"voice_wake_owner_profile_enrolled: {profile_path}"})
                if profile is None:
                    owner_verify_enabled = False
                    self._emit({"message": "voice_wake_owner_verify_disabled: profile_not_found"})
                elif profile.feature_version != 2:
                    owner_verify_enabled = False
                    self._emit(
                        {
                            "message": (
                                f"voice_wake_owner_verify_disabled: profile_version_mismatch "
                                f"(expected=2, got={profile.feature_version}). "
                                "Please re-enroll owner profile with current version."
                            )
                        }
                    )
                else:
                    owner_embeddings = list(profile.embeddings) if profile.embeddings else [profile.embedding]
                    _extract_speaker_embedding = extract_speaker_embedding
                    _cosine_similarity = cosine_similarity
                    self._emit(
                        {
                            "message": (
                                "voice_wake_owner_verify_enabled"
                                f" threshold={owner_threshold:.2f}"
                                f" window_s={owner_window_s:.2f}"
                                f" samples={len(owner_embeddings)}"
                                f" noise_suppression={owner_noise_suppression}"
                            )
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                owner_verify_enabled = False
                self._emit({"message": f"voice_wake_owner_verify_disabled: {type(exc).__name__}: {exc}"})

        owner_audio_chunks = None
        owner_audio_samples = 0
        owner_max_samples = max(samples_per_read, int(self.model.sample_rate * owner_window_s))
        if owner_verify_enabled:
            from collections import deque

            owner_audio_chunks = deque(maxlen=100)

        self._emit({"message": "voice_wake_ready"})
        try:
            with sd.InputStream(
                channels=1,
                samplerate=self.model.sample_rate,
                dtype="float32",
                blocksize=samples_per_read,
            ) as mic:
                while not self._stop.is_set():
                    audio, _ = mic.read(samples_per_read)
                    samples = np.ascontiguousarray(audio.reshape(-1))
                    if owner_verify_enabled and owner_audio_chunks is not None:
                        owner_audio_chunks.append(samples.copy())
                        owner_audio_samples += int(samples.size)
                        while owner_audio_samples > owner_max_samples and owner_audio_chunks:
                            owner_audio_samples -= int(owner_audio_chunks.popleft().size)

                    stream.accept_waveform(self.model.sample_rate, samples)
                    while spotter.is_ready(stream):
                        spotter.decode_stream(stream)

                    result = spotter.get_result(stream).strip()
                    if not result:
                        continue

                    spotter.reset_stream(stream)
                    now = time.monotonic()
                    if now - self._last_trigger < max(0.0, self.runtime.cooldown_s):
                        self._emit({"message": f"voice_wake_ignored_in_cooldown: {result}"})
                        continue

                    if (
                        owner_verify_enabled
                        and owner_embeddings is not None
                        and _extract_speaker_embedding is not None
                        and _cosine_similarity is not None
                        and owner_audio_chunks is not None
                    ):
                        if owner_audio_samples < max(samples_per_read, int(self.model.sample_rate * 0.35)):
                            self._emit({"message": "voice_wake_rejected_speaker: insufficient_audio"})
                            continue
                        try:
                            verify_samples = np.concatenate(list(owner_audio_chunks))
                            if verify_samples.size > owner_max_samples:
                                verify_samples = verify_samples[-owner_max_samples:]
                            candidate_embedding = _extract_speaker_embedding(
                                verify_samples,
                                sample_rate=self.model.sample_rate,
                                target_rate=self.model.sample_rate,
                                noise_suppression=owner_noise_suppression,
                            )
                            # Use max similarity strategy: compare with all enrolled samples
                            similarity = max(
                                float(_cosine_similarity(candidate_embedding, owner_emb))
                                for owner_emb in owner_embeddings
                            )
                        except Exception as exc:  # noqa: BLE001
                            self._emit({"message": f"voice_wake_rejected_speaker: feature_error={type(exc).__name__}"})
                            continue
                        if similarity < owner_threshold:
                            self._emit(
                                {
                                    "message": (
                                        "voice_wake_rejected_speaker"
                                        f" score={similarity:.3f}"
                                        f" threshold={owner_threshold:.3f}"
                                    )
                                }
                            )
                            continue

                    self._last_trigger = now
                    self._emit({"event": "voice_wake_triggered", "keyword": result})
                    self.on_wake(result)
        except Exception as exc:  # noqa: BLE001
            self._emit({"message": f"voice_wake_runtime_error: {type(exc).__name__}: {exc}"})


def make_wake_model_config(args: argparse.Namespace) -> WakeModelConfig:
    return WakeModelConfig(
        encoder=str(getattr(args, "wake_encoder", "")),
        decoder=str(getattr(args, "wake_decoder", "")),
        joiner=str(getattr(args, "wake_joiner", "")),
        tokens=str(getattr(args, "wake_tokens", "")),
        provider=str(getattr(args, "wake_provider", "cpu")),
        num_threads=int(getattr(args, "wake_num_threads", 2)),
        sample_rate=int(getattr(args, "wake_sample_rate", 16000)),
        tokens_type=normalize_tokens_type(str(getattr(args, "wake_tokens_type", "ppinyin"))),
        keywords_file=str(getattr(args, "wake_keywords_file", "")),
    )


def make_wake_runtime_config(args: argparse.Namespace) -> WakeRuntimeConfig:
    prefixes = _normalize_list(getattr(args, "wake_prefix", ["嗨", "嘿"]), fallback=["嗨", "嘿"])
    names = _normalize_list(getattr(args, "wake_name", ["小二"]), fallback=["小二"])
    return WakeRuntimeConfig(
        enabled=bool(getattr(args, "enable_voice_wake", False)),
        prefixes=prefixes,
        names=names,
        cooldown_s=float(getattr(args, "wake_cooldown_s", 3.0)),
        keyword_score=float(getattr(args, "wake_keyword_score", 1.5)),
        keyword_threshold=float(getattr(args, "wake_keyword_threshold", 0.25)),
        auto_name_variants=bool(getattr(args, "wake_auto_name_variants", True)),
        auto_prefix_variants=bool(getattr(args, "wake_auto_prefix_variants", True)),
        allow_name_only=bool(getattr(args, "wake_allow_name_only", True)),
        owner_verify_enabled=bool(getattr(args, "wake_owner_verify", False)),
        owner_profile_path=str(getattr(args, "wake_owner_profile", "~/.config/recordian/owner_voice_profile.json")),
        owner_sample_path=str(getattr(args, "wake_owner_sample", "")),
        owner_threshold=float(getattr(args, "wake_owner_threshold", 0.72)),
        owner_window_s=float(getattr(args, "wake_owner_window_s", 1.6)),
    )
