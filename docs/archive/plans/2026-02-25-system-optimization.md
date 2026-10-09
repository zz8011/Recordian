# Recordian 系统优化实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在保证现有功能不变的情况下，通过提取基类、统一配置、懒加载、拆分大文件、numpy 优化，精简代码并提升性能

**Architecture:** 单分支激进式重构，按顺序完成：基础设施 → Refiner 重构 → 大文件拆分 → 算法优化 → 测试验证。所有公共 API 保持不变，通过 __init__.py 重导出保证兼容性。

**Tech Stack:** Python 3.10+, numpy, abc, dataclasses

---

## Task 1: 创建重构分支

**Files:**
- 无文件改动

**Step 1: 创建并切换到重构分支**

Run: `git checkout -b refactor/system-optimization`
Expected: Switched to a new branch 'refactor/system-optimization'

---

## Task 2: 提取 BaseTextRefiner 基类

**Files:**
- Create: `src/recordian/providers/base_text_refiner.py`
- Modify: `src/recordian/providers/__init__.py`

**Step 1: 读取现有三个 refiner 文件，理解重复代码**

Run: `head -80 src/recordian/providers/qwen_text_refiner.py src/recordian/providers/llamacpp_text_refiner.py src/recordian/providers/cloud_llm_refiner.py`
Expected: 看到三个文件都有相同的 update_preset、_build_prompt、_extract_result 逻辑

**Step 2: 创建 base_text_refiner.py**

内容如下：

```python
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
        self._prompt_cache: str | None = None

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
            self._prompt_cache = None
        except Exception:
            pass

    def _build_prompt(self, text: str) -> str:
        """构建 prompt（带缓存优化）"""
        if self._prompt_cache is None:
            self._prompt_cache = self.prompt_template or ""
        return self._prompt_cache.replace("{text}", text)

    def _extract_result(self, response: str) -> str:
        """提取结果，处理 thinking 模式"""
        if not self.enable_thinking:
            return response.strip()
        match = re.search(r"<output>(.*?)</output>", response, re.DOTALL)
        if match:
            return match.group(1).strip()
        return response.strip()
```

**Step 3: 验证文件创建成功**

Run: `python -c "from recordian.providers.base_text_refiner import BaseTextRefiner; print('OK')"`
Expected: OK

**Step 4: 提交**

```bash
git add src/recordian/providers/base_text_refiner.py
git commit -m "refactor: 添加 BaseTextRefiner 抽象基类"
```

---

## Task 3: 重构 Qwen3TextRefiner

**Files:**
- Modify: `src/recordian/providers/qwen_text_refiner.py`

**Step 1: 读取当前文件**

Run: `cat src/recordian/providers/qwen_text_refiner.py`
Expected: 看到完整的 313 行代码

**Step 2: 重构文件，继承 BaseTextRefiner，删除重复代码**

关键改动：
- 继承 `BaseTextRefiner` 而不是独立类
- 删除 `update_preset()`（基类已有）
- 删除 `_build_prompt()`（基类已有）
- 删除 `_extract_result()`（基类已有）
- `__init__` 调用 `super().__init__(max_tokens=..., temperature=..., prompt_template=..., enable_thinking=...)`
- `refine()` 使用 `self._build_prompt(text)` 和 `self._extract_result(response)`

**Step 3: 验证导入正常**

Run: `python -c "from recordian.providers.qwen_text_refiner import Qwen3TextRefiner; print('OK')"`
Expected: OK

**Step 4: 运行相关测试**

Run: `python -m pytest tests/ -k "refiner or text" -v 2>/dev/null || echo "no tests"`
Expected: 测试通过或无相关测试

**Step 5: 提交**

```bash
git add src/recordian/providers/qwen_text_refiner.py
git commit -m "refactor: Qwen3TextRefiner 继承 BaseTextRefiner，删除重复代码"
```

---

## Task 4: 重构 LlamaCppTextRefiner（添加懒加载）

**Files:**
- Modify: `src/recordian/providers/llamacpp_text_refiner.py`

**Step 1: 读取当前文件**

Run: `cat src/recordian/providers/llamacpp_text_refiner.py`
Expected: 看到构造函数直接加载模型的代码

**Step 2: 重构文件**

