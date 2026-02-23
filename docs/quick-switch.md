# 快速切换指南

## 切换 Provider

### 切换到本地模型

编辑 `~/.config/recordian/hotkey.json`：

```json
{
  "refine_provider": "local"
}
```

### 切换到云端 API

```json
{
  "refine_provider": "cloud",
  "refine_api_key": "sk-cp-7-PJNWFYw24rpsNLK4HB3w4RHGpbo8EL3rXczD1T--u7PwXaWlUA9BGolDgN7A_TljgnQC9W0zA_RvueqsVjXUJf8gfH8QSd-pRRwxoL-yuBXexECJhcDEs"
}
```

## 切换预设

```json
{
  "refine_preset": "default"     // 默认整理
  "refine_preset": "formal"      // 正式书面语
  "refine_preset": "summary"     // 简洁总结
  "refine_preset": "meeting"     // 会议纪要
  "refine_preset": "technical"   // 技术文档
}
```

## 常用组合

### 日常使用（快速、免费）

```json
{
  "enable_text_refine": true,
  "refine_provider": "local",
  "refine_preset": "default"
}
```

### 重要会议（高质量）

```json
{
  "enable_text_refine": true,
  "refine_provider": "cloud",
  "refine_preset": "meeting",
  "refine_api_key": "your-api-key"
}
```

### 技术文档（专业）

```json
{
  "enable_text_refine": true,
  "refine_provider": "cloud",
  "refine_preset": "technical",
  "refine_api_key": "your-api-key"
}
```

### 正式书面语（专业）

```json
{
  "enable_text_refine": true,
  "refine_provider": "cloud",
  "refine_preset": "formal",
  "refine_api_key": "your-api-key"
}
```

## 测试配置

修改配置后，运行测试验证：

```bash
# 测试本地模型
./scripts/test_text_refiner.py

# 测试云端 API
./scripts/test_cloud_llm.py

# 测试完整流程
./scripts/test_cloud_pipeline.py
```

## 注意事项

- 修改配置后需要重启 `recordian-hotkey-dictate`
- 云端 API 需要网络连接
- 本地模型需要 ~1.5GB 显存
- API key 已配置在示例中，可直接使用
