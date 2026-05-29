import posixpath
from typing import Dict, Tuple

from astrbot.api import logger


VALID_CONFIG_KEYS = {
    "openlist_url",
    "username",
    "password",
    "max_display_files",
    "public_openlist_url",
    "fixed_base_directory",
    "allowed_extensions",
    "max_preview_size",
    "text_preview_length",
    "enable_cache",
    "cache_duration",
    "max_download_size",
    "max_upload_size",
    "upload_retry_attempts",
    "upload_retry_delay",
    "upload_chunk_size_mb",
    "upload_progress_step_mb",
    "upstream_connect_timeout",
    "upstream_read_timeout",
    "openlist_connect_timeout",
    "openlist_upload_response_timeout",
    "debug_transfer_logging",
    "debug_upload_logging",
    "backup_default_path",
    "backup_allowed_extensions",
    "backup_max_size",
    "backup_skip_existing",
    "backup_retry_attempts",
    "backup_retry_delay",
}

INT_CONFIG_KEYS = {
    "max_display_files",
    "cache_duration",
    "backup_max_size",
    "max_preview_size",
    "text_preview_length",
    "max_download_size",
    "max_upload_size",
    "upload_retry_attempts",
    "upload_retry_delay",
    "upload_chunk_size_mb",
    "upload_progress_step_mb",
    "upstream_connect_timeout",
    "upstream_read_timeout",
    "openlist_connect_timeout",
    "openlist_upload_response_timeout",
    "backup_retry_attempts",
    "backup_retry_delay",
}

BOOL_CONFIG_KEYS = {"enable_cache", "debug_transfer_logging", "backup_skip_existing"}
EXTENSION_LIST_KEYS = {"allowed_extensions", "backup_allowed_extensions"}
SENSITIVE_CONFIG_KEYS = {"password"}


def _build_setup_text() -> str:
    return """🛠️ Openlist 配置向导

请按以下步骤配置:

1️⃣ 设置 Openlist 服务器地址:
   /ol config set openlist_url http://your-server:5244

2️⃣ 设置用户名（可选）:
   /ol config set username your_username

3️⃣ 设置密码（可选）:
   /ol config set password your_password

4️⃣ 测试连接:
   /ol config test

5️⃣ 开始使用:
   /ol ls /

💡 如果服务器不需要登录，只设置 openlist_url 即可。"""


def build_help_text(is_user_auth_mode: bool, is_config_valid: bool) -> str:
    help_text = """📚 OpenList 助手帮助
💡 您也可以使用别名 `/网盘` 代替 `/ol`。

    核心导航指令
▶️ `/ol ls [路径|序号]`
- 浏览目录，文件过多时会自动分页
- 支持绝对路径、相对路径或当前列表序号
- `/ol ls 1` 可进入第 1 个目录，若第 1 项是文件则返回下载链接

▶️ `/ol next` / `/ol prev`
- 浏览上一页或下一页

▶️ `/ol quit`
- 返回上级目录

文件操作指令
`/ol dl <路径|序号>`
- 支持按路径或按当前列表序号直接下载文件并作为附件发送

`/ol search <关键词> [路径]`
- 搜索文件，结果依赖 OpenList 索引

`/ol info <路径|序号>`
- 支持按路径或按当前列表序号查看文件或目录详情

`/ol preview <路径|序号>`
- 支持按路径或按当前列表序号预览文本文件或压缩包目录

`/ol mkdir <名称|路径>`
- 在当前目录或指定路径创建文件夹

`/ol rm <路径|序号>`
- 支持按路径或按当前列表序号删除文件或目录，请谨慎操作

`/ol ul [路径|序号|cancel]`
- 开启上传模式，或取消当前上传模式
- 支持绝对路径、相对路径或当前列表中的目录序号作为上传目标
- 也支持引用文件/图片/视频消息后直接执行 `/ol ul`

`/ol backup [/目标路径] [@群号]`
- 备份群文件到 OpenList
- `/ol backup retry` 可重试上次失败项

`/ol autobackup <enable|disable> [@群号] [/路径]`
- 配置群文件自动备份

`/ol restore <路径> [@群号]`
- 将 OpenList 中的文件恢复到当前会话或指定群

插件配置指令
`/ol config setup`
`/ol config show`
`/ol config set <键> <值>`
`/ol config test`
`/ol config clear_cache`"""

    if is_user_auth_mode:
        help_text += "\n\n👤 当前模式: 用户独立认证\n- 每位用户都需要单独执行 `/ol config setup` 完成配置。"
        if not is_config_valid:
            help_text += "\n\n⚠️ 您当前尚未完成配置，请先执行 `/ol config setup`。"
    else:
        help_text += "\n\n🌐 当前模式: 全局共享\n- 所有用户共用管理员预设的 OpenList 服务配置。"

    help_text += (
        "\n\n💡 通用提示:\n"
        "1. 路径区分大小写，以 `/` 开头表示根目录。\n"
        "2. `ls` 主要用于浏览与取链接，`dl` 会直接下发文件。\n"
        "3. 管理员可在插件配置页调整全局设置。"
    )
    return help_text


