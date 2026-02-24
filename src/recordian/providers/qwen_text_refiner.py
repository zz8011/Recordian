from __future__ import annotations

from pathlib import Path


class Qwen3TextRefiner:
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
        self.model_name = model_name
        self.device = device
        self.dtype = dtype
        self.max_new_tokens = max_new_tokens
        self.temperature = temperature
        self.prompt_template = prompt_template
        self.enable_thinking = enable_thinking
        self._model = None
        self._tokenizer = None

    @property
    def provider_name(self) -> str:
        return f"qwen3-refiner:{self.model_name}"

    def update_preset(self, preset_name: str) -> None:
        """动态更新 preset（热切换）

        Args:
            preset_name: preset 名称（如 "default", "formal" 等）
        """
        from recordian.preset_manager import PresetManager

        preset_mgr = PresetManager()
        try:
            self.prompt_template = preset_mgr.load_preset(preset_name)
        except Exception:
            # 如果加载失败，保持当前 preset
            pass

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
        self._lazy_load()

        if not text.strip():
            return ""

        prompt = self._build_prompt(text)
        messages = [{"role": "user", "content": prompt}]

        # 根据 enable_thinking 参数控制是否启用思考模式
        chat_template_kwargs = {
            "tokenize": False,
            "add_generation_prompt": True,
        }

        # 如果 tokenizer 支持 enable_thinking 参数，则传递
        try:
            text_input = self._tokenizer.apply_chat_template(
                messages,
                enable_thinking=self.enable_thinking,
                **chat_template_kwargs,
            )
        except TypeError:
            # 如果不支持 enable_thinking 参数，使用默认方式
            text_input = self._tokenizer.apply_chat_template(
                messages,
                **chat_template_kwargs,
            )

        model_inputs = self._tokenizer([text_input], return_tensors="pt").to(self.device)

        import torch

        # 准备生成参数
        # 优化：使用 greedy decoding 提升速度和稳定性
        generate_kwargs = {
            "max_new_tokens": self.max_new_tokens,
            "do_sample": False,  # 使用 greedy decoding，更快更稳定
            "pad_token_id": self._tokenizer.pad_token_id,
            "eos_token_id": self._tokenizer.eos_token_id,
        }

        # 注意：不使用 stop_strings，因为会导致输出为空
        # thinking 模式通过 apply_chat_template 的 enable_thinking 参数控制

        with torch.no_grad():
            generated_ids = self._model.generate(
                model_inputs.input_ids,
                attention_mask=model_inputs.attention_mask,
                **generate_kwargs,
            )

        generated_ids = [
            output_ids[len(input_ids):]
            for input_ids, output_ids in zip(model_inputs.input_ids, generated_ids)
        ]

        response = self._tokenizer.batch_decode(generated_ids, skip_special_tokens=True)[0]

        # 始终移除 <think> 标签，只保留最终结果
        result = response.strip()

        # Method 1: Remove everything between <think> and </think>
        import re
        result = re.sub(r'<think>.*?</think>', '', result, flags=re.DOTALL)
        result = result.strip()

        # Method 2: If still starts with <think>, try to extract after </think>
        if '<think>' in result:
            parts = result.split('</think>')
            if len(parts) > 1:
                result = parts[-1].strip()
            else:
                # No closing tag, remove everything after <think>
                result = result.split('<think>')[0].strip()

        # Method 3: Remove any remaining <think> or </think> tags
        result = result.replace('<think>', '').replace('</think>', '').strip()

        return result

    def refine_stream(self, text: str):
        """流式精炼文本：逐 token 生成输出。

        Args:
            text: ASR 原始输出文本

        Yields:
            str: 每次生成的新文本片段
        """
        self._lazy_load()

        if not text.strip():
            return

        prompt = self._build_prompt(text)
        messages = [{"role": "user", "content": prompt}]

        # 根据 enable_thinking 参数控制是否启用思考模式
        chat_template_kwargs = {
            "tokenize": False,
            "add_generation_prompt": True,
        }

        # 如果 tokenizer 支持 enable_thinking 参数，则传递
        try:
            text_input = self._tokenizer.apply_chat_template(
                messages,
                enable_thinking=self.enable_thinking,
                **chat_template_kwargs,
            )
        except TypeError:
            # 如果不支持 enable_thinking 参数，使用默认方式
            text_input = self._tokenizer.apply_chat_template(
                messages,
                **chat_template_kwargs,
            )

        model_inputs = self._tokenizer([text_input], return_tensors="pt").to(self.device)

        import torch
        from transformers import TextIteratorStreamer
        import threading

        # 创建流式输出器
        streamer = TextIteratorStreamer(
            self._tokenizer,
            skip_prompt=True,
            skip_special_tokens=True,
        )

        # 准备生成参数
        generate_kwargs = {
            "input_ids": model_inputs.input_ids,
            "attention_mask": model_inputs.attention_mask,
            "max_new_tokens": self.max_new_tokens,
            "temperature": self.temperature,
            "do_sample": True if self.temperature > 0 else False,
            "pad_token_id": self._tokenizer.pad_token_id,
            "eos_token_id": self._tokenizer.eos_token_id,
            "streamer": streamer,
        }

        # 在后台线程中运行生成
        thread = threading.Thread(target=self._model.generate, kwargs=generate_kwargs)
        thread.start()

        # 始终过滤 <think> 标签，只输出最终结果
        in_think_block = False
        buffer = ""

        for new_text in streamer:
            buffer += new_text

            # 检测进入 <think> 块
            if '<think>' in buffer and not in_think_block:
                in_think_block = True
                # 输出 <think> 之前的内容
                before_think = buffer.split('<think>')[0]
                if before_think:
                    yield before_think
                # 保留 <think> 之后的内容继续处理
                buffer = buffer.split('<think>', 1)[1] if '<think>' in buffer else ""
                continue

            # 检测退出 </think> 块
            if '</think>' in buffer and in_think_block:
                in_think_block = False
                # 跳过 </think> 及之前的内容
                parts = buffer.split('</think>', 1)
                buffer = parts[1] if len(parts) > 1 else ""
                continue

            # 如果不在 think 块中，输出文本
            if not in_think_block:
                # 检查是否可能有未完成的标签
                if buffer.endswith('<') or buffer.endswith('<t') or buffer.endswith('<th') or \
                   buffer.endswith('<thi') or buffer.endswith('<thin') or buffer.endswith('<think'):
                    # 可能是标签的开始，等待更多内容
                    continue

                if buffer:
                    yield buffer
                    buffer = ""

        # 输出剩余内容（如果不在 think 块中）
        if buffer and not in_think_block:
            yield buffer

        thread.join()

    def _build_prompt(self, text: str) -> str:
        """构建文本精炼 prompt。"""
        if self.prompt_template:
            # 使用自定义模板，{text} 会被替换为实际文本
            return self.prompt_template.format(text=text)

        # 默认 prompt
        return f"""整理以下语音识别文本：
- 去除重复词语和句子
- 去除语气助词（嗯、啊、呃、那个、这个、然后等）
- 添加正确标点符号
- 保持原意，通顺易读
- 直接输出整理后的结果，不要输出思考过程，不要使用 <think> 标签

原文：{text}

整理后："""
