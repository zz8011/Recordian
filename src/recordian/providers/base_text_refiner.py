from __future__ import annotations

import re
from abc import ABC, abstractmethod


class BaseTextRefiner(ABC):
    """文本精炼器抽象基类，提供公共功能"""

    def __init__(
        self,
        *,
        max_tokens: int = 512,
        temperature: float = 0.1,
        prompt_template: str | None = None,
        enable_thinking: bool = False,
    ) -> None:
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.prompt_template = prompt_template
        self.enable_thinking = enable_thinking

    @property
    @abstractmethod
    def provider_name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def refine(self, text: str) -> str:
        raise NotImplementedError

    def update_preset(self, preset_name: str) -> None:
        """动态更新 preset（热切换）"""
        from recordian.preset_manager import PresetManager

        preset_mgr = PresetManager()
        try:
            self.prompt_template = preset_mgr.load_preset(preset_name)
        except Exception:
            pass

    def _max_output_tokens_for_text(self, text: str) -> int:
        """Return a token budget large enough for long-form cleanup.

        Refinement normally preserves most of the dictated text. A fixed
        512-token cap can silently keep only the beginning of a long note, so
        expand the budget from the input length while keeping explicit higher
        user limits intact.
        """
        stripped = text.strip()
        if not stripped:
            return max(1, int(self.max_tokens))

        cjk_chars = sum(1 for ch in stripped if "\u4e00" <= ch <= "\u9fff")
        non_cjk_chars = max(0, len(stripped) - cjk_chars)
        estimated_input_tokens = cjk_chars + (non_cjk_chars + 3) // 4
        dynamic_budget = int(estimated_input_tokens * 1.25) + 128
        return max(1, int(self.max_tokens), dynamic_budget)

    def _remove_think_tags(self, text: str) -> str:
        """移除文本中的 <think> 标签及其内容"""
        if not text:
            return ""

        result = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        result = result.strip()

        if "<think>" in result:
            parts = result.split("</think>")
            if len(parts) > 1:
                result = parts[-1].strip()
            else:
                result = result.split("<think>")[0].strip()

        result = result.replace("<think>", "").replace("</think>", "").strip()
        return result