def normalize_config_value(key: str, value: str) -> Tuple[object, str]:
    if key in INT_CONFIG_KEYS:
        try:
            parsed = int(value)
        except ValueError:
            return None, f"❌ {key} 必须是数字"

        if key == "max_display_files" and not 1 <= parsed <= 100:
            return None, "❌ max_display_files 必须在 1-100 之间"
        if key == "cache_duration" and parsed < 1:
            return None, "❌ cache_duration 必须大于 0"
        if key == "backup_max_size" and parsed < 0:
            return None, "❌ backup_max_size 必须大于等于 0"
        if key == "backup_retry_attempts" and parsed < 1:
            return None, "❌ backup_retry_attempts 必须大于 0"
        if key == "backup_retry_delay" and parsed < 0:
            return None, "❌ backup_retry_delay 必须大于等于 0"
        if key == "max_download_size" and parsed < 0:
            return None, "❌ max_download_size 必须大于等于 0"
        if key == "max_upload_size" and parsed < 0:
            return None, "❌ max_upload_size 必须大于等于 0"
        if key == "upload_retry_attempts" and parsed < 1:
            return None, "❌ upload_retry_attempts 必须大于 0"
        if key == "upload_retry_delay" and parsed < 0:
            return None, "❌ upload_retry_delay 必须大于等于 0"
        if key in {"upload_chunk_size_mb", "upload_progress_step_mb"} and parsed < 1:
            return None, f"❌ {key} 必须大于 0"
        if key in {
            "upstream_connect_timeout",
            "upstream_read_timeout",
            "openlist_connect_timeout",
            "openlist_upload_response_timeout",
        } and parsed < 1:
            return None, f"❌ {key} 必须大于 0"
        if key == "max_preview_size" and parsed < -1:
            return None, "❌ max_preview_size 必须大于等于 -1（-1 表示禁用，0 表示不限）"
        if key == "text_preview_length" and parsed < 1:
            return None, "❌ text_preview_length 必须大于 0"
        return parsed, ""

    if key in BOOL_CONFIG_KEYS:
        return value.lower() in {"true", "1", "yes", "on"}, ""

    if key in EXTENSION_LIST_KEYS:
        if value.strip().lower() in {"none", "null", "empty", "clear", "all", "*", "空", "不限", "不限制"}:
            return [], ""
        items = [ext.strip().lower() for ext in value.split(",") if ext.strip()]
        return [ext if ext.startswith(".") else f".{ext}" for ext in items], ""

    return value, ""


