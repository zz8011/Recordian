# 文档整理设计方案

日期：2026-02-25

## 目标

将项目中所有说明性 Markdown 文件统一整理，制作一份完整的使用文档，并简化 README。

## 文件变更

### 删除
- `QUICKSTART.md`
- `INSTALL.md`
- `docs/llamacpp-guide.md`
- `docs/LLAMACPP_GUIDE.md`

### 新建
- `docs/USER_GUIDE.md` — 综合使用文档

### 修改
- `README.md` — 简化内容，添加指向 `docs/USER_GUIDE.md` 的链接

## README.md 结构

简化后只保留：
1. 项目名称 + 一句话描述
2. 核心功能列表（5 条以内）
3. 快速安装命令
4. 链接到 `docs/USER_GUIDE.md`

## docs/USER_GUIDE.md 结构

采用线性叙述式，按用户使用流程组织：

1. **安装** — 系统要求、安装步骤、验证安装
2. **快速开始** — 首次启动、基本录音操作
3. **配置详解** — 配置文件位置、主要配置项
4. **文本精炼器** — Qwen3 本地模型、LlamaCpp、云端 LLM（OpenAI 兼容）
5. **Preset 系统** — 内置 preset、自定义 preset
6. **热键配置**
7. **常见问题**

## 内容来源

合并以下文件的内容，并更新至重构后的实际状态：
- `README.md`（现有内容）
- `QUICKSTART.md`
- `INSTALL.md`
- `docs/llamacpp-guide.md` / `docs/LLAMACPP_GUIDE.md`

项目结构说明需更新，加入重构后新增的模块：
- `backend_manager.py`
- `waveform_renderer.py`
- `src/recordian/providers/base_text_refiner.py`
