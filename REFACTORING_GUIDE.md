# Recordian 重构建议

> 基于《A Philosophy of Software Design》John Ousterhout

## 当前问题

### 1. 模块过大 (Change Amplification)

| 文件 | 大小 | 问题 |
|------|------|------|
| hotkey_dictate.py | 116KB | 功能过多，修改困难 |
| tray_gui.py | 132KB | GUI逻辑与业务逻辑混杂 |
| voice_wake.py | 40KB | 耦合度高 |

### 2. 信息泄露 (Information Leakage)

- 热词管理和唤醒词逻辑有重复代码
- providers 目录结构不够清晰
- audio 相关模块职责边界模糊

### 3. 认知负荷 (Cognitive Load)

- 23个Python文件，组织结构可以更清晰
- 缺少统一的入口和抽象层

---

## 重构方向

### 1. 拆分大模块

**hotkey_dictate.py (116KB)**
```
建议拆分为:
├── hotkey_dictate.py      # 主入口 (~30KB)
├── dictation_core.py     # 核心听写逻辑 (~40KB)
├── dictation_pipeline.py # 处理管道 (~30KB)
└── dictation_config.py   # 配置管理 (~16KB)
```

**tray_gui.py (132KB)**
```
建议拆分为:
├── tray_gui.py         # 主入口 (~30KB)
├── tray_menu.py        # 菜单逻辑 (~30KB)
├── tray_events.py     # 事件处理 (~30KB)
└── tray_settings.py   # 设置界面 (~42KB)
```

### 2. 统一唤醒模块 (Deep Module)

**问题**: voice_wake.py 和 hotkey_dictate.py 都有唤醒逻辑

**建议**: 创建统一的 wake_engine.py
```python
class WakeEngine:
    """统一的唤醒引擎 - Deep Module"""
    def __init__(self, config: WakeConfig):
        self.vad = SileroVAD()  # 使用Silero
        self.asr = QwenASR()
        self.trigger = WakeTrigger()
    
    def start_listening(self) -> None: ...
    def stop_listening(self) -> None: ...
    def on_wake_detected(self, callback): ...
```

### 3. 优化providers目录

```
providers/
├── __init__.py
├── base.py           # 抽象基类
├── asr/              # ASR提供者
│   ├── __init__.py
│   ├── base.py
│   └── qwen_asr.py
├── tts/              # TTS提供者
│   ├── __init__.py
│   ├── base.py
│   └── qwen_tts.py
└── llm/              # LLM提供者
    ├── __init__.py
    ├── base.py
    ├── ollama.py
    └── cloud.py
```

### 4. 创建抽象层 (Strategic Programming)

添加 abstraction 层，减少模块间直接依赖:
```
src/
├── recordian/
│   ├── __init__.py
│   ├── core/              # 核心抽象层 (NEW)
│   │   ├── pipeline.py   # 统一的处理管道
│   │   └── events.py    # 事件系统
│   └── ...
```

---

## 优先级

| 优先级 | 任务 | 预期收益 |
|--------|------|----------|
| P0 | 拆分 hotkey_dictate.py | 降低认知负荷 |
| P0 | 创建 wake_engine.py | 统一唤醒逻辑 |
| P1 | 拆分 tray_gui.py | 分离UI与业务 |
| P1 | 重构 providers 目录 | 清晰边界 |
| P2 | 添加 core 抽象层 | 便于扩展 |

---

## 原则遵循

- ✅ 单一职责
- ✅ 信息隐藏
- ✅ Deep Module (简单接口，丰富实现)
- ✅ 零容忍复杂度
- ✅ 战略编程 (每次改动顺带优化设计)