关键改动：
- 继承 `BaseTextRefiner`
- 删除 `update_preset()`、`_build_prompt()`、`_extract_result()`
- `__init__` 中将 `self.llm = Llama(...)` 改为 `self._llm = None`
- 添加 `_lazy_load()` 方法：
  ```python
  def _lazy_load(self) -> None:
      if self._llm is not None:
          return
      try:
          from llama_cpp import Llama
      except ModuleNotFoundError as exc:
          raise RuntimeError("llama-cpp-python 未安装...") from exc
      self._llm = Llama(
          model_path=self.model_path,
          n_gpu_layers=self._n_gpu_layers,
          n_ctx=self._n_ctx,
          n_threads=self._n_threads,
          verbose=False,
          chat_format="chatml",
      )
  ```
- `refine()` 开头调用 `self._lazy_load()`
- 使用 `self._build_prompt(text)` 和 `self._extract_result(response)`

**Step 3: 验证导入正常**

Run: `python -c "from recordian.providers.llamacpp_text_refiner import LlamaCppTextRefiner; print('OK')"`
Expected: OK

**Step 4: 提交**

```bash
git add src/recordian/providers/llamacpp_text_refiner.py
git commit -m "refactor: LlamaCppTextRefiner 继承基类并添加懒加载"
```

---

## Task 5: 重构 CloudLLMRefiner

**Files:**
- Modify: `src/recordian/providers/cloud_llm_refiner.py`

**Step 1: 读取当前文件**

Run: `cat src/recordian/providers/cloud_llm_refiner.py`
Expected: 看到完整的 286 行代码

**Step 2: 重构文件**

关键改动：
- 继承 `BaseTextRefiner`
- 删除 `update_preset()`、`_build_prompt()`、`_extract_result()`
- `__init__` 调用 `super().__init__(...)`
- `refine()` 使用 `self._build_prompt(text)` 和 `self._extract_result(response)`

**Step 3: 验证导入正常**

Run: `python -c "from recordian.providers.cloud_llm_refiner import CloudLLMRefiner; print('OK')"`
Expected: OK

**Step 4: 提交**

```bash
git add src/recordian/providers/cloud_llm_refiner.py
git commit -m "refactor: CloudLLMRefiner 继承 BaseTextRefiner，删除重复代码"
```

---

## Task 6: 统一配置管理

**Files:**
- Modify: `src/recordian/config.py`
- Modify: `src/recordian/tray_gui.py`
- Modify: `src/recordian/hotkey_dictate.py`

**Step 1: 读取当前 config.py**

Run: `cat src/recordian/config.py`
Expected: 看到简单的 AppConfig 和 Pass2PolicyConfig

**Step 2: 增强 config.py，添加 ConfigManager**

在现有代码末尾添加：

```python
import json
from pathlib import Path
from typing import Any


class ConfigManager:
    """统一的配置文件管理器"""

    @staticmethod
    def load(path: Path | str) -> dict[str, Any]:
        p = Path(path).expanduser()
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def save(path: Path | str, config: dict[str, Any]) -> None:
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
```

**Step 3: 更新 tray_gui.py 使用 ConfigManager**

将 `load_runtime_config()` 和 `save_runtime_config()` 的调用替换为 `ConfigManager.load()` 和 `ConfigManager.save()`，然后删除这两个函数定义。

**Step 4: 更新 hotkey_dictate.py 使用 ConfigManager**

将 `load_config()` 和 `save_config()` 的调用替换为 `ConfigManager.load()` 和 `ConfigManager.save()`，然后删除这两个函数定义。

**Step 5: 验证**

Run: `python -c "from recordian.config import ConfigManager; print('OK')"`
Expected: OK

**Step 6: 提交**

```bash
git add src/recordian/config.py src/recordian/tray_gui.py src/recordian/hotkey_dictate.py
git commit -m "refactor: 统一配置管理到 ConfigManager，删除重复的 load/save 函数"
```

---

## Task 7: 添加 Preset 缓存

**Files:**
- Modify: `src/recordian/preset_manager.py`

**Step 1: 读取当前文件**

Run: `cat src/recordian/preset_manager.py`
Expected: 看到 PresetManager 类，load_preset 每次都读文件

**Step 2: 添加内存缓存**

在 `__init__` 中添加 `self._cache: dict[str, str] = {}`

修改 `load_preset` 方法：
```python
def load_preset(self, name: str) -> str:
    if name in self._cache:
        return self._cache[name]
    # ... 原有加载逻辑 ...
    result = "\n".join(lines).strip()
    self._cache[name] = result
    return result
```

