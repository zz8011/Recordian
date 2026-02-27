# Recordian 开发实施计划

**制定日期**: 2026-02-28
**基于**: 阶段1评估报告（16个P0问题，23个P1问题）
**总工作量**: 34天（约7周）

---

## 一、迭代拆分策略

### Sprint 1: 稳定性修复（P0问题）- 2周
**目标**: 修复所有阻塞性问题，确保系统稳定运行
**优先级**: 最高
**工作量**: 10天

### Sprint 2: 质量提升（P1问题）- 2周
**目标**: 修复严重问题，提升代码质量和测试覆盖率
**优先级**: 高
**工作量**: 11天

### Sprint 3: 架构优化（P2问题）- 3周
**目标**: 降低技术债务，优化性能和可维护性
**优先级**: 中
**工作量**: 16天

---

## 二、Sprint 1 - 稳定性修复（P0问题）

### 迭代目标
- 修复所有资源泄漏问题（进程、临时文件、线程）
- 修复所有线程安全问题
- 建立CI/CD流水线
- 集成错误追踪系统
- 补充关键路径测试

### 任务清单

#### 任务1.1: 修复资源泄漏问题（3天）
**负责模块**: 后端、架构

**改动文件**:
- `src/recordian/engine.py` (Pass2超时任务取消)
- `src/recordian/realtime.py` (Pass2超时任务取消)
- `src/recordian/providers/qwen_asr.py` (VAD临时文件清理)
- `src/recordian/linux_dictate.py` (进程终止逻辑)
- `src/recordian/backend_manager.py` (进程生命周期管理)

**具体改动**:
1. **Pass2超时任务取消** (`engine.py:74-79`, `realtime.py:95-112`)
   ```python
   # 修改前
   with ThreadPoolExecutor(max_workers=1) as executor:
       future = executor.submit(...)
       try:
           return future.result(timeout=timeout_ms / 1000)
       except TimeoutError:
           return None  # 任务仍在后台运行

   # 修改后
   executor = ThreadPoolExecutor(max_workers=1)
   future = executor.submit(...)
   try:
       return future.result(timeout=timeout_ms / 1000)
   except TimeoutError:
       future.cancel()  # 尝试取消
       executor.shutdown(wait=False, cancel_futures=True)  # 强制关闭
       return None
   ```

2. **VAD临时文件清理** (`qwen_asr.py:119-130`)
   ```python
   # 修改前
   fd, temp_path = tempfile.mkstemp(suffix='.wav')
   os.close(fd)
   torchaudio.save(temp_path, trimmed, sample_rate)
   return temp_path

   # 修改后
   with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
       temp_path = f.name
   try:
       torchaudio.save(temp_path, trimmed, sample_rate)
       return temp_path
   finally:
       # 调用方负责清理，或使用上下文管理器
       pass
   ```

3. **进程终止逻辑** (`linux_dictate.py:263-297`, `backend_manager.py:76-87`)
   - 添加全局进程注册表
   - 使用`atexit`注册清理函数
   - 改进terminate→wait→kill链路，添加超时检查

**验收标准**:
- ✅ 24小时压力测试无资源泄漏
- ✅ 临时文件目录无残留文件
- ✅ 进程列表无僵尸进程

---

#### 任务1.2: 修复线程安全问题（2天）
**负责模块**: 前端、后端

**改动文件**:
- `src/recordian/linux_commit.py` (剪贴板定时器加锁)
- `src/recordian/hotkey_dictate.py` (状态管理重构)
- `src/recordian/tray_gui.py` (GTK线程安全)

**具体改动**:
1. **剪贴板定时器加锁** (`linux_commit.py:103-121`)
   ```python
   # 添加锁保护
   self._timer_lock = threading.Lock()

   def commit(self, text: str):
       with self._timer_lock:
           if self._clear_timer is not None:
               self._clear_timer.cancel()
               self._clear_timer = None
           # ... 设置新定时器
   ```

