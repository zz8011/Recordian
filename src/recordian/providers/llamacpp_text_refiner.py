from __future__ import annotations

from pathlib import Path


class LlamaCppTextRefiner:
    """基于 llama.cpp 的文本精炼器

    使用 llama-cpp-python 进行本地推理，支持：
    - GGUF 量化模型（Q4_K_M 等）
    - GPU 加速（CUDA）
    - CPU 后备
    - 低显存占用
    """

    def __init__(
        self,
        model_path: str,
        *,
        n_gpu_layers: int = -1,  # -1 表示全部放 GPU
        n_ctx: int = 2048,
        n_threads: int | None = None,
        max_new_tokens: int = 512,
        temperature: float = 0.1,
        prompt_template: str | None = None,
    ) -> None:
        try:
            from llama_cpp import Llama
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "llama-cpp-python 未安装。请执行:\n"
                "CMAKE_ARGS=\"-DLLAMA_CUDA=on\" pip install llama-cpp-python"
            ) from exc

        self.model_path = model_path
        self.max_new_tokens = max_new_tokens
        self.temperature = temperature
        self.prompt_template = prompt_template

        # 初始化 llama.cpp 模型
        self.llm = Llama(
            model_path=model_path,
            n_gpu_layers=n_gpu_layers,
            n_ctx=n_ctx,
            n_threads=n_threads,
            verbose=False,
        )

    @property
    def provider_name(self) -> str:
        model_name = Path(self.model_path).stem
        return f"llamacpp:{model_name}"

    def refine(self, text: str) -> str:
        """精炼文本

        Args:
            text: ASR 原始输出文本

        Returns:
            精炼后的文本
        """
        if not text.strip():
            return ""

        prompt = self._build_prompt(text)

        # 调用 llama.cpp 推理
        result = self.llm(
            prompt,
            max_tokens=self.max_new_tokens,
            temperature=self.temperature,
            stop=["原文：", "\n\n原文", "用户："],  # 停止词
            echo=False,
        )

        # 提取生成的文本
        if result and "choices" in result and len(result["choices"]) > 0:
            generated = result["choices"][0].get("text", "").strip()
            # 移除可能的前缀
            if generated.startswith("整理后："):
                generated = generated[5:].strip()
            return generated

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
