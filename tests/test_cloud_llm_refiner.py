"""测试 CloudLLMRefiner 云端精炼器"""
from __future__ import annotations


class TestCloudLLMRefinerInit:
    """测试 CloudLLMRefiner 初始化"""

    def test_init_with_defaults(self) -> None:
        """测试默认参数初始化"""
        from recordian.providers.cloud_llm_refiner import CloudLLMRefiner

        refiner = CloudLLMRefiner(
            api_base="https://api.example.com",
            api_key="test-key",
        )
        assert refiner.api_base == "https://api.example.com"
        assert refiner.api_key == "test-key"
        assert refiner.model == "claude-3-5-sonnet-20241022"
        assert refiner.max_tokens == 512
        assert refiner.temperature == 0.1
        assert refiner.timeout == 30

    def test_init_with_custom_timeout(self) -> None:
        """测试自定义超时"""
        from recordian.providers.cloud_llm_refiner import CloudLLMRefiner

        refiner = CloudLLMRefiner(
            api_base="https://api.example.com",
            api_key="test-key",
            timeout=60,
        )
        assert refiner.timeout == 60

    def test_api_format_auto_detection_ollama(self) -> None:
        """测试自动检测 Ollama API 格式"""
        from recordian.providers.cloud_llm_refiner import CloudLLMRefiner

        refiner = CloudLLMRefiner(
            api_base="http://localhost:11434",
            api_key="test-key",
        )
        assert refiner.api_format == "ollama"

    def test_api_format_auto_detection_openai(self) -> None:
        """测试自动检测 OpenAI API 格式"""
        from recordian.providers.cloud_llm_refiner import CloudLLMRefiner

        refiner = CloudLLMRefiner(
            api_base="https://api.groq.com",
            api_key="test-key",
        )
        assert refiner.api_format == "openai"

    def test_api_format_auto_detection_openai_for_local_v1_gateway(self) -> None:
        """测试自动识别局域网 OpenAI 兼容 /v1 网关"""
        from recordian.providers.cloud_llm_refiner import CloudLLMRefiner

        refiner = CloudLLMRefiner(
            api_base="http://192.168.5.111:8018/v1",
            api_key="test-key",
        )
        assert refiner.api_format == "openai"

    def test_api_format_auto_detection_anthropic(self) -> None:
        """测试自动检测 Anthropic API 格式"""
        from recordian.providers.cloud_llm_refiner import CloudLLMRefiner

        refiner = CloudLLMRefiner(
            api_base="https://api.anthropic.com",
            api_key="test-key",
        )
        assert refiner.api_format == "anthropic"

    def test_api_format_manual_override(self) -> None:
        """测试手动指定 API 格式"""
        from recordian.providers.cloud_llm_refiner import CloudLLMRefiner

        refiner = CloudLLMRefiner(
            api_base="https://custom.api.com",
            api_key="test-key",
            api_format="openai",
        )
        assert refiner.api_format == "openai"

    def test_empty_text_handling(self) -> None:
        """测试空文本处理"""
        from recordian.providers.cloud_llm_refiner import CloudLLMRefiner

        refiner = CloudLLMRefiner(
            api_base="https://api.anthropic.com",
            api_key="test-key",
        )

        result = refiner.refine("")
        assert result == ""

        result = refiner.refine("   ")
        assert result == ""

    def test_openai_payload_disables_thinking_by_default(self, monkeypatch) -> None:
        """测试 OpenAI 兼容接口默认显式关闭 thinking"""
        from recordian.providers.cloud_llm_refiner import CloudLLMRefiner

        captured: dict[str, object] = {}

        class _Response:
            status_code = 200

            def json(self) -> dict[str, object]:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": "整理完成",
                            }
                        }
                    ]
                }

        def _fake_post(url: str, *, headers: dict[str, str], json: dict[str, object], timeout: int) -> _Response:  # noqa: ANN001
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            captured["timeout"] = timeout
            return _Response()

        import requests

        monkeypatch.setattr(requests, "post", _fake_post)

        refiner = CloudLLMRefiner(
            api_base="http://192.168.5.111/v1",
            api_key="test-key",
            model="demo-model",
            api_format="openai",
            enable_thinking=False,
        )

        result = refiner.refine("测试一下")

        assert result == "整理完成"
        assert captured["url"] == "http://192.168.5.111/v1/chat/completions"
        assert captured["json"]["chat_template_kwargs"] == {"enable_thinking": False}

    def test_openai_payload_can_enable_thinking_when_requested(self, monkeypatch) -> None:
        """测试显式开启 thinking 时会透传到 OpenAI 兼容接口"""
        from recordian.providers.cloud_llm_refiner import CloudLLMRefiner

        captured: dict[str, object] = {}

        class _Response:
            status_code = 200

            def json(self) -> dict[str, object]:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": "整理完成",
                            }
                        }
                    ]
                }

        def _fake_post(url: str, *, headers: dict[str, str], json: dict[str, object], timeout: int) -> _Response:  # noqa: ANN001
            captured["json"] = json
            return _Response()

        import requests

        monkeypatch.setattr(requests, "post", _fake_post)

        refiner = CloudLLMRefiner(
            api_base="http://192.168.5.111/v1",
            api_key="test-key",
            model="demo-model",
            api_format="openai",
            enable_thinking=True,
        )

        result = refiner.refine("测试一下")

        assert result == "整理完成"
        assert captured["json"]["chat_template_kwargs"] == {"enable_thinking": True}
