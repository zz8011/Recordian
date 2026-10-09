from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import Future
from typing import Any

from .base_text_refiner import BaseTextRefiner
from .cloud_llm_refiner import _filter_thinking, _IncompleteRefinementError


class Qwen3TextRefiner(BaseTextRefiner):
    """Qwen3 文本精炼器：去重、去语气词、标点修复、总结。

    使用 transformers 后端加载 Qwen3 Instruct 模型。
    默认使用 0.6B 模型以获得更快的响应速度（质量与 1.7B 相同）。
    默认禁用 thinking 模式。
    """

    def __init__(
        self,
        model_name: str = "Qwen/Qwen3-0.6B",
        *,
        device: str = "cuda:0",
        dtype: str = "bfloat16",
        max_new_tokens: int = 256,  # 优化：从 512 降低到 256，减少生成开销
        temperature: float = 0.1,
        prompt_template: str | None = None,
        enable_thinking: bool = False,
    ) -> None:
        super().__init__(
            max_tokens=max_new_tokens,
            temperature=temperature,
            prompt_template=prompt_template,
            enable_thinking=enable_thinking,
        )
        self.model_name = model_name
        self.device = device
        self.dtype = dtype
        self.max_new_tokens = max_new_tokens
        self._model: Any | None = None
        self._tokenizer: Any | None = None

    def _eos_token_id(self) -> int | list[int] | None:
        # Preserve the model's extra termination tokens rather than overriding
        # them with the tokenizer's single EOS. Use the same IDs for validation.
        config = getattr(self._model, "generation_config", None)
        eos = getattr(config, "eos_token_id", None)
        if eos is None:
            eos = self._tokenizer.eos_token_id
        return eos

    @staticmethod
    def _completed_tokens(result, input_ids, *, max_new_tokens, eos_token_id, pad_token_id):
        """Accept tensor and return_dict_in_generate outputs, excluding the prompt."""
        sequences = result.get("sequences") if isinstance(result, Mapping) else getattr(result, "sequences", result)
        sequences = sequences.tolist() if hasattr(sequences, "tolist") else sequences
        inputs = input_ids.tolist() if hasattr(input_ids, "tolist") else input_ids
        if not isinstance(sequences, (list, tuple)) or len(sequences) != len(inputs):
            raise _IncompleteRefinementError("Qwen 生成结果缺少有效 token 序列；本次应保留 ASR 原文")
        eos_ids = set(eos_token_id if isinstance(eos_token_id, (list, tuple)) else [eos_token_id]) - {None}
        generated = []
        for prompt, sequence in zip(inputs, sequences, strict=True):
            if not isinstance(sequence, (list, tuple)) or list(sequence[:len(prompt)]) != list(prompt):
                raise _IncompleteRefinementError("Qwen 生成结果缺少输入序列；本次应保留 ASR 原文")
            tokens = list(sequence[len(prompt):])
            terminal = next((i for i, token in enumerate(tokens) if token in eos_ids), None)
            if terminal is not None:
                complete = all(token == pad_token_id for token in tokens[terminal + 1:])
            else:
                # With configured EOS, a streamer stop alone proves nothing.
                # Without EOS, exhausting the generation budget is truncation.
                complete = not eos_ids and 0 < len(tokens) < max_new_tokens
            if not complete or len(tokens) > max_new_tokens:
                raise _IncompleteRefinementError("Qwen 生成结果未正常完成；本次应保留 ASR 原文")
            generated.append(tokens)
        return generated

    @property
    def provider_name(self) -> str:
        return f"qwen3-refiner:{self.model_name}"

    def _lazy_load(self) -> None:
        if self._model is not None:
            return
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "transformers 未安装。请执行: pip install transformers torch"
            ) from exc

        dtype_map = {
            "bfloat16": torch.bfloat16,
            "float16": torch.float16,
            "float32": torch.float32,
        }
        torch_dtype = dtype_map.get(self.dtype, torch.bfloat16)

        self._tokenizer = AutoTokenizer.from_pretrained(
            self.model_name,
            trust_remote_code=True,
        )
        self._model = AutoModelForCausalLM.from_pretrained(
            self.model_name,
            torch_dtype=torch_dtype,
            device_map=self.device,
            trust_remote_code=True,
        )
        # Set model to evaluation mode
        self._model.eval()

        # 优化：启用 torch.compile() 加速推理（PyTorch 2.0+）
        try:
            import torch
            if hasattr(torch, 'compile'):
                self._model = torch.compile(
                    self._model,
                    mode="reduce-overhead",  # 优化模式
                    fullgraph=True,
                )
        except Exception:
            # torch.compile 失败时静默回退，不影响功能
            pass

    def refine(self, text: str) -> str:
        """精炼文本：去重、去语气词、标点修复。

        Args:
            text: ASR 原始输出文本

        Returns:
            精炼后的文本
        """
        if not text.strip():
            return ""

        self._lazy_load()

        messages = self._build_messages(text)
        tokenizer = self._tokenizer
        model = self._model
        if tokenizer is None or model is None:
            raise RuntimeError("Qwen3TextRefiner model is not loaded")

        # 根据 enable_thinking 参数控制是否启用思考模式
        chat_template_kwargs = {
            "tokenize": False,
            "add_generation_prompt": True,
        }

        # 如果 tokenizer 支持 enable_thinking 参数，则传递
        try:
            text_input = tokenizer.apply_chat_template(
                messages,
                enable_thinking=self.enable_thinking,
                **chat_template_kwargs,
            )
        except TypeError:
            # 如果不支持 enable_thinking 参数，使用默认方式
            text_input = tokenizer.apply_chat_template(
                messages,
                **chat_template_kwargs,
            )

        model_inputs = tokenizer([text_input], return_tensors="pt").to(self.device)

        import torch

        # 准备生成参数
        # 优化：使用 greedy decoding 提升速度和稳定性
        generate_kwargs = {
            "max_new_tokens": self._max_output_tokens_for_text(text),
            "do_sample": False,  # 使用 greedy decoding，更快更稳定
            "pad_token_id": tokenizer.pad_token_id,
            "eos_token_id": self._eos_token_id(),
        }

        # 注意：不使用 stop_strings，因为会导致输出为空
        # thinking 模式通过 apply_chat_template 的 enable_thinking 参数控制

        with torch.no_grad():
            generated_ids = model.generate(
                model_inputs.input_ids,
                attention_mask=model_inputs.attention_mask,
                **generate_kwargs,
            )

        generated_ids = self._completed_tokens(
            generated_ids, model_inputs.input_ids,
            max_new_tokens=generate_kwargs["max_new_tokens"],
            eos_token_id=generate_kwargs["eos_token_id"],
            pad_token_id=generate_kwargs["pad_token_id"],
        )

        response = tokenizer.batch_decode(generated_ids, skip_special_tokens=True)[0]
        return "".join(_filter_thinking([response])).strip()

    def refine_stream(self, text: str):
        """流式精炼文本：逐 token 生成输出。

        Args:
            text: ASR 原始输出文本

        Yields:
            str: 每次生成的新文本片段
        """
        if not text.strip():
            return

        self._lazy_load()

        messages = self._build_messages(text)
        tokenizer = self._tokenizer
        model = self._model
        if tokenizer is None or model is None:
            raise RuntimeError("Qwen3TextRefiner model is not loaded")

        # 根据 enable_thinking 参数控制是否启用思考模式
        chat_template_kwargs = {
            "tokenize": False,
            "add_generation_prompt": True,
        }

        # 如果 tokenizer 支持 enable_thinking 参数，则传递
        try:
            text_input = tokenizer.apply_chat_template(
                messages,
                enable_thinking=self.enable_thinking,
                **chat_template_kwargs,
            )
        except TypeError:
            # 如果不支持 enable_thinking 参数，使用默认方式
            text_input = tokenizer.apply_chat_template(
                messages,
                **chat_template_kwargs,
            )

        model_inputs = tokenizer([text_input], return_tensors="pt").to(self.device)

        import threading

        from transformers import TextIteratorStreamer

        # 创建流式输出器
        streamer = TextIteratorStreamer(
            tokenizer,
            skip_prompt=True,
            skip_special_tokens=True,
        )

        # 准备生成参数
        generate_kwargs = {
            "input_ids": model_inputs.input_ids,
            "attention_mask": model_inputs.attention_mask,
            "max_new_tokens": self._max_output_tokens_for_text(text),
            "temperature": self.temperature,
            "do_sample": True if self.temperature > 0 else False,
            "pad_token_id": tokenizer.pad_token_id,
            "eos_token_id": self._eos_token_id(),
            "streamer": streamer,
        }

        # Capture the actual output/error. The stream's end sentinel does not
        # distinguish EOS from a token limit or a failed generation.
        result: Future[Any] = Future()

        def generate() -> None:
            try:
                result.set_result(model.generate(**generate_kwargs))
            except BaseException as exc:
                result.set_exception(exc)
                # Transformers does not end the streamer on generation errors.
                # Release its reader so the original error reaches the caller.
                streamer.on_finalized_text("", stream_end=True)

        thread = threading.Thread(target=generate, daemon=True)
        thread.start()

        yield from _filter_thinking(streamer)
        self._completed_tokens(
            result.result(), model_inputs.input_ids,
            max_new_tokens=generate_kwargs["max_new_tokens"],
            eos_token_id=generate_kwargs["eos_token_id"],
            pad_token_id=generate_kwargs["pad_token_id"],
        )

    # --- Prompt -----------------------------------------------------------

    _DEFAULT_SYSTEM_PROMPT = (
        "整理以下语音识别文本：\n"
        "- 去除重复词语和句子\n"
        "- 去除语气助词（嗯、啊、呃、那个、这个、然后等）\n"
        "- 添加正确标点符号\n"
        "- 保持原意，通顺易读\n"
        "- 直接输出整理后的结果，不要输出思考过程，不要使用 <think> 标签"
    )

    def _build_messages(self, text: str) -> list[dict[str, str]]:
        """构建 system/user 分离的消息列表（防止 prompt 注入）。"""
        if self.prompt_template:
            user_content = self.prompt_template.replace("{text}", text)
            return [{"role": "user", "content": user_content}]

        return [
            {"role": "system", "content": self._DEFAULT_SYSTEM_PROMPT},
            {"role": "user", "content": f"原文：{text}\n\n整理后："},
        ]