2. **状态管理重构** (`hotkey_dictate.py:345-353`)
   - 使用`threading.RLock`替代`Lock`
   - 引入`enum.Enum`定义状态机
   - 禁止直接修改state字典

3. **GTK线程安全** (`tray_gui.py:1222-1258`)
   - 确保所有GTK调用都通过`GLib.idle_add`包装
   - 审查所有跨线程调用路径

**验收标准**:
- ✅ 并发测试通过（100次快速触发无异常）
- ✅ 剪贴板内容正确清理
- ✅ GTK无崩溃或死锁

---

#### 任务1.3: 建立CI/CD流水线（2天）
**负责模块**: DevOps

**改动文件**:
- `.github/workflows/ci.yml` (新建)
- `.github/workflows/release.yml` (新建)

**具体改动**:
1. **创建CI流水线** (`.github/workflows/ci.yml`)
   ```yaml
   name: CI
   on: [push, pull_request]
   jobs:
     test:
       runs-on: ubuntu-latest
       steps:
         - uses: actions/checkout@v3
         - uses: actions/setup-python@v4
           with:
             python-version: '3.10'
         - name: Install dependencies
           run: |
             pip install -e .[dev]
         - name: Run tests
           run: |
             pytest tests/ --cov=src/recordian --cov-report=xml
         - name: Upload coverage
           uses: codecov/codecov-action@v3
   ```

2. **创建发布流水线** (`.github/workflows/release.yml`)
   - 自动打包
   - 生成SHA256校验和
   - 创建GitHub Release

**验收标准**:
- ✅ CI流水线正常运行
- ✅ 测试覆盖率报告生成
- ✅ 发布流程自动化

---

#### 任务1.4: 集成错误追踪系统（1天）
**负责模块**: DevOps

**改动文件**:
- `src/recordian/__init__.py` (Sentry初始化)
- `pyproject.toml` (添加sentry-sdk依赖)

**具体改动**:
```python
# src/recordian/__init__.py
import sentry_sdk

sentry_sdk.init(
    dsn="https://your-dsn@sentry.io/project-id",
    environment="production",
    traces_sample_rate=0.1,
)
```

**验收标准**:
- ✅ Sentry正常接收错误报告
- ✅ 错误堆栈完整可读

---

#### 任务1.5: 补充关键路径测试（2天）
**负责模块**: 测试

**改动文件**:
- `tests/test_waveform_renderer.py` (新建)
- `tests/test_backend_manager.py` (新建)
- `tests/test_engine.py` (补充超时测试)

**具体改动**:
1. **waveform_renderer测试**
   - Mock pyglet和OpenGL
   - 测试shader编译失败降级
   - 测试命令队列满时的阻塞行为

2. **backend_manager测试**
   - Mock subprocess
   - 测试子进程异常退出
   - 测试事件解析失败

3. **engine超时测试**
   - 测试Pass2超时降级到Pass1
   - 测试超时值配置错误

**验收标准**:
- ✅ 新增测试全部通过
- ✅ 覆盖率提升至70%+

---

### Sprint 1 风险控制

**风险1: 重构引入新Bug**
- **缓解**: 每个改动都补充对应测试
- **回滚**: 使用git分支隔离，问题代码立即回滚

**风险2: CI/CD配置错误**
- **缓解**: 先在fork仓库测试
- **回滚**: 删除workflow文件

**风险3: Sentry配置泄露**
- **缓解**: 使用GitHub Secrets存储DSN
- **回滚**: 撤销Sentry项目

### Sprint 1 验收标准

**功能验收**:
- ✅ 所有P0问题已修复
- ✅ 24小时压力测试通过
- ✅ 并发测试通过

**质量验收**:
- ✅ CI/CD流水线正常运行
- ✅ 测试覆盖率 ≥ 70%
- ✅ Sentry正常接收错误

**性能验收**:
- ✅ 无资源泄漏
- ✅ 响应时间无退化

---

## 三、Sprint 2 - 质量提升（P1问题）