async def handle_config_command(plugin, event, action: str = "show", key: str = "", value: str = ""):
    user_id = event.get_sender_id()
    if action == "show":
        user_config = plugin.get_user_config(user_id)
        safe_config = user_config.copy()
        for sensitive_key in SENSITIVE_CONFIG_KEYS:
            if safe_config.get(sensitive_key):
                safe_config[sensitive_key] = "***"

        config_lines = [f"🔵 用户 {event.get_sender_name()} 的配置", ""]
        for config_key, config_value in safe_config.items():
            if config_key != "setup_completed":
                config_lines.append(f"🔼 {config_key}: {config_value}")

        global_cfg = plugin.get_global_config()
        require_auth = global_cfg.get("require_user_auth", True)
        default_url = global_cfg.get("openlist_url", "")
        if require_auth:
            config_lines.append("")
            config_lines.append("💡 提示: 当前启用了用户独立配置模式")
            if default_url:
                config_lines.append(f"🌐 默认服务器: {default_url}")
        else:
            config_lines.append("")
            config_lines.append("💡 提示: 当前使用全局共享配置模式")

        yield event.plain_result("\n".join(config_lines))
        return

    if action == "setup":
        yield event.plain_result(_build_setup_text())
        return

    if action == "set":
        if not key:
            yield event.plain_result("❌ 请指定配置项名称")
            return
        if not value:
            yield event.plain_result("❌ 请指定配置项值")
            return
        if key not in VALID_CONFIG_KEYS:
            yield event.plain_result(f"❌ 未知的配置项: {key}。可用项: {', '.join(sorted(VALID_CONFIG_KEYS))}")
            return

        user_manager = plugin.get_user_config_manager(user_id)
        user_config = user_manager.load_config()
        save_key = "debug_transfer_logging" if key == "debug_upload_logging" else key
        normalized_value, error_message = normalize_config_value(save_key, value)
        if error_message:
            yield event.plain_result(error_message)
            return

        user_config[save_key] = normalized_value
        if save_key == "openlist_url" and normalized_value:
            user_config["setup_completed"] = True
        user_manager.save_config(user_config)

        display_value = "***" if save_key in SENSITIVE_CONFIG_KEYS else str(normalized_value)
        yield event.plain_result(f"✅ 已为用户 {event.get_sender_name()} 设置 {save_key} = {display_value}")
        return

    if action == "test":
        user_config = plugin.get_user_config(user_id)
        if not plugin._validate_config(user_config):
            yield event.plain_result("❌ 请先配置 Openlist URL\n💡 使用 /ol config setup 开始配置向导")
            return
        try:
            async with plugin._create_openlist_client(user_config) as client:
                files = await client.list_files("/")
                if files is not None:
                    yield event.plain_result("✅ Openlist 连接测试成功！")
                else:
                    yield event.plain_result("❌ Openlist 连接失败，请检查配置")
        except Exception as e:
            logger.error(
                f"用户 {user_id} 连接测试失败: {e}, 服务器: {user_config.get('openlist_url')}",
                exc_info=True,
            )
            yield event.plain_result(f"❌ 连接测试失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")
        return

    if action == "clear_cache":
        plugin.cache_manager.clear_cache(user_id)
        yield event.plain_result("✅ 已清理您的文件列表缓存")
        return

    yield event.plain_result("❌ 未知操作，支持: show, set, test, setup, clear_cache")


async def handle_list_files(plugin, event, path: str = ""):
    user_id = event.get_sender_id()
    user_config = plugin.get_user_config(user_id)
    if not plugin._validate_config(user_config):
        yield event.plain_result("❌ 请先配置 Openlist 连接信息\n💡 使用 /ol config setup 开始配置向导")
        return

    path = (path or "").strip()
    target_path = plugin._resolve_target_path(user_id, path)
    path_candidates = [target_path]

    if path.isdigit():
        number = int(path)
        item = plugin._get_item_by_number(user_id, number)
        if not item:
            current_path = plugin._get_user_navigation_state(user_id)["current_path"]
            async with plugin._create_openlist_client(user_config) as client:
                result = await client.list_files(current_path)
                if result:
                    files = result.get("content", [])
                    plugin._update_user_navigation_state(user_id, current_path, files)
                    item = plugin._get_item_by_number(user_id, number)
            if not item:
                yield event.plain_result(f"❌ 序号 {number} 无效")
                return
        if item.get("is_dir", False):
            target_path = plugin._get_item_full_path(user_id, item, user_config)
            path_candidates = [target_path]
        else:
            async for result in plugin._get_and_send_download_link(event, item, user_config):
                yield result
            return
    else:
        path_candidates = plugin._resolve_path_candidates(user_id, path)

    try:
        cache_enabled = str(user_config.get("enable_cache", True)).lower() not in {"false", "0", "no", "off"}
        cache_duration = plugin._get_cache_duration_seconds(user_config)
        async with plugin._create_openlist_client(user_config) as client:
            for candidate_path in path_candidates:
                file_info = await client.get_file_info(candidate_path)
                if file_info and not file_info.get("is_dir", False):
                    async for result in plugin._get_and_send_download_link(
                        event, file_info, user_config, full_path=candidate_path
                    ):
                        yield result
                    return

                list_result = None
                if cache_enabled:
                    list_result = plugin.cache_manager.get_cache(
                        user_config["openlist_url"],
                        candidate_path,
                        user_id,
                        cache_duration,
                    )
                if list_result is None:
                    list_result = await client.list_files(candidate_path, per_page=0)
                    if list_result is not None and cache_enabled:
                        plugin.cache_manager.set_cache(
                            user_config["openlist_url"],
                            candidate_path,
                            user_id,
                            list_result,
                        )
                if list_result is not None:
                    files = list_result.get("content") or []
                    plugin._update_user_navigation_state(user_id, candidate_path, files)
                    yield event.plain_result(plugin._format_file_list(files, candidate_path, user_config, user_id))
                    return

            display_path = " / ".join(path_candidates)
            logger.warning(f"用户 {user_id} 无法访问路径候选: {display_path}")
            yield event.plain_result(f"❌ 无法访问路径: {display_path}")
    except Exception as e:
        logger.error(f"用户 {user_id} 列出文件失败: {e}, 路径候选: {path_candidates}", exc_info=True)
        yield event.plain_result(f"❌ 操作失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")


