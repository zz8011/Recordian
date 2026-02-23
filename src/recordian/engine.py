from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path

from .config import AppConfig
from .models import ASRResult, CommitResult, SessionContext, SessionState
from .policy import Pass2Policy
from .providers.base import ASRProvider


class DictationEngine:
    """Single-pass first, policy-driven pass2 correction."""

    def __init__(
        self,
        pass1_provider: ASRProvider,
        *,
        pass2_provider: ASRProvider | None = None,
        config: AppConfig | None = None,
    ) -> None:
        self.pass1_provider = pass1_provider
        self.pass2_provider = pass2_provider
        self.config = config or AppConfig()
        self.policy = Pass2Policy(self.config.policy)

    def transcribe_utterance(
        self,
        wav_path: Path,
        *,
        hotwords: list[str] | None = None,
        force_high_precision: bool = False,
    ) -> CommitResult:
        hotwords = hotwords or []
        state = SessionState.LISTENING
        pass1 = self.pass1_provider.transcribe_file(wav_path, hotwords=hotwords)
        state = SessionState.END_DETECTED

        context = SessionContext(
            hotwords=hotwords,
            force_high_precision=force_high_precision,
        )
        decision = self.policy.evaluate(pass1, context)

        pass2_result: ASRResult | None = None
        if decision.run_pass2 and self.pass2_provider is not None:
            state = SessionState.CORRECTING
            timeout_ms = (
                self.config.policy.pass2_timeout_ms_cloud
                if self.pass2_provider.is_cloud
                else self.config.policy.pass2_timeout_ms_local
            )
            pass2_result = self._run_pass2_with_timeout(wav_path, hotwords, timeout_ms)

        final_text = pass2_result.text if pass2_result and pass2_result.text else pass1.text
        state = SessionState.COMMIT
        return CommitResult(
            state=state,
            text=final_text,
            pass1_result=pass1,
            pass2_result=pass2_result,
            decision=decision,
        )

    def _run_pass2_with_timeout(
        self,
        wav_path: Path,
        hotwords: list[str],
        timeout_ms: int,
    ) -> ASRResult | None:
        if self.pass2_provider is None:
            return None

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.pass2_provider.transcribe_file, wav_path, hotwords=hotwords)
            try:
                return future.result(timeout=timeout_ms / 1000)
            except TimeoutError:
                return None
