#!/usr/bin/env python3
"""Four-arm ASR A/B eval harness for Recordian (wave runner + reporter).

Arms
  A  Confucius4-R2T2 Q4_K_M (production weights) — standalone llama-native
     streaming server on a scratch port, full audio replayed through the
     repo's ConfuciusASRProvider (same pattern as the Oct-2026 experiment).
  B  Qwen3-ASR-1.7B (local HF weights) via the repo's QwenASRProvider,
     offline decode of the full audio.
  C  Two-pass simulation — computed in the report, NOT a separate run:
     final text is arm B's output on the full segment audio, so C's CER is
     B's CER by construction; C's latency = A's streaming latency + B's
     decode latency.
  D  Confucius4-R2T2 Q8_0 decoder (official netease-youdao GGUF) — same
     server pattern as A, Q8 weights.

Scoring: house CER standard from the Oct-2026 experiment — normalize with
format_spoken_text, lowercase, strip everything except word chars and CJK,
then Levenshtein. Micro-averaged over the dataset.

Datasets: a directory of <stem>.wav + <stem>.txt pairs (a corpus.json with
{id, file, reference} rows is used for references when a .txt is missing),
or an eval-capture directory (index.jsonl + audio/) whose reference is the
captured final_text (NOT gold until edited_text is marked — see report).

Usage
  python3 tools/asr_ab_eval.py run --arm A --dataset DIR --out A.json
  python3 tools/asr_ab_eval.py run --arm B --dataset DIR --out B.json
  python3 tools/asr_ab_eval.py run --arm D --dataset DIR --out D.json
  python3 tools/asr_ab_eval.py report --inputs A.json B.json D.json --out REPORT.md

Run arms sequentially: each arm loads its model in its own process and the
GPU (RTX 4070 Laptop 8GB) is shared with the production :8321 server,
which this harness never touches (arms A/D use scratch ports 8331/8332).
Confucius arms split clips over 24s into per-session chunks (production
never feeds more than 24s per session) and run the scratch server with a
120s session budget; scratch-server output is kept in server-<arm>.log.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

HOME = Path.home()
MAIN_REPO = HOME / "Projects/recordian"
WORKTREE_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(WORKTREE_SRC))

from recordian.benchmark import edit_distance  # noqa: E402
from recordian.spoken_formatting import format_spoken_text  # noqa: E402

BUILTIN_TERMS = ["sinktalk", "magpie", "gufo", "recordian", "jev"]
ASR_VENV_PYTHON = str(MAIN_REPO / ".venv-asr/bin/python")
TOKEN_FILE = HOME / ".config/recordian/confucius_server_token.txt"
HOTKEY_CONFIG = HOME / ".config/recordian/hotkey.json"
QWEN_MODEL_DIR = MAIN_REPO / "models/Qwen3-ASR-1.7B"
Q8_GGUF_DIR = HOME / ".local/share/recordian/eval-models/q8"
PROD_GGUF_DIR = MAIN_REPO / "models/Confucius4-R2T2-GGUF"

# Scratch-server session budget: production caps a Confucius session at 30s
# of audio (server DEFAULT_MAX_SESSION_SECONDS) and the eval set has 12 clips
# over 30s (longest 98s). Raising the SCRATCH budget to 120s removes the hard
# rejection; chunking below keeps each session in the regime production
# itself uses (duration_guard.CONTINUOUS_SEGMENT_MAX_S = 24s), where the
# streaming model stays near realtime (a whole 98s file in one session never
# finishes: per-chunk cost grows with accumulated audio and EOS was still
# unreached after 180s).
SCRATCH_MAX_SESSION_SECONDS = 120.0
CONFUCIUS_CHUNK_S = 24.0


def norm(text: str) -> str:
    """October house standard: spoken formatting, lowercase, keep word/CJK."""
    return re.sub(r"[^\w\u4e00-\u9fff]", "", format_spoken_text(text).lower())


def wav_duration_s(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate())


def load_dataset(dataset_dir: Path) -> list[dict]:
    index = dataset_dir / "index.jsonl"
    if index.exists():
        rows = []
        for line in index.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("audio_missing") or not row.get("audio_file"):
                continue
            rows.append(
                {
                    "id": row["id"],
                    "wav": dataset_dir / row["audio_file"],
                    # edited (gold) text wins when a gold-marking pass filled it
                    "reference": row.get("edited_text") or row["final_text"],
                    "gold": bool(row.get("gold")),
                }
            )
        return rows
    corpus_refs: dict[str, str] = {}
    corpus = dataset_dir / "corpus.json"
    if corpus.exists():
        for row in json.loads(corpus.read_text(encoding="utf-8")):
            corpus_refs[row["id"]] = row["reference"]
    rows = []
    for wav in sorted(dataset_dir.glob("*.wav")):
        stem = wav.stem
        txt = dataset_dir / f"{stem}.txt"
        if txt.exists():
            reference = txt.read_text(encoding="utf-8").strip()
        elif stem in corpus_refs:
            reference = corpus_refs[stem]
        else:
            print(f"skip {wav.name}: no reference", file=sys.stderr)
            continue
        rows.append({"id": stem, "wav": wav, "reference": reference, "gold": True})
    return rows


def score_clip(reference: str, hypothesis: str) -> dict:
    ref_n = norm(reference)
    hyp_n = norm(hypothesis)
    errors = edit_distance(ref_n, hyp_n)
    term_misses = [
        term for term in BUILTIN_TERMS if term in ref_n and term not in hyp_n
    ]
    latin_ref = re.findall(r"[a-z][a-z0-9.+-]{2,}", reference.lower())
    latin_hyp = hypothesis.lower()
    latin_survived = sum(1 for tok in latin_ref if tok in latin_hyp)
    return {
        "ref_chars": len(ref_n),
        "errors": errors,
        "cer": (errors / len(ref_n)) if ref_n else 0.0,
        "term_misses": term_misses,
        "latin_ref_tokens": len(latin_ref),
        "latin_survived": latin_survived,
    }


def load_context() -> str:
    try:
        cfg = json.loads(HOTKEY_CONFIG.read_text(encoding="utf-8"))
        return str(cfg.get("asr_context", ""))
    except Exception:
        return ""


def run_confuius_arm(arm: str, clips: list[dict], context: str, *, server_log: Path | None = None,
                      chunk_s: float | None = None, sink: list[dict] | None = None) -> list[dict]:
    if arm == "A":
        gguf_dir, gguf_model, port = PROD_GGUF_DIR, "Confucius4-R2T2-Q4_K_M.gguf", 8331
    else:  # D
        gguf_dir, gguf_model, port = Q8_GGUF_DIR, "Confucius4-R2T2-Q8_0.gguf", 8332
        mmproj_link = gguf_dir / "mmproj-Confucius4-R2T2-Q8_0.gguf"
        if not mmproj_link.exists():
            mmproj_link.symlink_to(
                HOME / ".local/share/whisper/models/mmproj-Confucius4-R2T2-Q8_0.gguf"
            )
    import tempfile

    ready_file = Path(tempfile.mkdtemp(prefix=f"asr-ab-{arm}-")) / "ready.json"
    cmd = [
        ASR_VENV_PYTHON,
        "server/confucius_streaming_server.py",
        "--backend", "llama-native",
        "--gguf-dir", str(gguf_dir),
        "--gguf-model", gguf_model,
        "--model-dir", str(MAIN_REPO / "models/Confucius4-R2T2"),
        "--r2t2-source", str(MAIN_REPO / "runtime/Confucius4-R2T2-gguf"),
        "--max-model-len", "4096",
        "--host", "127.0.0.1",
        "--port", str(port),
        "--max-queued-frames", "512",
        "--max-session-seconds", str(SCRATCH_MAX_SESSION_SECONDS),
        "--token-file", str(TOKEN_FILE),
        "--warmup-wav", str(MAIN_REPO / "runtime/Confucius4-R2T2/resources/test.wav"),
        "--ready-file", str(ready_file),
    ]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(MAIN_REPO / "src")
    env["RECORDIAN_NATIVE_CORE"] = "required"
    gguf_libs = str(MAIN_REPO / "runtime/gguf-cuda-libs")
    env["LD_LIBRARY_PATH"] = gguf_libs + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    # Scratch-server output used to go to DEVNULL, which hid the server side of
    # every failure (the client deliberately never echoes the server error
    # detail). Keep it in a file next to the arm output instead.
    log_fp = open(server_log, "wb") if server_log is not None else None
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(MAIN_REPO),
            env=env,
            stdout=log_fp if log_fp is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if log_fp is not None else subprocess.DEVNULL,
            start_new_session=True,
        )
        return _run_confuius_with_server(proc, ready_file, arm, clips, context, port, chunk_s, sink)
    finally:
        if log_fp is not None:
            log_fp.close()


def _run_confuius_with_server(proc, ready_file, arm, clips, context, port, chunk_s, sink) -> list[dict]:
    try:
        deadline = time.monotonic() + 300
        while not ready_file.exists():
            if proc.poll() is not None:
                raise RuntimeError(f"arm {arm} server exited early rc={proc.returncode}")
            if time.monotonic() > deadline:
                raise RuntimeError(f"arm {arm} server not ready in 300s")
            time.sleep(1.0)
        from recordian.providers.confucius_asr import ConfuciusASRProvider

        provider = ConfuciusASRProvider(
            f"ws://127.0.0.1:{port}/asr_stream_api_v1",
            api_key=TOKEN_FILE.read_text(encoding="utf-8").strip(),
            context=context,
            timeout_s=60,
        )
        return transcribe_all(provider, clips, chunk_s=chunk_s, sink=sink)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=20)
        except Exception:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                pass


def run_qwen_arm(clips: list[dict], context: str) -> list[dict]:
    from recordian.providers.qwen_asr import QwenASRProvider

    provider = QwenASRProvider(
        model_name=str(QWEN_MODEL_DIR),
        device="cuda:0",
        dtype="bfloat16",
        language=None,
        max_new_tokens=1024,
        context=context,
    )
    return transcribe_all(provider, clips)


def chunk_wav(wav: Path, chunk_s: float, tmpdir: Path) -> list[Path]:
    """Split a wav into <= chunk_s segments (same format); returns part paths."""
    with wave.open(str(wav), "rb") as handle:
        params = handle.getparams()
        pcm = handle.readframes(handle.getnframes())
    frames_per_chunk = max(1, int(chunk_s * params.framerate))
    bytes_per_frame = params.sampwidth * params.nchannels
    total_frames = len(pcm) // bytes_per_frame
    parts: list[Path] = []
    for i, start in enumerate(range(0, total_frames, frames_per_chunk)):
        seg = pcm[start * bytes_per_frame : (start + frames_per_chunk) * bytes_per_frame]
        part = tmpdir / f"{wav.stem}-chunk{i}.wav"
        with wave.open(str(part), "wb") as out:
            out.setparams(params)
            out.writeframes(seg)
        parts.append(part)
    return parts


def _transcribe_once(provider, wav_path: Path) -> str:
    return str(getattr(provider.transcribe_file(wav_path, hotwords=[]), "text", "") or "")


def _transcribe_resilient(provider, wav_path: Path, split_s: float | None,
                          tmpdir: Path, depth: int = 0) -> tuple[str, int]:
    """Transcribe one file/chunk; returns (text, failed_chunks).

    Two fresh-session attempts, then (when a split size is given and this is
    the first level) split in halves and try each half the same way. A chunk
    that still fails is zero-filled and counted, so one bad decode never
    aborts the arm; the loss stays visible in the row's chunk_errors.
    """
    for _ in range(2):
        try:
            return _transcribe_once(provider, wav_path), 0
        except Exception:
            continue
    if split_s is None or depth >= 1:
        return "", 1
    texts: list[str] = []
    failed = 0
    for half in chunk_wav(wav_path, split_s / 2, tmpdir):
        text, err = _transcribe_resilient(provider, half, split_s / 2, tmpdir, depth + 1)
        texts.append(text)
        failed += err
    return "".join(texts), failed


def transcribe_all(provider, clips: list[dict], *, chunk_s: float | None = None,
                   sink: list[dict] | None = None) -> list[dict]:
    """Transcribe every clip; long clips are split into <= chunk_s sessions.

    A whole long file in one Confucius session never finishes (see module
    constants), so each chunk gets its own session and the hypotheses are
    concatenated. Each chunk is transcribed resiliently (retry, then split in
    halves, then zero-fill and count the loss) so one flaky native decode
    cannot abort the arm. ``sink`` (when given) receives each finished row
    immediately so a mid-run crash does not lose completed clips.
    """
    results = []
    with tempfile.TemporaryDirectory(prefix="asr-ab-chunks-") as tmp:
        for clip in clips:
            duration = wav_duration_s(clip["wav"])
            start = time.perf_counter()
            failed = 0
            if chunk_s and duration > chunk_s:
                parts = chunk_wav(clip["wav"], chunk_s, Path(tmp))
                hyp_parts: list[str] = []
                for part in parts:
                    text, err = _transcribe_resilient(provider, part, chunk_s, Path(tmp))
                    hyp_parts.append(text)
                    failed += err
                hyp = "".join(hyp_parts)
            elif chunk_s:
                hyp, failed = _transcribe_resilient(provider, clip["wav"], chunk_s, Path(tmp))
            else:
                result = provider.transcribe_file(clip["wav"], hotwords=[])
                hyp = str(getattr(result, "text", "") or "")
            elapsed = time.perf_counter() - start
            row = {
                "id": clip["id"],
                "gold": bool(clip.get("gold")),
                "chunk_errors": failed,
                "duration_s": round(duration, 3),
                "elapsed_s": round(elapsed, 3),
                "rtf": round(elapsed / duration, 4) if duration else 0.0,
                "hypothesis": hyp,
                **score_clip(clip["reference"], hyp),
            }
            results.append(row)
            if sink is not None:
                sink.append(row)
            print(f"  {clip['id']}: cer={row['cer']:.3f} rtf={row['rtf']:.3f}", flush=True)
    return results


def aggregate(results: list[dict]) -> dict:
    total_chars = sum(r["ref_chars"] for r in results)
    total_errors = sum(r["errors"] for r in results)
    total_audio = sum(r["duration_s"] for r in results)
    total_elapsed = sum(r["elapsed_s"] for r in results)
    return {
        "clips": len(results),
        "micro_cer": (total_errors / total_chars) if total_chars else 0.0,
        "total_errors": total_errors,
        "total_ref_chars": total_chars,
        "mean_rtf": (total_elapsed / total_audio) if total_audio else 0.0,
        "mean_elapsed_s": (total_elapsed / len(results)) if results else 0.0,
        "term_miss_clips": sum(1 for r in results if r["term_misses"]),
        "term_misses": [m for r in results for m in r["term_misses"]],
        "latin_ref_tokens": sum(r["latin_ref_tokens"] for r in results),
        "latin_survived": sum(r["latin_survived"] for r in results),
    }


def build_payload(arm: str, dataset_dir: Path, clips: list[dict], results: list[dict],
                  *, partial: bool = False, error: str = "") -> dict:
    payload = {
        "arm": arm,
        "dataset": str(dataset_dir),
        "gold_references": all(c.get("gold") for c in clips),
        "gold_clips": sum(1 for c in clips if c.get("gold")),
        "aggregate": aggregate(results),
        "clips": results,
    }
    if partial:
        payload["partial"] = True
        payload["error"] = error
    return payload


def cmd_run(args: argparse.Namespace) -> None:
    dataset_dir = Path(args.dataset).expanduser()
    clips = load_dataset(dataset_dir)
    if not clips:
        raise SystemExit(f"no clips found in {dataset_dir}")
    print(f"arm {args.arm}: {len(clips)} clips from {dataset_dir}")
    context = load_context()
    out = Path(args.out)
    completed: list[dict] = []
    try:
        if args.arm in ("A", "D"):
            server_log = out.with_name(f"server-{args.arm}.log")
            results = run_confuius_arm(
                args.arm, clips, context, server_log=server_log,
                chunk_s=CONFUCIUS_CHUNK_S, sink=completed,
            )
        elif args.arm == "B":
            results = run_qwen_arm(clips, context)
        else:
            raise SystemExit("arm C is computed by the report step, not run")
    except Exception as exc:
        # Never lose completed clips to a mid-run crash again: persist the
        # partial arm output, then re-raise so the failure stays visible.
        if completed:
            payload = build_payload(args.arm, dataset_dir, clips, completed,
                                    partial=True, error=f"{type(exc).__name__}: {exc}")
            out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"arm {args.arm} PARTIAL: {len(completed)}/{len(clips)} clips saved -> {out}")
        raise
    payload = build_payload(args.arm, dataset_dir, clips, results)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    agg = payload["aggregate"]
    print(
        f"arm {args.arm} done: micro CER={agg['micro_cer']:.4f} "
        f"mean RTF={agg['mean_rtf']:.3f} -> {out}"
    )


def cmd_report(args: argparse.Namespace) -> None:
    arms: dict[str, dict] = {}
    for path in args.inputs:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        arms[payload["arm"]] = payload
    lines = ["# ASR A/B eval report", ""]
    lines.append(f"Dataset: {next(iter(arms.values()))['dataset']}")
    gold = all(p["gold_references"] for p in arms.values())
    lines.append(
        "References: " + ("gold (human-written)" if gold else "captured final text — NOT gold")
    )
    lines += ["", "| arm | micro CER | mean RTF | mean latency/clip | term-miss clips | latin token survival |",
              "|---|---|---|---|---|---|"]
    for arm in ("A", "B", "D"):
        if arm not in arms:
            continue
        agg = arms[arm]["aggregate"]
        survival = (
            f"{agg['latin_survived']}/{agg['latin_ref_tokens']}"
            if agg["latin_ref_tokens"]
            else "n/a"
        )
        lines.append(
            f"| {arm} | {agg['micro_cer']:.4f} | {agg['mean_rtf']:.3f} | "
            f"{agg['mean_elapsed_s']:.2f}s | {agg['term_miss_clips']} | {survival} |"
        )
    if "A" in arms and "B" in arms:
        a, b = arms["A"]["aggregate"], arms["B"]["aggregate"]
        lines.append(
            f"| C (simulated) | {b['micro_cer']:.4f} (= B by construction) | "
            f"{a['mean_rtf'] + b['mean_rtf']:.3f} | "
            f"{a['mean_elapsed_s'] + b['mean_elapsed_s']:.2f}s (A stream + B decode) | "
            f"{b['term_miss_clips']} | see B |"
        )
        lines += [
            "",
            "Arm C note: the two-pass design commits arm B's full-audio decode as",
            "the final text, so C's CER equals B's CER exactly; the only real C",
            "question is latency, reported above as A's streaming latency plus",
            "B's offline decode latency. No separate C run was performed.",
        ]
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--arm", choices=["A", "B", "D"], required=True)
    run.add_argument("--dataset", required=True)
    run.add_argument("--out", required=True)
    run.set_defaults(func=cmd_run)
    report = sub.add_parser("report")
    report.add_argument("--inputs", nargs="+", required=True)
    report.add_argument("--out", required=True)
    report.set_defaults(func=cmd_report)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