### 迭代目标
- 修复所有严重问题
- 补充核心模块测试
- 统一错误处理和日志
- 添加超时保护
- 完善监控和告警

### 任务清单

#### 任务2.1: 修复前端P1问题（3天）
**改动文件**:
- `src/recordian/hotkey_dictate.py` (音频线程停止)
- `src/recordian/waveform_renderer.py` (异步初始化)
- `src/recordian/tray_gui.py` (设置窗口单例、模式切换反馈)

**具体改动**:
1. **音频线程停止逻辑** (`hotkey_dictate.py:392-460`)
   - 在所有异常路径设置`level_stop`事件
   - 添加线程join超时检查

2. **波形渲染器异步初始化** (`waveform_renderer.py:12-29`)
   - 将初始化移到后台线程
   - 主线程立即返回，后台完成后通知

3. **设置窗口单例管理** (`tray_gui.py:326-333`)
   - 使用`weakref`或显式清理引用
   - 添加窗口销毁回调

4. **模式切换反馈** (`tray_gui.py:219-230`)
   - 添加toast通知或状态栏提示

**验收标准**:
- ✅ 音频线程正确停止
- ✅ UI启动无阻塞
- ✅ 设置窗口无重复创建
- ✅ 模式切换有明确反馈

---

#### 任务2.2: 修复后端P1问题（2天）
**改动文件**:
- `src/recordian/hotkey_dictate.py` (精炼器超时)
- `src/recordian/providers/cloud_llm_refiner.py` (超时配置)
- `src/recordian/policy.py` (正则优化)

**具体改动**:
1. **精炼器超时保护** (`hotkey_dictate.py:547-573`)
   - 为本地LLM推理添加超时
   - 云端API超时从120s降至30s

2. **正则表达式优化** (`policy.py:12-17`)
   - 预编译正则并缓存匹配结果
   - 使用更高效的正则模式

**验收标准**:
- ✅ 精炼器超时正常工作
- ✅ 正则性能提升50%+

---

#### 任务2.3: 统一错误处理（2天）
**改动文件**:
- `src/recordian/exceptions.py` (新建)
- 所有模块 (替换`except Exception`)

**具体改动**:
1. **定义异常层次结构**
   ```python
   # src/recordian/exceptions.py
   class RecordianError(Exception):
       """Base exception"""

   class ASRError(RecordianError):
       """ASR related errors"""

   class CommitError(RecordianError):
       """Commit related errors"""

   class ConfigError(RecordianError):
       """Config related errors"""
   ```

2. **替换宽泛异常捕获**
   - 将`except Exception`替换为具体异常类型
   - 添加异常上下文信息

**验收标准**:
- ✅ 所有异常都有明确类型
- ✅ 错误信息包含上下文

---

#### 任务2.4: 统一日志系统（2天）
**改动文件**:
- `src/recordian/logging_config.py` (新建)
- 所有模块 (使用统一logger)

**具体改动**:
1. **创建日志配置**
   ```python
   # src/recordian/logging_config.py
   import logging
   from logging.handlers import RotatingFileHandler

   def setup_logging(level=logging.INFO):
       logger = logging.getLogger('recordian')
       logger.setLevel(level)

       # 文件handler
       fh = RotatingFileHandler(
           '~/.local/share/recordian/recordian.log',
           maxBytes=10*1024*1024,  # 10MB
           backupCount=5
       )
       fh.setFormatter(logging.Formatter(
           '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
       ))
       logger.addHandler(fh)

       return logger
   ```

2. **所有模块使用统一logger**
   ```python
   from recordian.logging_config import setup_logging
   logger = setup_logging()
   ```

**验收标准**:
- ✅ 所有模块使用统一logger
- ✅ 日志轮转正常工作
- ✅ 日志级别可配置

---

#### 任务2.5: 补充核心测试（2天）
**改动文件**:
- `tests/test_base_text_refiner.py` (新建)
- `tests/test_llamacpp_text_refiner.py` (新建)
- `tests/test_cloud_llm_refiner.py` (新建)
- `tests/test_policy.py` (补充边界测试)

