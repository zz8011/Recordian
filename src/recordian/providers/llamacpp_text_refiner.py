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
        enable_thinking: bool = False,
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
        self.enable_thinking = enable_thinking

        # 初始化 llama.cpp 模型
        self.llm = Llama(
            model_path=model_path,
            n_gpu_layers=n_gpu_layers,
            n_ctx=n_ctx,
            n_threads=n_threads,
            verbose=False,
            chat_format="chatml",  # Qwen 使用 ChatML 格式
        )

    @property
    def provider_name(self) -> str:
        model_name = Path(self.model_path).stem
        return f"llamacpp:{model_name}"

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

    def refine(self, text: str) -> str:
        """精炼文本

        Args:
            text: ASR 原始输出文本

        Returns:
            精炼后的文本
        """
        if not text.strip():
            return ""

        # 根据 prompt_template 判断使用哪种 Few-shot
        prompt = self._build_fewshot_prompt(text)

        # 调用 llama.cpp 推理（使用原始 completion API）
        result = self.llm(
            prompt,
            max_tokens=min(self.max_new_tokens, len(text) * 2 + 50),  # 增加 token 限制
            temperature=0.1,  # 稍微增加随机性，避免过于死板
            repeat_penalty=1.2,  # 降低惩罚，避免影响正常输出
            top_p=0.9,
            stop=["\n\n", "输入：", "<think>", "<|"],  # 优化停止词
            echo=False,
        )

        # 提取生成的文本
        if result and "choices" in result and len(result["choices"]) > 0:
            generated = result["choices"][0]["text"].strip()

            # 移除 <think> 标签
            generated = self._remove_think_tags(generated)

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

        return ""

    def _remove_think_tags(self, text: str) -> str:
        """移除文本中的 <think> 标签及其内容"""
        if not text:
            return ""

        import re

        # Method 1: Remove everything between <think> and </think>
        result = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
        result = result.strip()

        # Method 2: If still contains <think>, try to extract after </think>
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
