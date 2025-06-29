# Openlist 文件管理插件

这是一个用于 AstrBot 的 Openlist 文件管理插件，让您可以通过聊天界面方便地管理 Openlist 服务器上的文件。

本插件源代码基于 [linjianyan0229/astrbot_plugin_alistfile](https://github.com/linjianyan0229/astrbot_plugin_alistfile) 进行二次开发，在此向原作者表示衷心感谢！

**请注意：** 本插件主要为自用目的修改，代码中预设了特定的文件路径（如 OpenList 下载文件的根目录）。若非自用，请根据您的实际Openlist服务器文件结构进行相应调整。

## ✨ 主要功能

* 📁 **智能导航** - 序号快速导航，支持一键进入目录或获取文件链接。
* ⬅️ **快速回退** - `/ol quit` 指令快速返回上级目录。
* 📥 **直接下载** - 对于路径操作，自动下载小文件并直接传送给用户。
* 🔗 **链接获取** - 对于序号操作，获取文件的直接下载链接。
* 📤 **文件上传** - 上传模式支持直接发送文件或图片上传到 Openlist。
* 🔍 **文件搜索** - 在指定目录下搜索文件。
* 📋 **文件信息** - 查看文件详细信息（大小、修改时间等）。
* ⚙️ **灵活设置** - 支持用户独立设置和全局设置两种模式。
* 🎨 **美化显示** - 智能文件图标，直观的信息展示。
* 🌐 **WebUI 支持** - 管理员可在 Dashboard 中设置插件全局选项。
* ⚠️⚠️**注意**：目前仅能稳定上传图片，因 astrbot 对文件处理的逻辑问题，直接发送文件上传可能不稳定。

## 🔧 设置方式

### 两种设置模式

#### 1. 用户独立设置模式（默认）

* 每个用户拥有独立的 Openlist 连接设置。
* 用户设置互不干扰，支持连接不同的 Openlist 服务器。
* 用户首次使用需要自行设置连接信息。

#### 2. 全局设置模式

* 所有用户共享同一个 Openlist 服务器连接。
* 管理员在 WebUI 中统一设置。
* 适合团队共享同一个文件服务器的场景。

### WebUI 设置（管理员）

1. 打开 AstrBot Dashboard
2. 进入“插件管理”页面
3. 找到“Openlist 文件管理插件”，点击“插件设置”按钮
4. 在设置页面中设置全局选项：
   * **默认 Openlist 服务器地址** - 用户设置的默认值（支持 http:// 或 https://）
   * **最大显示文件数** - 限制每次显示的文件数量（范围：1-100）
   * **允许的文件扩展名** - 控制显示的文件类型（用逗号分隔，如：.txt,.pdf,.jpg）
   * **启用文件预览** - 是否显示文件预览功能
   * **要求用户认证** - 切换用户独立设置/全局设置模式

> 注意：设置保存后会立即生效，影响所有用户的使用体验。

### 用户设置（聊天界面）

#### 快速设置向导

```
/ol config setup
```

#### 手动设置

**Bash**

```
# 显示当前设置
/ol config show

# 设置 Openlist 服务器地址
/ol config set openlist_url http://your-server:5244

# 设置用户名（可选）
/ol config set username your_username

# 设置密码（可选）
/ol config set password your_password

# 设置访问 Token（可选，优先级高于用户名密码）
/ol config set token your_token

# 测试连接
/ol config test

# 清理文件缓存
/ol config clear_cache
```

## 📖 使用指南

### 浏览与导航

**Bash**

```
# 查看帮助文档
/ol help

# 列出根目录文件 (自动分页并显示序号)
/ol ls /

# 使用序号进入子目录
/ol ls 1          # 如果1号是目录，则进入该目录

# 翻页
/ol page next     # 查看下一页
/ol page prev     # 查看上一页

# 返回上级目录
/ol quit

# 传统路径方式依然支持
/ol ls /movies    # 列出 /movies 目录的内容
```

### 文件搜索与信息

**Bash**

```
# 搜索文件 (注意：依赖服务器索引，结果可能非最新)
/ol search "年度报告"

# 在指定目录搜索
/ol search "年度报告" /documents

# 查看文件信息 (注意：必须使用完整路径，不支持序号)
/ol info /movies/Inception.mkv
```

### 下载功能

**Bash**

```
# 方式一：获取下载链接
/ol ls 2                      # 如果2号是文件，获取其下载链接
/ol ls /movies/Inception.mkv  # 获取指定路径文件的下载链接

# 方式二：直接下载文件
/ol download 2                      # 直接下载列表中的2号文件并作为附件发送
/ol download /movies/Inception.mkv  # 直接下载指定路径的文件
```

### 文件上传

**Bash**

```
# 开始上传模式
/ol upload

# 在上传模式下直接发送文件或图片即可上传
# （注意：文件上传可能不稳定，建议主要用于上传图片）

# 取消上传模式
/ol upload cancel

# 上传模式会在 10 分钟后自动取消
```

### 设置示例

#### 用户独立设置示例

**Bash**

```
# 用户 A 设置自己的家庭 NAS
/ol config set openlist_url http://home-nas:5244
/ol config set username userA
/ol config set password ****

# 用户 B 设置自己的云盘
/ol config set openlist_url http://cloud-drive:5244
/ol config set username userB
/ol config set password ****
```

#### 管理员全局设置示例

在 WebUI 中设置：

* 默认 Openlist 服务器地址：`http://company-files:5244`
* 要求用户认证：`false`（切换到全局模式）
* 最大显示文件数：`30`

## 🎨 功能特色

### 智能文件图标

* 🖼️ 图片文件（jpg, png, gif 等）
* 🎬 视频文件（mp4, avi, mkv 等）
* 🎵 音频文件（mp3, wav, flac 等）
* 📄 文档文件（pdf, doc 等）
* 📦 压缩文件（zip, rar 等）
* 📂 目录

### 信息显示

* 文件大小自动格式化（B/KB/MB/GB）
* 文件修改时间显示
* 分类显示（目录优先，文件在后）
* 超出限制时显示省略提示

### 安全特性

* 密码和 Token 自动隐藏
* 用户设置隔离
* 输入验证和错误处理
* 详细的日志记录

### 性能优化

* 智能文件列表缓存系统
* 可设置的缓存有效期
* 按用户独立缓存
* 支持手动清理缓存

## 🔧 高级设置

### 设置项详解


| **设置项**             | **类型** | **默认值**      | **说明**                 |
| ---------------------- | -------- | --------------- | ------------------------ |
| `default_openlist_url` | string   | ""              | 默认 Openlist 服务器地址 |
| `default_username`     | string   | ""              | 默认用户名               |
| `default_password`     | string   | ""              | 默认密码                 |
| `default_token`        | string   | ""              | 默认访问 Token           |
| `max_display_files`    | int      | 20              | 最大显示文件数量         |
| `allowed_extensions`   | text     | ".txt,.pdf,..." | 允许显示的扩展名         |
| `enable_preview`       | bool     | true            | 启用文件预览功能         |
| `enable_cache`         | bool     | true            | 启用文件列表缓存         |
| `cache_duration`       | int      | 300             | 缓存有效期(秒)           |
| `max_download_size`    | int      | 50              | 最大下载文件大小(MB)     |
| `max_upload_size`      | int      | 100             | 最大上传文件大小(MB)     |
| `require_user_auth`    | bool     | true            | 要求用户独立认证         |

### 文件存储结构

```
data/plugins_data/openlist/
├── global_config.json          # 全局设置文件
├── users/                      # 用户设置目录
│   ├── user1.json              # 用户 1 的设置
│   ├── user2.json              # 用户 2 的设置
│   └── ...
├── cache/                      # 文件列表缓存目录
│   ├── abc123.json             # 缓存文件 (MD5 命名)
│   └── ...
└── downloads/                  # 临时下载目录
    ├── user123_1234567890_file.txt # 临时下载文件
    └── ...
```

## 🚀 快速开始

### 用户首次使用

1. 运行设置向导：`/ol config setup`
2. 按提示设置 Openlist 服务器地址
3. 测试连接：`/ol config test`
4. 开始使用：`/ol ls /`

### 管理员部署

1. 在 WebUI 插件管理中安装插件
2. 进入插件设置页面
3. 根据需求选择设置模式：
   * **团队共享**：关闭“要求用户认证”，设置默认服务器
   * **用户独立**：保持“要求用户认证”开启，用户自行设置
4. 调整其他参数（文件显示数量、允许的扩展名等）

## 🛠️ 故障排除

### 常见问题

**Q: 提示“❌ 请先配置 Openlist 连接信息”**

A: 这是因为您处于“用户独立设置模式”。请运行 `/ol config setup` 设置向导，或使用 `/ol config set openlist_url <您的地址>` 进行手动设置。

**Q: 为什么 `search` 搜不到文件，但 `ls` 能看到？**

A: 这是因为 `search` 依赖服务器的**搜索索引**，而 `ls` 是实时列出文件。如果文件是新添加的，服务器索引可能尚未更新。请联系您的 Openlist 服务器管理员，在后台对相应存储**手动更新索引**。

**Q: 连接测试失败**

A: 请检查：1. 服务器地址是否正确（包含`http://`或`https://`）；2. 您的设备网络是否能访问到该地址；3. 用户名和密码是否正确。

### 设置验证

使用以下指令验证设置：

**Bash**

```
/ol config show    # 查看当前设置
/ol config test    # 测试连接
/ol ls /           # 测试文件列表
```

## 📋 依赖要求

* aiohttp >= 3.8.0
* AstrBot >= 3.5.0

## 🔄 版本历史

### v1.0.2

* ✨ **功能**: 新增 `/ol page` 分页浏览功能。
* ✨ **优化**: 明确 `ls` 和 `download` 指令的职责。`ls` 用于浏览和获取链接，`download` 用于直接下载文件。

### v1.0.1

* ✨ **优化**：对调了 `ls` 和 `download` 的核心逻辑。现在使用**序号**会获取下载链接，使用**路径**会直接下载文件。
* ✨ **优化**：将主指令从 `/openlist` 缩短为 `/ol`，操作更便捷。

### v1.0.0

* **初始版本**: 基于 `astrbot_plugin_alistfile` 进行二次开发。
* ✨ **功能**: 支持基本的文件浏览、搜索、信息查看功能。
* ✨ **功能**: 支持 Openlist 签名，若服务器端开启签名，插件获取的下载链接可**免登录直接下载**。启用签名可实现免登录下载文件。

## 📞 技术支持

如有问题或建议，请：

1. 查阅本文档的故障排除部分
2. 在 AstrBot 社区群聊中寻求帮助
3. 提交 Issue 到插件仓库

---

💡 **提示**：建议新用户首先使用设置向导（`/ol config setup`）来完成初始设置，这样可以避免大部分设置问题。