async def handle_next_page(plugin, event):
    user_id = event.get_sender_id()
    user_config = plugin.get_user_config(user_id)
    nav_state = plugin._get_user_navigation_state(user_id)
    if not nav_state.get("items"):
        yield event.plain_result("🤔 没有可供翻页的列表，请先使用 /ol ls 查看一个目录。")
        return

    current_page = nav_state.get("current_page", 1)
    all_items = nav_state.get("items", [])
    max_files_per_page = user_config.get("max_display_files", 20)
    total_pages = (len(all_items) + max_files_per_page - 1) // max_files_per_page
    if current_page >= total_pages:
        yield event.plain_result("➡️ 已经是最后一页了。")
        return

    nav_state["current_page"] += 1
    yield event.plain_result(plugin._format_file_list(all_items, nav_state["current_path"], user_config, user_id))


async def handle_prev_page(plugin, event):
    user_id = event.get_sender_id()
    user_config = plugin.get_user_config(user_id)
    nav_state = plugin._get_user_navigation_state(user_id)
    if not nav_state.get("items"):
        yield event.plain_result("🤔 没有可供翻页的列表，请先使用 /ol ls 查看一个目录。")
        return

    current_page = nav_state.get("current_page", 1)
    if current_page <= 1:
        yield event.plain_result("⬅️ 已经是第一页了。")
        return

    nav_state["current_page"] -= 1
    all_items = nav_state.get("items", [])
    yield event.plain_result(plugin._format_file_list(all_items, nav_state["current_path"], user_config, user_id))


async def handle_search_files(plugin, event, keyword: str, path: str = "/"):
    if not keyword:
        yield event.plain_result("❌ 请提供搜索关键词")
        return

    user_id = event.get_sender_id()
    target_path = plugin._resolve_target_path(user_id, path)
    user_config = plugin.get_user_config(user_id)
    if not plugin._validate_config(user_config):
        yield event.plain_result("❌ 请先配置 Openlist 连接信息\n💡 使用 /ol config setup 开始配置向导")
        return

    try:
        yield event.plain_result(f'🔍 正在搜索 "{keyword}"...')
        async with plugin._create_openlist_client(user_config) as client:
            files = await client.search_files(keyword, target_path)
            if not files:
                yield event.plain_result(f"🔍 未找到包含 '{keyword}' 的文件")
                return

            search_title = f'🔳 搜索 "{keyword}"'
            plugin._update_user_navigation_state(user_id, search_title, files)
            yield event.plain_result(plugin._format_file_list(files, search_title, user_config, user_id))
    except Exception as e:
        logger.error(f"用户 {user_id} 搜索文件失败: {e}, 关键词: {keyword}, 路径: {target_path}", exc_info=True)
        yield event.plain_result(f"❌ 搜索失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")


async def handle_quit_navigation(plugin, event):
    user_id = event.get_sender_id()
    user_config = plugin.get_user_config(user_id)
    if not plugin._validate_config(user_config):
        yield event.plain_result("❌ 请先配置 Openlist 连接信息\n💡 使用 /ol config setup 开始配置向导")
        return

    nav_state = plugin._get_user_navigation_state(user_id)
    if not nav_state["parent_paths"]:
        yield event.plain_result("📂 已经在根目录，无法继续回退。")
        return

    previous_path = nav_state["parent_paths"].pop()
    try:
        async with plugin._create_openlist_client(user_config) as client:
            result = await client.list_files(previous_path)
            if result is None:
                logger.warning(f"用户 {user_id} 无法访问上级目录: {previous_path}")
                yield event.plain_result(f"❌ 无法访问上级目录: {previous_path}")
                return

            files = result.get("content") or []
            nav_state["current_path"] = previous_path
            nav_state["items"] = files
            formatted_list = plugin._format_file_list(files, previous_path, user_config, user_id)
            yield event.plain_result(f"⬅️ 已返回上级目录\n\n{formatted_list}")
    except Exception as e:
        logger.error(f"用户 {user_id} 回退目录失败: {e}, 目标路径: {previous_path}", exc_info=True)
        yield event.plain_result(f"❌ 回退失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")


