from __future__ import annotations

import re
from functools import lru_cache

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
        # 缓存 hotwords 的小写版本，避免重复转换
        self._normalized_hotwords_cache: dict[tuple[str, ...], list[str]] = {}

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

    @staticmethod
    @lru_cache(maxsize=128)
    def _contains_risk_pattern(text: str) -> bool:
        """检查文本是否包含风险模式（带缓存）"""
        for pattern in Pass2Policy._risk_patterns:
            if pattern.search(text):
                return True
        return False

    def _hotword_missing(self, text: str, hotwords: list[str]) -> bool:
        """检查是否缺少必需的热词"""
        if not hotwords:
            return False

        # 使用缓存的标准化 hotwords
        hotwords_tuple = tuple(hotwords)
        if hotwords_tuple not in self._normalized_hotwords_cache:
            self._normalized_hotwords_cache[hotwords_tuple] = [
                hw.strip().lower() for hw in hotwords if hw.strip()
            ]

        normalized_hotwords = self._normalized_hotwords_cache[hotwords_tuple]
        if not normalized_hotwords:
            return False

        normalized_text = text.lower()
        for hw in normalized_hotwords:
            if hw not in normalized_text:
                return True
        return False
