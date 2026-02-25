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
