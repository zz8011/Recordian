from __future__ import annotations

import json
from urllib.parse import urlparse

from .base_text_refiner import BaseTextRefiner

#: GPUStack 在模型实例未运行时返回的 404 文案片段。
MODEL_NOT_RUNNING_HINTS = ("no running instances", "model not found")


def _describe_refine_failure(*, api_base: str, model: str, status: int, body: str) -> str:
    """把精炼失败翻译成一句可读中文（overlay 只显示前 72 字符，重点放前面）。"""
    lowered = str(body or "").lower()
    if status == 404 and any(hint in lowered for hint in MODEL_NOT_RUNNING_HINTS):
        return (
            f"文字精炼模型「{model}」没有运行实例（HTTP 404）；"
            f"请在 GPUStack 启动该模型（{api_base}），本次按 ASR 原文上屏"
        )
    if status in {502, 503, 504}:
        return f"文字精炼后端暂时不可用（HTTP {status}），模型实例可能正在重启（{api_base}）"
    if status in {401, 403}:
        return f"文字精炼鉴权失败（HTTP {status}）；请检查 refine_api_key（{api_base}）"
    preview = str(body or "").strip().replace("\n", " ")[:120]
    return f"文字精炼调用失败（HTTP {status}）: {preview}（{api_base}）"


def _describe_refine_transport_failure(*, api_base: str, model: str, timeout_s: float, exc: BaseException) -> str:
    """连接层失败（超时/连不上）的中文说明。"""
    name = type(exc).__name__
    if name in {"Timeout", "ReadTimeout", "ConnectTimeout"}:
        return f"文字精炼请求超时（>{timeout_s:.0f}s），后端可能卡住或网络不通（{api_base}）"
    if name in {"ConnectionError", "NewConnectionError", "RemoteDisconnected"}:
        return f"无法连接文字精炼后端 {api_base}（模型 {model}）；请检查主机是否在线、端口是否可达"
    return f"文字精炼请求失败：{name}: {exc}（{api_base}）"


