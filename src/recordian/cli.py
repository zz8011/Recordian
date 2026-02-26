from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import AppConfig
from .engine import DictationEngine
from .linux_commit import CommitError, resolve_committer
from .providers import HttpCloudProvider
from .realtime import RealtimeDictationEngine


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Recordian voice dictation engine")
    parser.add_argument("--mode", choices=["utterance", "realtime-sim"], default="utterance")
    parser.add_argument("--wav", required=True, help="Path to audio file (wav/mp3/ogg/mp4)")
    parser.add_argument("--chunk-ms", type=int, default=480, help="Chunk size in ms for realtime-sim")
    parser.add_argument("--hotword", action="append", default=[], help="Hotword, repeatable")
    parser.add_argument("--force-pass2", action="store_true", help="Always run pass2")
    parser.add_argument(
        "--commit-backend",
        choices=["none", "auto", "wtype", "xdotool", "xdotool-clipboard", "stdout"],
        default="none",
        help="Commit final text to focused app on Linux",
    )

    parser.add_argument(
        "--pass1",
        choices=["http"],
        default="http",
    )
    parser.add_argument("--pass1-model", default="")
    parser.add_argument("--pass1-device", default="cpu")
    parser.add_argument("--pass1-hub", default="ms", choices=["ms", "hf"])
    parser.add_argument("--pass1-remote-code", default="")
    parser.add_argument("--pass1-endpoint", default="")
    parser.add_argument("--pass1-api-key", default="")

    parser.add_argument("--pass2", choices=["none", "http"], default="none")
    parser.add_argument("--pass2-model", default="FunAudioLLM/Fun-ASR-Nano-2512")
    parser.add_argument("--pass2-device", default="cpu")
    parser.add_argument("--pass2-hub", default="ms", choices=["ms", "hf"])
    parser.add_argument("--pass2-remote-code", default="")
    parser.add_argument("--pass2-endpoint", default="")
    parser.add_argument("--pass2-api-key", default="")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    wav_path = Path(args.wav)
    committer = resolve_committer(args.commit_backend)

    pass2 = None
    if args.pass2 == "http":
        if not args.pass2_endpoint:
            parser.error("--pass2=http 时必须提供 --pass2-endpoint")
        pass2 = HttpCloudProvider(args.pass2_endpoint, api_key=args.pass2_api_key or None)

    if args.mode == "utterance":
        if args.pass1 == "http":
            if not args.pass1_endpoint:
                parser.error("--pass1=http 时必须提供 --pass1-endpoint")
            pass1 = HttpCloudProvider(args.pass1_endpoint, api_key=args.pass1_api_key or None)
        else:
            parser.error("--mode=utterance 仅支持 --pass1=http")

        engine = DictationEngine(pass1, pass2_provider=pass2, config=AppConfig())
        result = engine.transcribe_utterance(
            wav_path,
            hotwords=args.hotword,
            force_high_precision=args.force_pass2,
        )
        output = {
            "mode": args.mode,
            "state": result.state,
            "text": result.text,
            "decision": {
                "run_pass2": result.decision.run_pass2,
                "reasons": result.decision.reasons,
            },
            "pass1": {
                "model": result.pass1_result.model_name,
                "confidence": result.pass1_result.confidence,
                "text": result.pass1_result.text,
            },
            "pass2": None if result.pass2_result is None else {
                "model": result.pass2_result.model_name,
                "confidence": result.pass2_result.confidence,
                "text": result.pass2_result.text,
            },
        }
        final_text = result.text
    else:
        parser.error("--mode=realtime-sim 已移除（需要 FunASR streaming）")

    commit_info = {
        "backend": committer.backend_name,
        "committed": False,
        "detail": "disabled",
    }
    if final_text.strip():
        try:
            commit_result = committer.commit(final_text)
            commit_info = {
                "backend": commit_result.backend,
                "committed": commit_result.committed,
                "detail": commit_result.detail,
            }
        except CommitError as exc:
            commit_info = {
                "backend": committer.backend_name,
                "committed": False,
                "detail": str(exc),
            }
    else:
        commit_info = {
            "backend": committer.backend_name,
            "committed": False,
            "detail": "empty_text",
        }
    output["commit"] = commit_info

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
