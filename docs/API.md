# Recordian API 文档

## 概览

Recordian 是一个智能语音输入工具，提供语音识别（ASR）和文本精炼功能。

---

## 核心模块

### 1. 配置管理 (config.py)

#### ConfigManager

统一的配置文件管理器。

**方法**:

##### `load(path: Path | str) -> dict[str, Any]`

加载配置文件。

**参数**:
- `path`: 配置文件路径

**返回**:
- 配置字典

**异常**:
- `ConfigError`: 配置文件格式错误或验证失败

**示例**:
```python
from recordian.config import ConfigManager

config = ConfigManager.load("~/.config/recordian/config.json")
print(config["policy"]["confidence_threshold"])
```

##### `save(path: Path | str, config: dict[str, Any]) -> None`

保存配置文件（自动备份）。

**参数**:
- `path`: 配置文件路径
- `config`: 配置字典

**异常**:
- `ConfigError`: 保存失败或验证失败

**示例**:
```python
config = {
    "version": "1.0",
    "policy": {
        "confidence_threshold": 0.9
    }
}
ConfigManager.save("config.json", config)
```

##### `backup(path: Path | str, max_backups: int = 5) -> Path | None`

备份配置文件。

**参数**:
- `path`: 配置文件路径
- `max_backups`: 最大备份数量（默认5）

**返回**:
- 备份文件路径，如果文件不存在则返回 None

**示例**:
```python
backup_path = ConfigManager.backup("config.json", max_backups=3)
print(f"Backup created: {backup_path}")
```

#### ConfigValidator

配置验证器。

##### `validate(config: dict[str, Any]) -> list[str]`

验证配置，返回错误列表。

**参数**:
- `config`: 配置字典

**返回**:
- 错误信息列表，空列表表示验证通过

**示例**:
```python
from recordian.config import ConfigValidator

config = {"policy": {"confidence_threshold": 1.5}}
errors = ConfigValidator.validate(config)
if errors:
    print("Validation errors:", errors)
```

#### ConfigMigrator

配置迁移器。

##### `migrate(config: dict[str, Any]) -> dict[str, Any]`

迁移配置到最新版本。

**参数**:
- `config`: 旧版本配置

**返回**:
- 迁移后的配置

**示例**:
```python
from recordian.config import ConfigMigrator

old_config = {"policy": {"confidence_threshold": 0.9}}
new_config = ConfigMigrator.migrate(old_config)
print(new_config["version"])  # "1.0"
```

---

### 2. 异常处理 (exceptions.py)

#### ConfigError

配置相关错误。

**继承**: `Exception`

**示例**:
```python
from recordian.exceptions import ConfigError

try:
    config = ConfigManager.load("invalid.json")
except ConfigError as e:
    print(f"Config error: {e}")
```

#### ASRError

语音识别错误。

**继承**: `Exception`

#### RefinerError

文本精炼错误。

**继承**: `Exception`

---

### 3. 预设管理 (preset_manager.py)

#### PresetManager

管理 prompt 预设文件。

**构造函数**:
```python
PresetManager(presets_dir: str | Path = "presets")
```

**参数**:
- `presets_dir`: 预设目录路径（默认 "presets"）

**方法**:

##### `list_presets() -> list[str]`

列出所有可用的预设名称。

**返回**:
- 预设名称列表（排序后）

**示例**:
```python
from recordian.preset_manager import PresetManager

manager = PresetManager()
presets = manager.list_presets()
print(f"Available presets: {presets}")
```

##### `load_preset(name: str) -> str`

加载指定预设的 prompt 内容。

**参数**:
- `name`: 预设名称（不含 .md 后缀）

**返回**:
- 预设的 prompt 内容

**异常**:
- `FileNotFoundError`: 预设文件不存在
- `ValueError`: 非法预设名称（包含路径遍历）

**示例**:
```python
manager = PresetManager()
prompt = manager.load_preset("default")
print(prompt)
```

##### `preset_exists(name: str) -> bool`

检查预设是否存在。

**参数**:
- `name`: 预设名称

**返回**:
- 是否存在

**示例**:
```python
if manager.preset_exists("custom"):
    prompt = manager.load_preset("custom")
```

##### `clear_cache() -> None`

清除内存缓存。

**示例**:
```python
manager.clear_cache()  # 强制重新加载预设
```

---

### 4. 性能基准测试 (performance_benchmark.py)

