from __future__ import annotations


class ASRContextComposer:
    """Compose provider-facing ASR context with stable dedupe and size limits."""

    def __init__(self, base_context: str = "", *, max_hotwords: int = 40) -> None:
        self.base_context = (base_context or "").strip()
        self.max_hotwords = max(0, int(max_hotwords))

    def normalize_hotwords(self, hotwords: list[str]) -> list[str]:
        if self.max_hotwords == 0:
            return []

        deduped: list[str] = []
        seen: set[str] = set()
        for raw in hotwords:
            token = str(raw).strip()
            if not token or token in seen:
                continue
            seen.add(token)
            deduped.append(token)
            if len(deduped) >= self.max_hotwords:
                break
        return deduped

    def compose_text(
        self,
        hotwords: list[str],
        *,
        hotword_prefix: str = "热词参考: ",
        separator: str = "、",
    ) -> str:
        normalized_hotwords = self.normalize_hotwords(hotwords)
        if not normalized_hotwords:
            return self.base_context

        hotword_hint = hotword_prefix + separator.join(normalized_hotwords)
        if not self.base_context:
            return hotword_hint
        return f"{self.base_context}\n{hotword_hint}"