**具体改动**:
1. **base_text_refiner测试**
   - 测试`_remove_think_tags()`正则回溯攻击
   - 测试嵌套`<think>`标签

2. **llamacpp_text_refiner测试**
   - 测试Few-shot prompt超长输入
   - 测试`_remove_repetitions()`无限循环

3. **cloud_llm_refiner测试**
   - 使用`requests-mock`模拟超时
   - 测试三种API格式

4. **policy边界测试**
   - 测试confidence=0.88临界点
   - 测试多条件组合

**验收标准**:
- ✅ 新增测试全部通过
- ✅ 覆盖率提升至80%+

---

### Sprint 2 风险控制

**风险1: 超时配置过短**
- **缓解**: 根据实际测试调整超时值
- **回滚**: 恢复原超时配置

**风险2: 日志过多影响性能**
- **缓解**: 使用异步日志handler
- **回滚**: 降低日志级别

### Sprint 2 验收标准

**功能验收**:
- ✅ 所有P1问题已修复
- ✅ 错误处理统一
- ✅ 日志系统完善

**质量验收**:
- ✅ 测试覆盖率 ≥ 80%
- ✅ 所有超时正常工作

**性能验收**:
- ✅ 正则性能提升50%+
- ✅ 响应时间无退化

---

## 四、Sprint 3 - 架构优化（P2问题）

### 迭代目标
- 拆分大文件，降低技术债务
- 性能优化（缓存、批处理）
- 文档完善
- 插件化架构重构

### 任务清单

#### 任务3.1: 拆分tray_gui.py（5天）
**改动文件**:
- `src/recordian/tray_gui.py` (拆分为多个文件)
- `src/recordian/gui/` (新建目录)
  - `tray_menu.py`
  - `settings_window.py`
  - `preset_editor.py`
  - `config_schema.py`

**具体改动**:
1. **提取TrayMenu类** (300行)
2. **提取SettingsWindow类** (800行)
3. **提取PresetEditor类** (200行)
4. **引入ConfigSchema验证层** (100行)

**验收标准**:
- ✅ 单文件不超过500行
- ✅ 职责清晰，易于维护
- ✅ 所有功能正常工作

---

#### 任务3.2: 性能优化（3天）
**改动文件**:
- `src/recordian/providers/qwen_asr.py` (VAD缓存)
- `src/recordian/preset_manager.py` (LRU缓存)
- `src/recordian/waveform_renderer.py` (OpenGL优化)

**具体改动**:
1. **VAD结果缓存**
   - 使用`@lru_cache`缓存VAD结果
   - 缓存到临时文件

2. **PresetManager缓存**
   - 添加文件修改时间检测
   - 使用`@lru_cache(maxsize=32)`

3. **波形渲染优化**
   - 使用pyglet的OpenGL后端
   - 减少不必要的重绘

**验收标准**:
- ✅ ASR延迟降低50-100ms
- ✅ 预设加载速度提升10x
- ✅ 波形渲染帧率稳定60fps

---

#### 任务3.3: 文档完善（3天）
**改动文件**:
- `docs/ARCHITECTURE.md` (新建)
- `docs/API.md` (新建)
- `CHANGELOG.md` (新建)
- 所有核心类 (添加docstring)

**具体改动**:
1. **架构文档**
   - 系统架构图
   - 模块职责说明
   - 数据流图

2. **API文档**
   - 使用Sphinx生成
   - 所有公开API都有文档

3. **变更日志**
   - 遵循Keep a Changelog格式
   - 记录所有版本变更

**验收标准**:
- ✅ 架构文档完整
- ✅ API文档可生成
- ✅ CHANGELOG记录完整

---

#### 任务3.4: 其他P2优化（5天）
**改动文件**:
- 多个模块 (配置缓存、模型卸载、批处理等)

**具体改动**:
1. **配置缓存优化**
2. **模型卸载机制**
3. **GPU资源监控**
4. **批处理优化**
5. **代码重复消除**

