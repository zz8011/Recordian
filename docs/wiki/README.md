# 如何在 GitHub 上创建 Wiki

由于 GitHub Wiki 需要通过网页界面首次初始化，请按以下步骤操作：

## 步骤 1: 访问项目 Wiki

1. 打开浏览器访问: https://github.com/zz8011/Recordian
2. 点击顶部导航栏的 "Wiki" 标签
3. 如果看到 "Create the first page"，点击它

## 步骤 2: 创建首页

1. 页面标题输入: `Home`
2. 将 `docs/wiki/Home.md` 的内容复制粘贴到编辑器
3. 点击 "Save Page"

## 步骤 3: 创建其他页面

按以下顺序创建页面：

### 入门文档
1. **Installation** - 复制 `docs/wiki/Installation.md`
2. **Quick-Start** - 复制 `docs/wiki/Quick-Start.md`
3. **FAQ** - 复制 `docs/wiki/FAQ.md`

### 项目文档
4. **Sprint-1-Stability** - 复制 `docs/wiki/Sprint-1-Stability.md`

## 步骤 4: 创建页面的方法

1. 在 Wiki 页面点击 "New Page"
2. 输入页面标题（如 `Installation`）
3. 复制对应 `.md` 文件的内容
4. 点击 "Save Page"

## 步骤 5: 验证链接

创建完所有页面后，回到 Home 页面，确认所有链接都能正常跳转。

## 已准备的文档

所有 Wiki 文档已保存在 `docs/wiki/` 目录：

```
docs/wiki/
├── Home.md                    # Wiki 首页
├── Installation.md            # 安装指南
├── Quick-Start.md             # 快速入门
├── FAQ.md                     # 常见问题
└── Sprint-1-Stability.md      # Sprint 1 记录
```

## 后续维护

Wiki 创建后，可以通过以下方式更新：

### 方法 1: 网页编辑
直接在 GitHub Wiki 页面点击 "Edit" 编辑

### 方法 2: Git 克隆
```bash
git clone https://github.com/zz8011/Recordian.wiki.git
cd Recordian.wiki
# 编辑文件
git add .
git commit -m "更新 Wiki"
git push
```

## 注意事项

1. Wiki 页面标题不要包含特殊字符
2. 内部链接使用页面标题（如 `[安装指南](Installation)`）
3. 图片需要上传到 Wiki 或使用外部链接
4. Markdown 语法与 GitHub README 相同

---

**创建日期**: 2026-03-01
