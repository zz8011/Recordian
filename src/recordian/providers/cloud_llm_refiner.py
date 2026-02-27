from __future__ import annotations

from .base_text_refiner import BaseTextRefiner


class CloudLLMRefiner(BaseTextRefiner):
    """云端 LLM 文本精炼器：通过 API 调用外部 LLM 服务

    支持两种 API 格式：
    - Anthropic API (MiniMax 等)
    - OpenAI API (Groq, DeepSeek 等)
    """

    def __init__(
        self,
        api_base: str,
        api_key: str,
        model: str = "claude-3-5-sonnet-20241022",
        *,
        max_tokens: int = 512,
        temperature: float = 0.1,
        prompt_template: str | None = None,
        api_format: str = "auto",  # "auto", "anthropic", "openai"
        enable_thinking: bool = False,
        timeout: int = 30,  # API 超时时间（秒），默认30秒
    ) -> None:
        super().__init__(
            max_tokens=max_tokens,
            temperature=temperature,
            prompt_template=prompt_template,
            enable_thinking=enable_thinking,
        )
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

        # 自动检测 API 格式
        if api_format == "auto":
            if ":11434" in api_base:
                # Ollama 原生 API
                self.api_format = "ollama"
            elif "groq.com" in api_base.lower() or "openai.com" in api_base.lower() or "deepseek.com" in api_base.lower():
                self.api_format = "openai"
            else:
                self.api_format = "anthropic"
        else:
            self.api_format = api_format

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

        if self.api_format == "ollama":
            return self._refine_ollama(text)
        elif self.api_format == "openai":
            return self._refine_openai(text)
        else:
            return self._refine_anthropic(text)

    def _refine_anthropic(self, text: str) -> str:
        """使用 Anthropic API 格式"""
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
            timeout=self.timeout,
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"API 调用失败: {response.status_code} {response.text}"
            )

        result = response.json()
        content = result.get("content", [])

        # 查找 type="text" 的内容
        output = ""
        if content and isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    output = item.get("text", "").strip()
                    break

        # 移除 <think> 标签
        return self._remove_think_tags(output)

    def _refine_openai(self, text: str) -> str:
        """使用 OpenAI API 格式（Groq, DeepSeek 等）"""
        prompt = self._build_prompt(text)

        try:
            import requests
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "requests 未安装。请执行: pip install requests"
            ) from exc

        # 调用 OpenAI-compatible API
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
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
            f"{self.api_base}/chat/completions",
            headers=headers,
            json=payload,
            timeout=self.timeout,
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"API 调用失败: {response.status_code} {response.text}"
            )

        result = response.json()
        choices = result.get("choices", [])

        output = ""
        if choices and len(choices) > 0:
            message = choices[0].get("message", {})
            output = message.get("content", "").strip()

        # 移除 <think> 标签
        return self._remove_think_tags(output)

    def _refine_ollama(self, text: str) -> str:
        """使用 Ollama 原生 API 格式"""
        prompt = self._build_prompt(text)

        # 根据 enable_thinking 参数决定是否添加 /no_think 标记
        if not self.enable_thinking and "/no_think" not in prompt:
            prompt = prompt + " /no_think"

        try:
            import requests
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "requests 未安装。请执行: pip install requests"
            ) from exc

        # 调用 Ollama 原生 API
        headers = {
            "Content-Type": "application/json",
        }

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            "stream": False,
            "options": {
                "num_predict": self.max_tokens,
                "temperature": self.temperature,
            }
        }

        response = requests.post(
            f"{self.api_base}/api/chat",
            headers=headers,
            json=payload,
            timeout=self.timeout,
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"API 调用失败: {response.status_code} {response.text}"
            )

        result = response.json()
        message = result.get("message", {})
        output = message.get("content", "").strip()

        # 移除 <think> 标签
        return self._remove_think_tags(output)

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