async def handle_remove_command(plugin, event, path: str):
    path = (path or "").strip()
    if not path:
        yield event.plain_result("❌ 请提供文件路径或序号")
        return

    user_id = event.get_sender_id()
    user_config = plugin.get_user_config(user_id)
    if not plugin._validate_config(user_config):
        yield event.plain_result("❌ 请先配置 Openlist 连接信息\n💡 使用 /ol config setup 开始配置向导")
        return

    if path.isdigit():
        number = int(path)
        item = plugin._get_item_by_number(user_id, number)
        if not item:
            yield event.plain_result(f"❌ 序号 {number} 无效。")
            return
        full_path = plugin._get_item_full_path(user_id, item, user_config)
    else:
        full_path = plugin._resolve_target_path(user_id, path)

    if full_path == "/":
        yield event.plain_result("❌ 不允许删除根目录。")
        return

    target_dir = posixpath.dirname(full_path) or "/"
    target_names = [posixpath.basename(full_path)]
    display_name = full_path

    try:
        async with plugin._create_openlist_client(user_config) as client:
            success = await client.remove(target_dir, target_names)
            if not success:
                yield event.plain_result("❌ 删除失败，请检查权限或路径是否正确")
                return

            yield event.plain_result(f"✅ 已删除: {display_name}")
            plugin.cache_manager.clear_cache(user_id)

            nav_state = plugin._get_user_navigation_state(user_id)
            current_path = nav_state["current_path"]
            deleted_full_paths = []
            for name in target_names:
                deleted_path = f"{target_dir.rstrip('/')}/{name}"
                if not deleted_path.startswith("/"):
                    deleted_path = "/" + deleted_path
                deleted_full_paths.append(deleted_path)

            is_current_path_deleted = any(
                current_path == deleted_path or current_path.startswith(deleted_path + "/")
                for deleted_path in deleted_full_paths
            )
            if is_current_path_deleted:
                result = await client.list_files("/")
                if result is not None:
                    files = result.get("content") or []
                    plugin.user_navigation_state[user_id] = {
                        "current_path": "/",
                        "items": files,
                        "parent_paths": [],
                        "current_page": 1,
                    }
                    yield event.plain_result("⚠️ 当前目录已被删除，已自动返回根目录。")
                return

            if target_dir == current_path:
                result = await client.list_files(current_path)
                if result is not None:
                    files = result.get("content") or []
                    plugin._update_user_navigation_state(user_id, current_path, files)
    except Exception as e:
        logger.error(f"用户 {user_id} 删除失败: {e}, 路径: {path}", exc_info=True)
        yield event.plain_result(f"❌ 删除失败: {str(e)}")


async def handle_mkdir_command(plugin, event, name: str):
    name = (name or "").strip()
    if not name:
        yield event.plain_result("❌ 请提供文件夹名称或路径")
        return

    user_id = event.get_sender_id()
    user_config = plugin.get_user_config(user_id)
    if not plugin._validate_config(user_config):
        yield event.plain_result("❌ 请先配置 Openlist 连接信息\n💡 使用 /ol config setup 开始配置向导")
        return

    full_path = plugin._resolve_target_path(user_id, name)
    if full_path == "/":
        yield event.plain_result("❌ 不允许创建根目录。")
        return

    try:
        async with plugin._create_openlist_client(user_config) as client:
            success = await client.mkdir(full_path)
            if not success:
                yield event.plain_result("❌ 创建文件夹失败")
                return

            yield event.plain_result(f"✅ 已创建文件夹: {name}")
            plugin.cache_manager.clear_cache(user_id)

            nav_state = plugin._get_user_navigation_state(user_id)
            current_path = plugin._normalize_openlist_path(nav_state["current_path"])
            parent_path = posixpath.dirname(full_path) or "/"
            if parent_path == current_path.rstrip("/") or (current_path == "/" and parent_path == "/"):
                result = await client.list_files(current_path)
                if result:
                    files = result.get("content") or []
                    plugin._update_user_navigation_state(user_id, current_path, files)
    except Exception as e:
        logger.error(f"用户 {user_id} 创建文件夹失败: {e}, 名称: {name}", exc_info=True)
        yield event.plain_result(f"❌ 创建失败: {str(e)}")


async def handle_help_command(plugin, event):
    user_id = event.get_sender_id()
    user_config = plugin.get_user_config(user_id)
    global_cfg = plugin.get_global_config()
    help_text = build_help_text(
        is_user_auth_mode=global_cfg.get("require_user_auth", True),
        is_config_valid=plugin._validate_config(user_config),
    )
    yield event.plain_result(help_text)
