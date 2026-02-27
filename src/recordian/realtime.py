from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from tempfile import TemporaryDirectory

from .audio import chunk_samples, read_wav_mono_f32, write_wav_mono_f32
from .config import AppConfig
from .models import ASRResult, CommitResult, RealtimeRunResult, SessionContext, SessionState, StreamUpdate
from .policy import Pass2Policy
from .providers.base import ASRProvider
from .providers.streaming_base import StreamingASRProvider


class RealtimeDictationEngine:
    """Realtime pass1 with policy-driven optional pass2 correction."""

    def __init__(
        self,
        pass1_provider: StreamingASRProvider,
        *,
        pass2_provider: ASRProvider | None = None,
        config: AppConfig | None = None,
        sample_rate: int = 16000,
    ) -> None:
        self.pass1_provider = pass1_provider
        self.pass2_provider = pass2_provider
        self.config = config or AppConfig()
        self.policy = Pass2Policy(self.config.policy)
        self.sample_rate = sample_rate

    def transcribe_wav(
        self,
        wav_path: Path,
        *,
        chunk_ms: int = 480,
        hotwords: list[str] | None = None,
        force_high_precision: bool = False,
    ) -> RealtimeRunResult:
        samples = read_wav_mono_f32(wav_path, sample_rate=self.sample_rate)
        chunks = chunk_samples(samples, sample_rate=self.sample_rate, chunk_ms=chunk_ms)
        return self.transcribe_chunks(
            chunks,
            hotwords=hotwords,
            force_high_precision=force_high_precision,
        )

    def transcribe_chunks(
        self,
        chunks: list[list[float]],
        *,
        hotwords: list[str] | None = None,
        force_high_precision: bool = False,
    ) -> RealtimeRunResult:
        hotwords = hotwords or []
        updates: list[StreamUpdate] = []
        buffered: list[float] = []

        self.pass1_provider.start_session(hotwords=hotwords)
        for idx, chunk in enumerate(chunks):
            if not chunk:
                continue
            buffered.extend(chunk)
            is_final = idx == len(chunks) - 1
            update = self.pass1_provider.push_chunk(chunk, is_final=is_final, chunk_index=idx)
            if update is not None:
                updates.append(update)

        pass1 = self.pass1_provider.end_session()
        context = SessionContext(
            hotwords=hotwords,
            force_high_precision=force_high_precision,
        )
        decision = self.policy.evaluate(pass1, context)

        pass2: ASRResult | None = None
        if decision.run_pass2 and self.pass2_provider is not None and buffered:
            timeout_ms = (
                self.config.policy.pass2_timeout_ms_cloud
                if self.pass2_provider.is_cloud
                else self.config.policy.pass2_timeout_ms_local
            )
            pass2 = self._run_pass2_with_timeout(buffered, hotwords, timeout_ms)

        final_text = pass2.text if pass2 and pass2.text else pass1.text
        commit = CommitResult(
            state=SessionState.COMMIT,
            text=final_text,
            pass1_result=pass1,
            pass2_result=pass2,
            decision=decision,
        )
        return RealtimeRunResult(updates=updates, commit=commit)

    def _run_pass2_with_timeout(
        self,
        samples: list[float],
        hotwords: list[str],
        timeout_ms: int,
    ) -> ASRResult | None:
        if self.pass2_provider is None:
            return None

        with TemporaryDirectory(prefix="recordian-pass2-") as temp_dir:
            wav_path = Path(temp_dir) / "pass2.wav"
            write_wav_mono_f32(wav_path, samples, sample_rate=self.sample_rate)
            executor = ThreadPoolExecutor(max_workers=1)
            try:
                future = executor.submit(self.pass2_provider.transcribe_file, wav_path, hotwords=hotwords)
                try:
                    return future.result(timeout=timeout_ms / 1000.0)
                except TimeoutError:
                    future.cancel()
                    return None
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