**验收标准**:
- ✅ 所有P2问题已修复
- ✅ 代码质量提升

---

### Sprint 3 风险控制

**风险1: 重构破坏现有功能**
- **缓解**: 充分的回归测试
- **回滚**: 保留原代码分支

**风险2: 性能优化效果不明显**
- **缓解**: 先benchmark再优化
- **回滚**: 恢复原实现

### Sprint 3 验收标准

**功能验收**:
- ✅ 所有P2问题已修复
- ✅ 所有功能正常工作

**质量验收**:
- ✅ 技术债务降至20%以下
- ✅ 文档完整

**性能验收**:
- ✅ ASR延迟降低50-100ms
- ✅ 内存占用降低20%+

---

## 五、总体风险控制策略

### 1. 分支管理策略
- **主分支**: `master` (稳定版本)
- **开发分支**: `develop` (集成分支)
- **特性分支**: `feature/sprint-1-*`, `feature/sprint-2-*`, `feature/sprint-3-*`
- **修复分支**: `hotfix/*`

### 2. 代码审查流程
- 所有改动必须通过PR
- 至少1人审查
- CI测试必须通过

### 3. 回滚策略
- 每个Sprint结束打tag
- 问题版本立即回滚到上一个稳定tag
- 保留原代码分支至少1个月

### 4. 测试策略
- 单元测试覆盖率 ≥ 80%
- 关键路径必须有集成测试
- 每个Sprint结束进行回归测试

---

## 六、资源需求

### 人力资源
- **后端开发**: 1人（Sprint 1-3）
- **前端开发**: 1人（Sprint 1-2）
- **测试工程师**: 1人（Sprint 1-3）
- **DevOps工程师**: 0.5人（Sprint 1）

### 时间资源
- **Sprint 1**: 2周（10个工作日）
- **Sprint 2**: 2周（11个工作日）
- **Sprint 3**: 3周（16个工作日）
- **总计**: 7周（37个工作日）

### 硬件资源
- **开发环境**: GPU服务器（用于测试ASR模型）
- **CI/CD**: GitHub Actions（免费额度足够）
- **监控**: Sentry（免费版足够）

---

## 七、里程碑与交付物

### Sprint 1 里程碑（第2周末）
**交付物**:
- ✅ 所有P0问题修复完成
- ✅ CI/CD流水线上线
- ✅ Sentry错误追踪集成
- ✅ 测试覆盖率达到70%
- ✅ Beta版本发布

### Sprint 2 里程碑（第4周末）
**交付物**:
- ✅ 所有P1问题修复完成
- ✅ 错误处理和日志统一
- ✅ 测试覆盖率达到80%
- ✅ RC版本发布

### Sprint 3 里程碑（第7周末）
**交付物**:
- ✅ 所有P2问题修复完成
- ✅ 架构优化完成
- ✅ 文档完善
- ✅ v0.2.0正式版本发布

---

## 八、成功标准

### 稳定性标准
- ✅ 24小时压力测试无崩溃
- ✅ 无资源泄漏
- ✅ 无线程安全问题

### 质量标准
- ✅ 测试覆盖率 ≥ 80%
- ✅ CI/CD流水线正常运行
- ✅ 错误追踪系统正常工作

### 性能标准
- ✅ ASR延迟 < 1s（90%分位）
- ✅ 精炼延迟 < 2s（90%分位）
- ✅ 内存占用稳定

### 可维护性标准
- ✅ 单文件不超过500行
- ✅ 技术债务 < 20%
- ✅ 文档完整

---

## 九、后续规划

### v0.3.0 (3个月后)
- 插件化架构重构
- 支持更多ASR模型
- 支持更多上屏后端

### v1.0.0 (6个月后)
- 生产环境稳定运行
- 完整的用户文档
- 社区支持

---

**计划制定人**: Team Lead
**审批状态**: 待审批
**下一步**: 开始Sprint 1执行