#### PerformanceBenchmark

通用性能测试工具。

**方法**:

##### `measure(name: str, func: Callable, iterations: int = 1) -> PerformanceMetrics`

测量函数性能。

**参数**:
- `name`: 测试名称
- `func`: 要测试的函数
- `iterations`: 迭代次数（默认1）

**返回**:
- 平均性能指标

**示例**:
```python
from recordian.performance_benchmark import PerformanceBenchmark

benchmark = PerformanceBenchmark()

def my_function():
    # 你的代码
    pass

metrics = benchmark.measure("my_test", my_function, iterations=10)
print(f"Average duration: {metrics.duration_ms:.2f}ms")
```

##### `get_results(name: str) -> list[PerformanceMetrics]`

获取测试结果。

**参数**:
- `name`: 测试名称

**返回**:
- 性能指标列表

##### `print_summary() -> None`

打印性能摘要。

**示例**:
```python
benchmark.print_summary()
```

#### PerformanceMetrics

性能指标数据类。

**属性**:
- `duration_ms`: 执行时间（毫秒）
- `memory_mb`: 内存使用（MB）
- `cpu_percent`: CPU 使用率（%）

---

### 5. Provider 接口 (providers/base.py)

#### ASRProvider

语音识别 Provider 抽象基类。

**抽象方法**:

##### `provider_name() -> str`

Provider 名称。

##### `transcribe_file(wav_path: Path, *, hotwords: list[str]) -> ASRResult`

转录音频文件。

**参数**:
- `wav_path`: WAV 音频文件路径
- `hotwords`: 热词列表

**返回**:
- ASR 结果

**示例**:
```python
from recordian.providers.base import ASRProvider
from recordian.models import ASRResult
from pathlib import Path

class MyASRProvider(ASRProvider):
    @property
    def provider_name(self) -> str:
        return "my_asr"

    def transcribe_file(self, wav_path: Path, *, hotwords: list[str]) -> ASRResult:
        # 实现转录逻辑
        return ASRResult(text="转录结果", confidence=0.95)
```

**属性**:

##### `is_cloud() -> bool`

是否为云端 Provider（默认 False）。

---

## 工具函数

### benchmark.py

#### `normalize_text(text: str) -> str`

标准化文本用于 CER 比较。

**参数**:
- `text`: 输入文本

**返回**:
- 标准化后的文本（小写，仅保留字母数字）

#### `edit_distance(a: str, b: str) -> int`

计算 Levenshtein 距离。

**参数**:
- `a`: 字符串 A
- `b`: 字符串 B

**返回**:
- 编辑距离

#### `char_error_rate(reference: str, hypothesis: str) -> tuple[float, int, int]`

计算字符错误率（CER）。

**参数**:
- `reference`: 参考文本
- `hypothesis`: 假设文本

**返回**:
- `(cer, errors, ref_chars)` 元组

**示例**:
```python
from recordian.benchmark import char_error_rate

cer, errors, ref_chars = char_error_rate("hello world", "helo world")
print(f"CER: {cer:.2%}, Errors: {errors}/{ref_chars}")
```

---

## 数据模型

### models.py

#### ASRResult

ASR 结果数据类。

**属性**:
- `text`: 识别文本
- `confidence`: 置信度（0.0-1.0）

**示例**:
```python
from recordian.models import ASRResult

result = ASRResult(text="你好世界", confidence=0.95)
print(f"Text: {result.text}, Confidence: {result.confidence}")
```

---

## 最佳实践

### 1. 配置管理

- 始终使用 `ConfigManager` 加载和保存配置
- 配置会自动验证和迁移
- 保存时会自动创建备份

### 2. 异常处理

- 捕获特定异常类型（`ConfigError`, `ASRError` 等）
- 提供有意义的错误信息

### 3. 性能测试

- 使用 `PerformanceBenchmark` 进行性能测试
- 多次迭代取平均值
- 定期运行基准测试

### 4. Provider 实现

- 继承 `ASRProvider` 实现自定义 Provider
- 实现所有抽象方法
- 处理异常并返回有意义的错误

---

## 版本兼容性

当前 API 版本：1.0

配置版本：1.0

---

## 更多信息

- [用户手册](USER_GUIDE.md)
- [开发者指南](DEVELOPER_GUIDE.md)
- [贡献指南](CONTRIBUTING.md)
