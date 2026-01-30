<div align="center">

# <img src="https://raw.githubusercontent.com/OpenListTeam/Logo/main/logo.svg" width="32" height="32" style="vertical-align: middle;"> OpenList 助手

<i>🚀 跨越终端，触手可及的网盘管理专家</i>

![License](https://img.shields.io/badge/license-AGPL--3.0-green?style=flat-square)
![Python](https://img.shields.io/badge/python-3.10+-blue?style=flat-square&logo=python&logoColor=white)
![AstrBot](https://img.shields.io/badge/framework-AstrBot-ff6b6b?style=flat-square)

</div>

## ✨ 简介

一款为 [**AstrBot**](https://github.com/AstrBotDevs/AstrBot) 设计的 [**OpenList**](https://github.com/OpenListTeam/OpenList) 文件管理插件。它将强大的网盘管理功能带入聊天界面，让您可以像聊天一样轻松列出、搜索、下载和上传文件，支持智能导航、文件预览、群文件备份等多种高级特性。

---

## ✨ 功能特性

* 📁 **智能导航** - 序号快速导航，支持一键进入文件夹或获取文件。
* 📥 **直接下载** - 可下载文件并直接传送给用户。
* 🔗 **链接获取** - 可获取文件的直接下载链接。
* 📤 **文件上传** - 上传模式支持直接发送文件或图片上传。
* 🔍 **文件搜索** - 支持在指定目录中搜索目标文件。
* 📋 **文件信息** - 查看文件详细信息（大小、修改时间等）。
* 📦 **备份恢复** - 支持群文件备份，以及恢复文件到群。
* 👁️ **内容预览** - 支持文本文件预览和压缩包内容查看。
* ⚙️ **灵活设置** - 支全局设置和用户独立设置两种模式。
* 🎨 **美化显示** - 智能文件图标，直观的信息展示。

---

## 🔧 设置方式

### 🔌 两种设置模式

#### 1. 全局设置模式（默认）

* 所有用户共享同一个 OpenList 服务器连接。
* 管理员在 WebUI 中统一设置。
* 适合团队共享同一个文件服务器的场景。

#### 2. 用户独立设置模式

* 每个用户拥有独立的 OpenList 连接设置。
* 用户设置互不干扰，支持连接不同的 OpenList 服务器。
* **注意：此模式不保证后续维护，建议优先使用全局模式。**

### 💬 用户设置（聊天界面）

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

---

## 📖 使用指南

### 📝 指令列表

插件支持主指令 `/ol` 及其别名 `/网盘`。以下是常用指令及其对应的中文别名：

| 指令 | 中文别名 | 指令示例 | 说明 |
| :--- | :--- | :--- | :--- |
| `/ol ls` | `/网盘 列表`, `/网盘 直链` | `/ol ls /` | 列出文件/获取下载链接 |
| `/ol config` | `/网盘 配置` | `/ol config show` | 配置插件参数 |
| `/ol next` | `/网盘 下一页` | `/ol next` | 列表翻页（下一页） |
| `/ol prev` | `/网盘 上一页` | `/ol prev` | 列表翻页（上一页） |
| `/ol search` | `/网盘 搜索` | `/ol search "关键词"` | 搜索文件 |
| `/ol info` | `/网盘 信息` | `/ol info /path/file` | 查看文件/目录详细信息 |
| `/ol download` | `/网盘 下载` | `/ol download 1` | 直接下载文件并发送 |
| `/ol upload` | `/网盘 上传` | `/ol upload` | 开启/取消上传模式 |
| `/ol backup` | `/网盘 备份` | `/ol backup /path @群号` | 手动备份群文件 |
| `/ol autobackup` | `/网盘 自动备份` | `/ol autobackup enable` | 配置自动备份 |
| `/ol restore` | `/网盘 恢复` | `/ol restore /path @群号` | 从网盘恢复文件 |
| `/ol preview` | `/网盘 预览` | `/ol preview 1` | 预览文本或压缩包 |
| `/ol rm` | `/网盘 删除` | `/ol rm 1` | 删除文件或目录 |
| `/ol mkdir` | `/网盘 新建` | `/ol mkdir folder` | 创建新目录 |
| `/ol quit` | `/网盘 上一级`, `/网盘 返回` | `/ol quit` | 返回上级目录 |
| `/ol help` | `/网盘 帮助` | `/ol help` | 显示帮助信息 |

### 📂 浏览与导航

**Bash**

```
# 查看帮助文档
/ol help

# 列出根目录文件
/ol ls /

# 使用序号进入子目录
/ol ls 1          # 如果1号是目录，则进入该目录

# 翻页
/ol next     # 查看下一页
/ol prev     # 查看上一页

# 返回上级目录
/ol quit

# 路径方式
/ol ls /movies    # 列出 /movies 目录的内容
```

### 🔍 文件搜索与信息

**Bash**

```
# 搜索文件 (注意：依赖服务器索引，结果可能非最新)
/ol search "年度报告"

# 在指定目录搜索
/ol search "年度报告" /documents

# 查看文件信息 (注意：必须使用完整路径，不支持序号)
/ol info /movies/Inception.mkv

# 预览文件内容 (支持文本和压缩包)
/ol preview 2                     # 预览序号为2的文件
/ol preview /data/config.txt       # 预览指定路径文件

# 新建文件夹
/ol mkdir my_folder               # 在当前目录下创建
/ol mkdir /data/new_dir           # 在指定目录下创建

# 删除文件或文件夹 (谨慎操作)
/ol rm 3                          # 删除序号为3的项目
/ol rm /temp/old_file.txt         # 删除指定路径的项目
```

### 📥 下载与上传

**Bash**

```
# 方式一：获取下载链接
/ol ls 2                      # 如果2号是文件，获取其下载链接
/ol ls /movies/Inception.mkv  # 获取指定路径文件的下载链接

# 方式二：直接下载文件
/ol download 2                      # 直接下载列表中的2号文件并作为附件发送
/ol download /movies/Inception.mkv  # 直接下载指定路径的文件

# 开始上传模式
/ol upload

# 在上传模式下直接发送文件或图片即可上传

# 取消上传模式
/ol upload cancel
```

### 📦 备份与恢复

**Bash**

```
# 手动备份群文件到 OpenList
# 用法: /ol backup [@群号] [/目标路径]
/ol backup @123456789 /backup/group_files  # 备份指定群文件到指定目录
/ol backup /my_backup                      # 备份当前群文件到指定目录

# 自动备份设置
/ol autobackup enable @123456789 /backup   # 开启指定群的自动备份
/ol autobackup disable         # 关闭当前群的自动备份

# 从 OpenList 恢复文件到群
# 用法: /ol restore /来源路径 [@目标群号]
/ol restore /backup/important_file @123456789  # 恢复文件到指定群
/ol restore /backup/folder                     # 恢复整个目录到当前群
```

---

## 📜 项目说明

### ⚙️ 配置说明

首次加载后，请在 AstrBot 后台 -> 插件 页面找到本插件进行设置。所有配置项都有详细的说明和提示。

### 📂 文件存储结构

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

---

## 🛠️ 故障排除

### ❓ 常见问题

**Q: 提示“❌ 请先配置 OpenList 连接信息”**

A: 这是因为您处于“用户独立设置模式”。请运行 `/ol config setup` 设置向导，或使用 `/ol config set openlist_url <您的地址>` 进行手动设置。

**Q: 为什么 `search` 搜不到文件，但 `ls` 能看到？**

A: 这是因为 `search` 依赖服务器的**搜索索引**，而 `ls` 是实时列出文件。如果文件是新添加的，服务器索引可能尚未更新。请联系您的 OpenList 服务器管理员，在后台对相应存储**手动更新索引**。

**Q: 连接测试失败**

A: 请检查：

1. 服务器地址是否正确（包含`http://`或`https://`）；
2. 您的设备网络是否能访问到该地址；
3. 用户名和密码是否正确。

### ✅ 设置验证

使用以下指令验证设置：

**Bash**

```
/ol config show    # 查看当前设置
/ol config test    # 测试连接
/ol ls /           # 测试文件列表
```

---

## 🔄 版本历史

<details>
<summary>点击展开版本历史</summary>

### v1.2.2

* 🐛 **修复**:
  * 修复了最新框架版本下的兼容性问题。

### v1.2.1

* 🐛 **修复**:
  * 修复了手动备份群文件指令执行时的异常错误。

### v1.2.0

* ✨ **功能**:
  * 新增 `/ol backup` 群文件备份功能。
  * 新增 `/ol autobackup` 自动备份功能。
  * 新增 `/ol restore` 文件恢复功能。
  * 新增 `/ol preview` 文件预览功能。
  * 新增 `/ol rm` 删除文件功能。
  * 新增 `/ol mkdir` 创建目录功能。
* ⚡ **优化**:
  * 新增指令中文别名。

### v1.1.2

* ⚡ **优化**:
  * 简化了分页导航指令，将 `/ol page next` 和 `/ol page prev` 简化为 `/ol next` 和 `/ol prev`。

### v1.1.1

* ⚡ **优化**:
  * 新增了`公网地址`配置项，保证在默认使用内网地址时，能正确获取到可供外部访问的下载链接。

### v1.1.0

* ✨ **功能**:
  * 搜索结果支持分页翻页功能。
  * 支持显示文件夹大小。
* 🐛 **修复**:
  * 修复文件上传的严重错误。
  * 修复完整路径作为指令参数时下载和获取链接失败的问题。

### v1.0.2

* ✨ **功能**:
  * 新增 `/ol page` 分页浏览功能。
  * 明确 `ls` 和 `download` 指令的职责。`ls` 用于浏览和获取链接，`download` 用于直接下载文件。

### v1.0.1

* ⚡ **优化**:
  * 对调了 `ls` 和 `download` 的核心逻辑。现在使用**序号**会获取下载链接，使用**路径**会直接下载文件。
  * 将主指令从 `/openlist` 缩短为 `/ol`，操作更便捷。

### v1.0.0

* ✨ **功能**:
  * 基于 `astrbot_plugin_alistfile` 进行二次开发。
  * 支持基本的文件浏览、搜索、信息查看功能。
  * 支持 OpenList 签名，若服务器端开启签名，插件获取的下载链接可**免登录直接下载**。启用签名可实现免登录下载文件。

</details>

---

## 🙏 致谢

本插件源代码基于 [astrbot_plugin_alistfile](https://github.com/linjianyan0229/astrbot_plugin_alistfile) 进行二次开发，在此向原作者表示衷心感谢！

---

## ❤️ 支持

* [AstrBot 帮助文档](https://astrbot.app)
* 如果您在使用中遇到问题，欢迎在本仓库提交 [Issue](https://github.com/Foolllll-J/astrbot_plugin_openlistfile/issues)。

---

<div align="center">

**如果本插件对你有帮助，欢迎点个 ⭐ Star 支持一下！**

</div>

