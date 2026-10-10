# 归档文档

这里的文档**不再维护**，保留原因只有两个：记录当时的决策过程，或提供仍可能有参考价值的技术细节。

它们的内容大多基于 2026-02 至 2026-03 的代码状态，其中的文件路径、命令、默认值可能已经和当前版本不一致。**不要把这些文档当作当前行为的依据**，需要准确信息请回到 [`../README.md`](../README.md)（文档索引）或直接看代码。

## 内容分类

### 过程记录

一次性任务的产物，记录「当时做了什么、为什么」：

| 文件 | 内容 |
| --- | --- |
| `sprint1-delivery.md` / `sprint2-delivery.md` / `sprint3-progress.md` | 2026 年初三轮迭代的过程记录 |
| `GIT_HISTORY_3DAYS.md` | 2026-02-28 至 03-03 的提交清单快照 |

### 技术研究

结论可能已经落地或已被取代，但分析过程仍有参考价值：

| 文件 | 内容 |
| --- | --- |
| `README-OPTIMIZATION.md` | 语音唤醒 CPU 优化文档包的索引 |
| `RESEARCH_SUMMARY_CN.md` | 语音唤醒 CPU 优化研究总结 |
| `voice-wake-optimization-summary.md` | 上述优化的实施方案 |
| `voice-wake-optimization-examples.py` | 配套示例代码 |
| `QUICK-REFERENCE-CPU-OPTIMIZATION.md` | 优化速查卡 |
| `onnx-quantization-guide.md` | Sherpa-ONNX 模型量化指南 |
| `python-performance-optimization-research.md` | Python 侧性能优化调研 |

### 历史设计

| 目录 | 内容 |
| --- | --- |
| `plans/` | 2026-02 的文档重组、预设优化、系统优化设计稿 |
| `requirements/` | 远程粘贴 Agent 的需求文档 |
| `sparc/` | 托盘拆分与托盘 UX 修复的 SPARC 规格 |

### 未发布的 Wiki

| 目录 | 内容 |
| --- | --- |
| `wiki/` | 为 GitHub Wiki 准备的页面（安装、快速开始、FAQ 等），内容与 `docs/USER_GUIDE.md` 重复，Wiki 未实际发布 |
| `WIKI-PUBLISH-GUIDE.md` | 上述 Wiki 的手动发布步骤 |