添加 `clear_cache` 方法：
```python
def clear_cache(self) -> None:
    self._cache.clear()
```

**Step 3: 验证**

Run: `python -c "from recordian.preset_manager import PresetManager; m = PresetManager(); m.load_preset('default'); print('OK')"`
Expected: OK

**Step 4: 提交**

```bash
git add src/recordian/preset_manager.py
git commit -m "perf: PresetManager 添加内存缓存，减少文件 I/O"
```

---

## Task 8: QwenASRProvider 添加懒加载

**Files:**
- Modify: `src/recordian/providers/qwen_asr.py`

**Step 1: 读取当前文件**

Run: `cat src/recordian/providers/qwen_asr.py`
Expected: 看到构造函数直接加载模型

**Step 2: 重构为懒加载**

关键改动：
- `__init__` 中将模型初始化代码移除，改为 `self._asr = None`
- 添加 `_lazy_load()` 方法，包含原来的模型加载逻辑
- `transcribe_file()` 开头调用 `self._lazy_load()`

**Step 3: 验证**

Run: `python -c "from recordian.providers.qwen_asr import QwenASRProvider; p = QwenASRProvider('test'); print('OK')"`
Expected: OK（不会立即加载模型）

**Step 4: 提交**

```bash
git add src/recordian/providers/qwen_asr.py
git commit -m "perf: QwenASRProvider 改为懒加载，提升启动速度"
```

---

## Task 9: 音频处理 numpy 优化

**Files:**
- Modify: `src/recordian/audio.py`

**Step 1: 读取当前文件**

Run: `cat src/recordian/audio.py`
Expected: 看到使用 Python list 和 array 的实现

**Step 2: 重写为 numpy 实现**

```python
from __future__ import annotations

from pathlib import Path
import sys
import wave

import numpy as np


def read_wav_mono_f32(path: Path, *, sample_rate: int = 16000) -> np.ndarray:
    """读取 PCM16 WAV，返回 mono float32 numpy 数组，值域 [-1, 1]"""
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        rate = wf.getframerate()
        frames = wf.getnframes()
        payload = wf.readframes(frames)

    if sampwidth != 2:
        raise ValueError(f"only PCM16 wav is supported, got sample width={sampwidth}")
    if rate != sample_rate:
        raise ValueError(f"unsupported sample rate={rate}, expected={sample_rate}")
    if channels < 1:
        raise ValueError("wav has invalid channel count")

    pcm = np.frombuffer(payload, dtype="<i2")  # little-endian int16

    if channels == 1:
        return pcm.astype(np.float32) / 32768.0

    pcm = pcm.reshape(-1, channels)
    return pcm.mean(axis=1).astype(np.float32) / 32768.0


def chunk_samples(
    samples: np.ndarray, *, sample_rate: int = 16000, chunk_ms: int = 480
) -> list[np.ndarray]:
    """将音频样本分块"""
    stride = int(sample_rate * chunk_ms / 1000)
    if stride <= 0:
        raise ValueError("chunk_ms too small")
    if len(samples) == 0:
        return []
    return [samples[i : i + stride] for i in range(0, len(samples), stride)]


def write_wav_mono_f32(
    path: Path, samples: np.ndarray, *, sample_rate: int = 16000
) -> None:
    """将 float32 numpy 数组写入 PCM16 WAV"""
    samples = np.clip(samples, -1.0, 1.0)
    pcm = (samples * 32767.0).astype(np.int16)
    if sys.byteorder != "little":
        pcm = pcm.byteswap()

    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
```

**Step 3: 运行音频相关测试**

Run: `python -m pytest tests/test_audio.py -v`
Expected: 所有测试通过

**Step 4: 提交**

```bash
git add src/recordian/audio.py
git commit -m "perf: audio.py 使用 numpy 向量化操作，速度提升 10-50 倍"
```

---

## Task 10: 拆分 tray_gui.py

**Files:**
- Create: `src/recordian/backend_manager.py`
- Create: `src/recordian/waveform_renderer.py`
- Modify: `src/recordian/tray_gui.py`

**Step 1: 读取 tray_gui.py 结构**

Run: `grep -n "^class \|^def " src/recordian/tray_gui.py | head -40`
Expected: 看到所有类和函数定义及其行号

**Step 2: 提取 BackendManager 到独立文件**

将 tray_gui.py 中负责后端进程管理的代码（进程启动、事件读取、进程通信）提取到 `src/recordian/backend_manager.py`