def _detect_api_format(api_base: str) -> str:
    normalized = api_base.rstrip("/")
    lowered = normalized.lower()

    if ":11434" in lowered:
        return "ollama"

    parsed = urlparse(normalized)
    path = parsed.path.rstrip("/")
    if (
        "groq.com" in lowered
        or "openai.com" in lowered
        or "deepseek.com" in lowered
        or path == "/v1"
        or path.endswith("/v1")
    ):
        return "openai"

    return "anthropic"


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
        timeout: float = 30.0,  # API 超时时间（秒），默认30秒
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
            self.api_format = _detect_api_format(api_base)
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

    def _sanitize_output(self, text: str) -> str:
        cleaned = self._remove_think_tags(text)
        # Some models may echo control tokens from prompts.
        return cleaned.replace("/no_think", "").strip()

    def refine_stream(self, text: str):
        if not text.strip():
            return

        if self.api_format == "ollama":
            yield from self._refine_stream_ollama(text)
            return
        if self.api_format == "openai":
            yield from self._refine_stream_openai(text)
            return

        output = self._refine_anthropic(text)
        if output:
            yield output

    # ------------------------------------------------------------------
    # Shared helpers — eliminate duplication across provider methods
    # ------------------------------------------------------------------

    def _ensure_requests(self):
        """Lazily import *requests*, raising a user-friendly error if missing."""
        try:
            import requests
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "requests 未安装。请执行: pip install requests"
            ) from exc
        return requests

    def _raise_on_error(self, response) -> None:
        """Raise RuntimeError when the API response is non-200."""
        if response.status_code != 200:
            try:
                body = str(response.text or "")
            except Exception:  # noqa: BLE001
                body = ""
            raise RuntimeError(
                _describe_refine_failure(
                    api_base=self.api_base,
                    model=self.model,
                    status=int(response.status_code),
                    body=body,
                )
            )

    def _post_json(self, url: str, headers: dict, payload: dict):
        """POST 并在连接层失败时给出可读中文报错。"""
        requests = self._ensure_requests()
        try:
            return requests.post(url, headers=headers, json=payload, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                _describe_refine_transport_failure(
                    api_base=self.api_base,
                    model=self.model,
                    timeout_s=self.timeout,
                    exc=exc,
                )
            ) from exc

    # --- Anthropic --------------------------------------------------------

    def _build_anthropic_headers(self) -> dict:
        """Anthropic-compatible API 请求头"""
        return {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

    # --- Prompt -----------------------------------------------------------

    _DEFAULT_SYSTEM_PROMPT = (
        "整理以下语音识别文本：\n"
        "- 去除重复词语和句子\n"
        "- 去除语气助词（嗯、啊、呃、那个、这个、然后等）\n"
        "- 添加正确标点符号\n"
        "- 保持原意，通顺易读\n"
        "- 直接输出结果，不要思考过程"
    )

    def _build_messages(self, text: str) -> list[dict[str, str]]:
        """构建 system/user 分离的消息列表（防止 prompt 注入）。

        将指令放入 system 角色确保模型优先遵循，用户文本放入 user
        角色，避免恶意文本篡改指令。
        """
        if self.prompt_template:
            # 使用 .replace() 代替 .format() 防止格式字符串攻击
            user_content = self.prompt_template.replace("{text}", text)
            return [{"role": "user", "content": user_content}]

        return [
            {"role": "system", "content": self._DEFAULT_SYSTEM_PROMPT},
            {"role": "user", "content": f"原文：{text}\n\n整理后："},
        ]

    def _build_anthropic_payload(self, messages: list[dict[str, str]], *, source_text: str = "") -> dict:
        """Anthropic-compatible API 请求体"""
        return {
            "model": self.model,
            "max_tokens": self._max_output_tokens_for_text(source_text),
            "temperature": self.temperature,
            "messages": messages,
        }

    @staticmethod
    def _parse_anthropic_response(result: dict) -> str:
        """从 Anthropic API 响应中提取文本"""
        content = result.get("content", [])

        # 查找 type="text" 的内容
        if content and isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text", "")
                    return text.strip() if isinstance(text, str) else ""
        return ""

    def _refine_anthropic(self, text: str) -> str:
        """使用 Anthropic API 格式"""
        messages = self._build_messages(text)
        requests = self._ensure_requests()

        # 调用 Anthropic-compatible API
        headers = self._build_anthropic_headers()
        payload = self._build_anthropic_payload(messages, source_text=text)

        response = requests.post(
            f"{self.api_base}/v1/messages",
            headers=headers,
            json=payload,
            timeout=self.timeout,
        )
        self._raise_on_error(response)

        output = self._parse_anthropic_response(response.json())
        return self._sanitize_output(output)

    # --- OpenAI -----------------------------------------------------------

    def _build_openai_headers(self) -> dict:
        """OpenAI-compatible API 请求头（Groq, DeepSeek 等）"""
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    def _build_openai_payload(
        self, messages: list[dict[str, str]], *, stream: bool = False, source_text: str = ""
    ) -> dict:
        """OpenAI-compatible API 请求体"""
        payload: dict = {
            "model": self.model,
            "max_tokens": self._max_output_tokens_for_text(source_text),
            "temperature": self.temperature,
            "messages": messages,
            # vLLM/GPUStack serving Qwen3.5 defaults to reasoning mode unless
            # chat_template_kwargs.enable_thinking is explicitly disabled.
            "chat_template_kwargs": {
                "enable_thinking": bool(self.enable_thinking),
            },
        }
        if stream:
            payload["stream"] = True
        return payload

    @staticmethod
    def _parse_openai_response(result: dict) -> str:
        """从 OpenAI API 响应中提取文本"""
        choices = result.get("choices", [])
        if choices and isinstance(choices, list):
            choice = choices[0]
            if isinstance(choice, dict):
                message = choice.get("message", {})
                if isinstance(message, dict):
                    content = message.get("content", "")
                    return content.strip() if isinstance(content, str) else ""
        return ""

    def _refine_openai(self, text: str) -> str:
        """使用 OpenAI API 格式（Groq, DeepSeek 等）"""
        messages = self._build_messages(text)

        # 调用 OpenAI-compatible API
        headers = self._build_openai_headers()
        payload = self._build_openai_payload(messages, source_text=text)

        response = self._post_json(f"{self.api_base}/chat/completions", headers, payload)
        self._raise_on_error(response)

        output = self._parse_openai_response(response.json())
        return self._sanitize_output(output)

    def _refine_stream_openai(self, text: str):
        messages = self._build_messages(text)
        requests = self._ensure_requests()

        headers = self._build_openai_headers()
        payload = self._build_openai_payload(messages, stream=True, source_text=text)

        response = requests.post(
            f"{self.api_base}/chat/completions",
            headers=headers,
            json=payload,
            timeout=self.timeout,
            stream=True,
        )
        self._raise_on_error(response)

        for raw_line in response.iter_lines(decode_unicode=False):
            if not raw_line:
                continue
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            event = json.loads(payload)
            choices = event.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            choice = choices[0]
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta", {})
            if not isinstance(delta, dict):
                continue
            content = delta.get("content")
            if isinstance(content, str) and content:
                yield content
            if choice.get("finish_reason") is not None:
                break

    # --- Ollama -----------------------------------------------------------

    def _build_ollama_headers(self) -> dict:
        """Ollama 原生 API 请求头"""
        return {
            "Content-Type": "application/json",
        }

    def _build_ollama_payload(
        self, messages: list[dict[str, str]], *, stream: bool = False, source_text: str = ""
    ) -> dict:
        """Ollama 原生 API 请求体"""
        return {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            # Qwen3.5 reasoning models on Ollama may put output in `thinking`
            # unless `think` is explicitly disabled.
            "think": bool(self.enable_thinking),
            "options": {
                "num_predict": self._max_output_tokens_for_text(source_text),
                "temperature": self.temperature,
            },
        }

    @staticmethod
    def _parse_ollama_response(result: dict) -> str:
        """从 Ollama API 响应中提取文本"""
        message = result.get("message", {})
        if isinstance(message, dict):
            content = message.get("content", "")
            return content.strip() if isinstance(content, str) else ""
        return ""

    def _refine_ollama(self, text: str) -> str:
        """使用 Ollama 原生 API 格式"""
        messages = self._build_messages(text)
        requests = self._ensure_requests()

        # 调用 Ollama 原生 API
        headers = self._build_ollama_headers()
        payload = self._build_ollama_payload(messages, source_text=text)

        response = requests.post(
            f"{self.api_base}/api/chat",
            headers=headers,
            json=payload,
            timeout=self.timeout,
        )
        self._raise_on_error(response)

        output = self._parse_ollama_response(response.json())
        return self._sanitize_output(output)

    def _refine_stream_ollama(self, text: str):
        messages = self._build_messages(text)
        requests = self._ensure_requests()

        headers = self._build_ollama_headers()
        payload = self._build_ollama_payload(messages, stream=True, source_text=text)

        response = requests.post(
            f"{self.api_base}/api/chat",
            headers=headers,
            json=payload,
            timeout=self.timeout,
            stream=True,
        )
        self._raise_on_error(response)

        for raw_line in response.iter_lines(decode_unicode=False):
            if not raw_line:
                continue
            event = json.loads(raw_line.decode("utf-8", errors="replace"))
            message = event.get("message", {})
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str) and content:
                yield content
            if bool(event.get("done")):
                break

    # --- Legacy -----------------------------------------------------------

    # _build_prompt 已移除，由 _build_messages 替代（system/user 角色分离）
