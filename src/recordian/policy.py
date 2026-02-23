from __future__ import annotations

import re

from .config import Pass2PolicyConfig
from .models import ASRResult, Decision, SessionContext


class Pass2Policy:
    """Decide whether pass2 correction is needed."""

    _risk_patterns = [
        re.compile(r"\b\d{2,}\b"),
        re.compile(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b"),
        re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"),
        re.compile(r"https?://\S+"),
    ]

    def __init__(self, config: Pass2PolicyConfig) -> None:
        self.config = config

    def evaluate(self, result: ASRResult, context: SessionContext) -> Decision:
        reasons: list[str] = []

        if context.force_high_precision:
            reasons.append("forced_high_precision")

        if result.confidence is not None and result.confidence < self.config.confidence_threshold:
            reasons.append("low_confidence")

        if result.english_ratio > self.config.english_ratio_threshold:
            reasons.append("high_english_ratio")

        if self._contains_risk_pattern(result.text):
            reasons.append("high_risk_text")

        if self._hotword_missing(result.text, context.hotwords):
            reasons.append("hotword_missing")

        return Decision(run_pass2=len(reasons) > 0, reasons=reasons)

    def _contains_risk_pattern(self, text: str) -> bool:
        for pattern in self._risk_patterns:
            if pattern.search(text):
                return True
        return False

    @staticmethod
    def _hotword_missing(text: str, hotwords: list[str]) -> bool:
        if not hotwords:
            return False

        normalized = text.lower()
        for hotword in hotwords:
            hw = hotword.strip().lower()
            if hw and hw not in normalized:
                return True
        return False
