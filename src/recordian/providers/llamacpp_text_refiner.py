from __future__ import annotations

import concurrent.futures
import logging
from pathlib import Path
from typing import Any, cast

from .base_text_refiner import BaseTextRefiner

logger = logging.getLogger(__name__)


class LlamaCppTextRefiner(BaseTextRefiner):
    """基于 llama.cpp 的文本精炼器

    使用 llama-cpp-python 进行本地推理，支持：
    - GGUF 量化模型（Q4_K_M 等）
    - GPU 加速（CUDA）
    - CPU 后备
    - 低显存占用

    preset 模板会被**原样**渲染后作为 user 消息交给模型（与 cloud provider 语义
    一致）；只有在没有 preset 时才退回内置 few-shot 原始补全。
    """

    #: 推理失败时的短日志前缀，便于在 recordian.log 里定位。
    def __init__(
        self,
        model_path: str,
        *,
        n_gpu_layers: int = -1,  # -1 表示全部放 GPU
        n_ctx: int = 3072,  # intent preset 约 1.4k token，2048 会截断掉正文
        n_threads: int | None = None,
        max_new_tokens: int = 512,
        temperature: float = 0.1,
        prompt_template: str | None = None,
        enable_thinking: bool = False,
        timeout: int = 60,  # 本地推理超时时间（秒），默认60秒
    ) -> None:
        super().__init__(
            max_tokens=max_new_tokens,
            temperature=temperature,
            prompt_template=prompt_template,
            enable_thinking=enable_thinking,
        )
        self.model_path = model_path
        self.max_new_tokens = max_new_tokens
        self.timeout = timeout
        self._n_gpu_layers = n_gpu_layers
        self._n_ctx = n_ctx
        self._n_threads = n_threads
        self._llm = None
        self.last_error = ""

    def _lazy_load(self) -> None:
        if self._llm is not None:
            return
        try:
            from llama_cpp import Llama
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "llama-cpp-python 未安装。请执行:\n"
                "pip install llama-cpp-python --extra-index-url "
                "https://abetlen.github.io/llama-cpp-python/whl/cpu"
            ) from exc
        if not Path(self.model_path).expanduser().exists():
            raise RuntimeError(f"精炼模型文件不存在: {self.model_path}")
        self._llm = Llama(
            model_path=str(Path(self.model_path).expanduser()),
            n_gpu_layers=self._n_gpu_layers,
            n_ctx=self._n_ctx,
            n_threads=self._n_threads,
            verbose=False,
            # chat_format=None -> 用 GGUF 自带的 chat template（Qwen3 的模板里带
            # thinking 开关，硬编码 chatml 会丢掉这些行为）
            chat_format=None,
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

        self._lazy_load()

        # 使用 ThreadPoolExecutor 添加超时保护
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self._run_inference, text)
            try:
                generated = future.result(timeout=self.timeout)
            except concurrent.futures.TimeoutError:
                self.last_error = f"timeout>{self.timeout}s"
                logger.warning("llamacpp refine 超时（>%ss），按 ASR 原文提交", self.timeout)
                return text
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("llamacpp refine 失败: %s: %s", type(exc).__name__, exc)
                return text

        generated = str(generated or "").strip()
        if not generated:
            return ""

        # 移除 <think> 标签
        generated = self._remove_think_tags(generated).replace("/no_think", "").strip()

        # 移除可能的前缀
        for prefix in ["输出：", "书面语：", "纪要：", "文档："]:
            if generated.startswith(prefix):
                generated = generated[len(prefix):].strip()
                break

        # 只取第一段（避免多余输出）
        if "\n\n" in generated:
            generated = generated.split("\n\n")[0].strip()

        # 检测并移除重复句子
        generated = self._remove_repetitions(generated)

        return generated

    def _build_chat_messages(self, text: str) -> list[dict[str, str]] | None:
        """渲染 preset 模板为 chat 消息；没有 preset 时返回 None。"""
        template = self.prompt_template
        if not template:
            return None
        if "{text}" in template:
            content = template.replace("{text}", text)
        else:
            content = f"{template}\n原文：\n{text}"
        if not self.enable_thinking:
            # Qwen3 的软开关：非 thinking 模式必须显式关闭，否则会输出推理过程
            content = f"{content}\n/no_think"
        return [{"role": "user", "content": content}]

    def _run_inference(self, text: str) -> str:
        """执行推理（可被超时中断），返回生成的文本。"""
        if self._llm is None:
            raise RuntimeError("llama model not loaded")

        max_tokens = self._max_output_tokens_for_text(text)
        messages = self._build_chat_messages(text)
        if messages is not None:
            try:
                result = self._llm.create_chat_completion(
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=0.1,
                    repeat_penalty=1.2,
                    top_p=0.9,
                    # 注意：这里不能停 "\n\n"——Qwen3 会先吐 ` thinking\n\n response`
                    # （非 thinking 模式下是空块），按空行截断会让输出整个变空。
                    stop=["输入：", "<|"],
                )
            except Exception as exc:  # noqa: BLE001
                # GGUF 没有内嵌 chat template 时，退回内置 few-shot 原始补全
                logger.warning("chat template 不可用（%s），退回 few-shot 补全", type(exc).__name__)
                messages = None

        if messages is None:
            prompt = self._build_fewshot_prompt(text)
            result = cast(dict, self._llm(
                prompt,
                max_tokens=max_tokens,
                temperature=0.1,
                repeat_penalty=1.2,
                top_p=0.9,
                stop=["\n\n", "输入：", "<think>", "<|"],  # 优化停止词
                echo=False,
            ))
            return self._extract_completion_text(result)

        return self._extract_chat_text(cast(dict, result))

    @staticmethod
    def _extract_chat_text(result: dict[str, Any]) -> str:
        choices = result.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        return str(message.get("content") or "").strip()

    @staticmethod
    def _extract_completion_text(result: dict[str, Any]) -> str:
        choices = result.get("choices") or []
        if not choices:
            return ""
        return str(choices[0].get("text") or "").strip()

    def _build_fewshot_prompt(self, text: str) -> str:
        """根据 prompt_template 动态构建 Few-shot prompt

        从 preset 文件内容中提取规则，生成对应的 Few-shot 示例。

        Args:
            text: 输入文本

        Returns:
            Few-shot prompt
        """
        if not self.prompt_template:
            # 如果没有 preset，使用默认的 Few-shot
            return self._build_default_fewshot(text)

        # 从 prompt_template 中提取任务描述和规则
        lines = self.prompt_template.split('\n')
        task_description = ""
        rules = []

        for line in lines:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('-'):
                # 提取规则
                rules.append(line[1:].strip())
            elif '{text}' not in line and not line.startswith('原文：') and not line.startswith('输入：'):
                # 提取任务描述
                if task_description:
                    task_description += " "
                task_description += line

        # 根据规则和任务描述生成 Few-shot prompt
        return self._generate_fewshot_from_rules(task_description, rules, text)

    def _generate_fewshot_from_rules(self, task_description: str, rules: list, text: str) -> str:
        """根据任务描述和规则生成 Few-shot prompt

        Args:
            task_description: 任务描述
            rules: 规则列表
            text: 输入文本

        Returns:
            Few-shot prompt
        """
        # 检测 preset 类型并生成对应的示例
        task_lower = task_description.lower()
        rules_text = ' '.join(rules).lower()

        # formal preset
        if "正式" in task_lower or "书面语" in task_lower or "正式" in rules_text:
            return f"""将口语转换为书面语：
输入：嗯，我觉得这个方案还不错，可以试试看
输出：该方案具有可行性，建议尝试实施。
输入：那个，我们明天开会讨论一下这个问题吧
输出：建议明日召开会议讨论此问题。
输入：{text}
输出："""

        # meeting preset
        elif "会议" in task_lower or "纪要" in task_lower or "会议" in rules_text:
            return f"""整理为会议纪要：
输入：我们今天讨论了项目进度，张三负责前端开发
输出：- 讨论项目进度
- 张三负责前端开发
输入：下周一提交报告，王五跟进测试工作
输出：- 下周一提交报告
- 王五跟进测试工作
输入：{text}
输出："""

        # technical preset
        elif "技术" in task_lower or "文档" in task_lower or "技术" in rules_text:
            return f"""整理为技术文档：
输入：这个函数就是用来处理数据的，把输入转成输出
输出：该函数用于数据处理，将输入数据转换为输出格式。
输入：我们用了一个算法来优化性能，速度提升了很多
输出：采用优化算法提升性能，执行速度显著提高。
输入：{text}
输出："""

        # 检测是否需要数字转换
        has_number_rule = any("数字" in rule or "阿拉伯" in rule for rule in rules)

        # 检测是否需要分段
        has_paragraph_rule = any("分段" in rule or "换行" in rule for rule in rules)

        # default preset：根据规则动态生成示例
        return self._build_default_fewshot(text, has_number_rule, has_paragraph_rule)

    def _build_default_fewshot(self, text: str, has_number_rule: bool = True, has_paragraph_rule: bool = False) -> str:
        """构建默认的 Few-shot prompt

        Args:
            text: 输入文本
            has_number_rule: 是否包含数字转换规则
            has_paragraph_rule: 是否包含分段规则

        Returns:
            Few-shot prompt
        """
        examples = [
            ("嗯这个这个那个我觉得可以", "这个那个我觉得可以"),
            ("打开打开浏览器然后呃进入主页", "打开浏览器进入主页"),
        ]

        # 如果有数字规则，添加数字转换示例
        if has_number_rule:
            examples.extend([
                ("我有一二三四五个苹果", "我有12345个苹果"),
                ("第一步打开文件，第二步编辑内容", "第1步打开文件，第2步编辑内容"),
            ])

        # 如果有分段规则，添加分段示例
        if has_paragraph_rule:
            examples.append((
                "首先我们需要准备材料，然后开始制作，最后进行测试",
                "首先我们需要准备材料。\n然后开始制作。\n最后进行测试。"
            ))

        # 构建 Few-shot prompt
        prompt = "整理语音识别文本：\n"
        for input_text, output_text in examples:
            prompt += f"输入：{input_text}\n输出：{output_text}\n"
        prompt += f"输入：{text}\n输出："

        return prompt

    def _remove_repetitions(self, text: str) -> str:
        """移除重复的句子或短语

        Args:
            text: 输入文本

        Returns:
            移除重复后的文本
        """
        if not text:
            return text

        # 按句号、问号、感叹号分割句子
        import re
        sentences = re.split(r'([。？！])', text)

        # 重新组合句子（保留标点）
        combined = []
        for i in range(0, len(sentences) - 1, 2):
            if i + 1 < len(sentences):
                combined.append(sentences[i] + sentences[i + 1])
        if len(sentences) % 2 == 1:
            combined.append(sentences[-1])

        # 去重：只保留第一次出现的句子
        seen = set()
        result = []
        for sentence in combined:
            sentence_clean = sentence.strip()
            if sentence_clean and sentence_clean not in seen:
                seen.add(sentence_clean)
                result.append(sentence)

        return ''.join(result).strip()
