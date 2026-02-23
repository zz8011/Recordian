from __future__ import annotations

from pathlib import Path


class CloudLLMRefiner:
    """云端 LLM 文本精炼器：通过 API 调用外部 LLM 服务"""

    def __init__(
        self,
        api_base: str,
        api_key: str,
        model: str = "claude-3-5-sonnet-20241022",
        *,
        max_tokens: int = 512,
        temperature: float = 0.1,
        prompt_template: str | None = None,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.prompt_template = prompt_template

    @property
    def provider_name(self) -> str:
        return f"cloud-llm:{self.model}"

    def refine(self, text: str) -> str:
        """精炼文本：调用云端 API

        Args:
            text: ASR 原始输出文本

        Returns:
            精炼后的文本
        """
        if not text.strip():
            return ""

        prompt = self._build_prompt(text)

        try:
            import requests
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "requests 未安装。请执行: pip install requests"
            ) from exc

        # 调用 Anthropic-compatible API
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
        }

        response = requests.post(
            f"{self.api_base}/v1/messages",
            headers=headers,
            json=payload,
            timeout=30,
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"API 调用失败: {response.status_code} {response.text}"
            )

        result = response.json()
        content = result.get("content", [])

        # 查找 type="text" 的内容
        if content and isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    return item.get("text", "").strip()

        return ""

    def _build_prompt(self, text: str) -> str:
        """构建文本精炼 prompt"""
        if self.prompt_template:
            return self.prompt_template.format(text=text)

        # 默认 prompt
        return f"""整理以下语音识别文本：
- 去除重复词语和句子
- 去除语气助词（嗯、啊、呃、那个、这个、然后等）
- 添加正确标点符号
- 保持原意，通顺易读
- 直接输出结果，不要思考过程

原文：{text}

整理后："""