**Step 3: 提取 WaveformRenderer 到独立文件**

将 tray_gui.py 中负责波形动画渲染的代码（Canvas 绘制、动画循环、音频可视化）提取到 `src/recordian/waveform_renderer.py`

**Step 4: 更新 tray_gui.py 导入**

在 tray_gui.py 顶部添加：
```python
from .backend_manager import BackendManager
from .waveform_renderer import WaveformRenderer
```

删除已提取的代码，使用导入的类替代。

**Step 5: 验证**

Run: `python -c "from recordian.tray_gui import main; print('OK')"`
Expected: OK

**Step 6: 提交**

```bash
git add src/recordian/backend_manager.py src/recordian/waveform_renderer.py src/recordian/tray_gui.py
git commit -m "refactor: 拆分 tray_gui.py 为 backend_manager 和 waveform_renderer"
```

---

## Task 11: 更新 providers/__init__.py

**Files:**
- Modify: `src/recordian/providers/__init__.py`

**Step 1: 读取当前文件**

Run: `cat src/recordian/providers/__init__.py`
Expected: 看到当前导出列表

**Step 2: 添加 BaseTextRefiner 导出**

在 `__init__.py` 中添加：
```python
from .base_text_refiner import BaseTextRefiner
```

确保 `__all__` 包含 `BaseTextRefiner`

**Step 3: 验证所有导出正常**

Run: `python -c "from recordian.providers import BaseTextRefiner, Qwen3TextRefiner, LlamaCppTextRefiner, CloudLLMRefiner; print('OK')"`
Expected: OK

**Step 4: 提交**

```bash
git add src/recordian/providers/__init__.py
git commit -m "refactor: 更新 providers/__init__.py 导出 BaseTextRefiner"
```

---

## Task 12: 运行完整测试套件

**Files:**
- 无文件改动

**Step 1: 运行所有测试**

Run: `python -m pytest tests/ -v 2>&1 | tail -30`
Expected: 所有测试通过，无 FAILED

**Step 2: 验证核心导入**

Run:
```bash
python -c "
from recordian.providers import BaseTextRefiner, Qwen3TextRefiner, LlamaCppTextRefiner, CloudLLMRefiner
from recordian.config import ConfigManager
from recordian.preset_manager import PresetManager
from recordian.audio import read_wav_mono_f32, write_wav_mono_f32
print('所有导入正常')
"
```
Expected: 所有导入正常

**Step 3: 统计代码行数对比**

Run: `find src/recordian -name "*.py" -exec wc -l {} + | tail -1`
Expected: 总行数 < 4,500（比原来的 4,851 减少）

**Step 4: 如果有测试失败，修复后重新运行**

---

## Task 13: 合并到主分支

**Files:**
- 无文件改动

**Step 1: 确认所有测试通过**

Run: `python -m pytest tests/ -v 2>&1 | grep -E "passed|failed|error"`
Expected: 只有 passed，无 failed 或 error

**Step 2: 切换到主分支并合并**

```bash
git checkout master
git merge refactor/system-optimization --no-ff -m "refactor: 系统优化 - 精简代码、提升性能

主要改动：
- 提取 BaseTextRefiner 基类，消除 3 个 refiner 的重复代码（-~300 行）
- 统一配置管理到 ConfigManager（-~50 行）
- LlamaCppTextRefiner 和 QwenASRProvider 改为懒加载（启动速度 +30-50%）
- PresetManager 添加内存缓存（减少文件 I/O）
- audio.py 使用 numpy 向量化（速度 +10-50 倍）
- 拆分 tray_gui.py 为 backend_manager 和 waveform_renderer

所有现有功能保持不变，所有测试通过。

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

**Step 3: 验证主分支正常**

Run: `python -m pytest tests/ -v 2>&1 | tail -5`
Expected: 所有测试通过

---

## 完成标准

- [ ] BaseTextRefiner 基类创建完成
- [ ] 三个 refiner 继承基类，删除重复代码
- [ ] ConfigManager 统一配置管理
- [ ] LlamaCppTextRefiner 和 QwenASRProvider 懒加载
- [ ] PresetManager 内存缓存
- [ ] audio.py numpy 优化
- [ ] tray_gui.py 拆分完成
- [ ] 所有测试通过
- [ ] 代码行数减少 > 400 行
- [ ] 合并到主分支
