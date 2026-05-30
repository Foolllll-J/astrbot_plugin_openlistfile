import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import File, Image, Reply, Video
from astrbot.api.star import StarTools
from astrbot.core.utils.io import ensure_dir

from . import download_handler
from .client import OpenlistClient
from .config import UserConfigManager
from .download_service import normalize_download_headers
from .helpers import normalize_openlist_path
from .path_service import (
    get_item_full_path,
    resolve_path_candidates,
    resolve_target_path,
    strip_fixed_base_directory,
)
from .runtime_config import (
    LEGACY_ALLOWED_EXTENSIONS,
    build_transfer_config,
    format_extension_filter,
    get_bool_config,
    get_cache_duration_seconds,
    get_extension_filter,
    get_positive_int_config,
    get_size_limit_mb,
    is_extension_allowed,
)
from .upload_service import (
    extract_quoted_upload_components,
    get_upload_state_key,
    get_user_upload_state,
    is_regular_message_event,
    set_user_upload_waiting,
)


class PluginRuntimeMixin:
    def get_webui_config(self, key: str, default=None):
        """在 WebUI 分组配置中查找指定键的值。"""
        if not self.config or not isinstance(self.config, dict):
            return default
        for group_value in self.config.values():
            if isinstance(group_value, dict) and key in group_value:
                return group_value[key]
        return default

    def get_global_config(self) -> Dict:
        """合并 WebUI 和本地全局配置。"""
        config = self.global_config_manager.load_config()
        mapping = {
            "default_openlist_url": "openlist_url",
            "public_openlist_url": "public_openlist_url",
            "default_username": "username",
            "default_password": "password",
            "fixed_base_directory": "fixed_base_directory",
            "max_display_files": "max_display_files",
            "allowed_extensions": "allowed_extensions",
            "max_preview_size": "max_preview_size",
            "text_preview_length": "text_preview_length",
            "enable_cache": "enable_cache",
            "cache_duration": "cache_duration",
            "max_download_size": "max_download_size",
            "max_upload_size": "max_upload_size",
            "upload_retry_attempts": "upload_retry_attempts",
            "upload_retry_delay": "upload_retry_delay",
            "upload_chunk_size_mb": "upload_chunk_size_mb",
            "upload_progress_step_mb": "upload_progress_step_mb",
            "upstream_connect_timeout": "upstream_connect_timeout",
            "upstream_read_timeout": "upstream_read_timeout",
            "openlist_connect_timeout": "openlist_connect_timeout",
            "openlist_upload_response_timeout": "openlist_upload_response_timeout",
            "debug_transfer_logging": "debug_transfer_logging",
            "backup_default_path": "backup_default_path",
            "require_user_auth": "require_user_auth",
            "autobackup_groups": "autobackup_groups",
            "backup_allowed_extensions": "backup_allowed_extensions",
            "backup_max_size": "backup_max_size",
            "backup_skip_existing": "backup_skip_existing",
            "backup_retry_attempts": "backup_retry_attempts",
            "backup_retry_delay": "backup_retry_delay",
        }

        defaults = self.global_config_manager.default_config
        for webui_key, local_key in mapping.items():
            webui_val = self.get_webui_config(webui_key)
            if webui_val is None:
                continue

            if isinstance(webui_val, list) and local_key == "autobackup_groups":
                local_val = config.get(local_key, [])
                combined = list(local_val)
                existing_gids = {item.split(":", 1)[0] for item in local_val if ":" in item}
                existing_gids.update({item for item in local_val if ":" not in item})
                for item in webui_val:
                    gid = item.split(":", 1)[0] if ":" in item else item
                    if gid not in existing_gids:
                        combined.append(item)
                config[local_key] = combined
                continue

            current_val = config.get(local_key)
            default_val = defaults.get(local_key, defaults.get(webui_key))
            if current_val in (None, "") or current_val == default_val:
                config[local_key] = webui_val

        for legacy_key, local_key in {
            "default_openlist_url": "openlist_url",
            "default_username": "username",
            "default_password": "password",

        }.items():
            if not config.get(local_key) and config.get(legacy_key):
                config[local_key] = config[legacy_key]

        for key in ["allowed_extensions", "backup_allowed_extensions"]:
            if isinstance(config.get(key), str):
                values = [ext.strip().lower() for ext in config[key].split(",") if ext.strip()]
                config[key] = [ext if ext.startswith(".") else f".{ext}" for ext in values]

        return config

    def _get_size_limit_mb(self, user_config: Dict, key: str, default: int) -> int:
        """读取 MB 限制配置。"""
        return get_size_limit_mb(user_config, key, default, logger=logger)

    def _get_upload_mode_timeout_minutes(self, user_config: Dict) -> int:
        """上传等待模式超时时间。"""
        return 10

    def _get_cache_duration_seconds(self, user_config: Dict) -> int:
        """读取缓存有效期。"""
        return get_cache_duration_seconds(user_config, logger=logger)

    def _get_positive_int_config(
        self,
        user_config: Dict,
        key: str,
        default: int,
        minimum: int = 1,
    ) -> int:
        """读取正整数配置。"""
        return get_positive_int_config(user_config, key, default, minimum=minimum, logger=logger)

    def _get_bool_config(self, user_config: Dict, key: str, default: bool = False) -> bool:
        """读取布尔配置。"""
        return get_bool_config(user_config, key, default)

    def _get_transfer_config(self, user_config: Dict) -> Dict:
        """构建传输参数。"""
        return build_transfer_config(user_config, logger=logger)

    def _create_openlist_client(self, user_config: Dict) -> OpenlistClient:
        """基于当前配置创建 OpenList 客户端。"""
        return OpenlistClient(
            user_config["openlist_url"],
            user_config.get("public_openlist_url", ""),
            user_config.get("username", ""),
            user_config.get("password", ""),
            user_config.get("fixed_base_directory", ""),
            transfer_config=self._get_transfer_config(user_config),
        )

    def _get_retry_config(self, user_config: Dict, prefix: str) -> tuple:
        """读取重试次数和间隔。"""
        attempts = user_config.get(f"{prefix}_retry_attempts", 3)
        delay = user_config.get(f"{prefix}_retry_delay", 5)
        return (attempts, delay)

    async def _upload_file_with_retry(
        self,
        client: OpenlistClient,
        file_path: str,
        target_path: str,
        file_name: str,
        user_config: Dict,
    ) -> bool:
        """本地文件上传失败时重试。"""
        attempts, retry_delay = self._get_retry_config(user_config, "upload")
        debug_transfer_logging = self._get_bool_config(user_config, "debug_transfer_logging", False)
        for attempt in range(1, attempts + 1):
            if await client.upload_file(file_path, target_path, file_name):
                return True
            if debug_transfer_logging:
                logger.debug("上传失败: %s (%s/%s)", file_name, attempt, attempts)
            if attempt < attempts:
                await asyncio.sleep(retry_delay)
        return False

    async def _upload_url_stream_with_retry(
        self,
        client: OpenlistClient,
        source_url: str,
        target_path: str,
        file_name: str,
        file_size: Optional[int],
        user_config: Dict,
        refresh_url=None,
    ) -> bool:
        """URL 流式上传失败时重试，并支持刷新源链接。"""
        attempts, retry_delay = self._get_retry_config(user_config, "upload")
        debug_transfer_logging = self._get_bool_config(user_config, "debug_transfer_logging", False)
        current_url = source_url
        for attempt in range(1, attempts + 1):
            if attempt > 1 and callable(refresh_url):
                try:
                    refreshed_url = await refresh_url()
                    if refreshed_url:
                        current_url = refreshed_url
                except Exception as e:
                    if debug_transfer_logging:
                        logger.debug("刷新上传链接失败: %s (%s/%s): %s", file_name, attempt, attempts, e)

            if current_url and await client.upload_url_stream(
                current_url, target_path, file_name, file_size
            ):
                return True
            if debug_transfer_logging:
                logger.debug("URL 上传失败: %s (%s/%s)", file_name, attempt, attempts)
            if attempt < attempts:
                await asyncio.sleep(retry_delay)
        return False

    def _get_extension_filter(self, user_config: Dict, key: str = "allowed_extensions") -> List[str]:
        """读取扩展名过滤配置。"""
        return get_extension_filter(user_config, key)

    def _is_extension_allowed(
        self,
        filename: str,
        user_config: Dict,
        key: str = "allowed_extensions",
    ) -> bool:
        """判断文件扩展名是否允许。"""
        return is_extension_allowed(filename, user_config, key)

    def _format_extension_filter(self, user_config: Dict, key: str = "allowed_extensions") -> str:
        """格式化扩展名过滤信息。"""
        return format_extension_filter(user_config, key)

    def _is_admin_role(self, role) -> bool:
        """兼容 AstrBot 和平台事件中的管理员角色表示。"""
        if role is None:
            return False
        for attr in ("name", "value"):
            attr_value = getattr(role, attr, None)
            if attr_value is not None and attr_value is not role:
                if self._is_admin_role(attr_value):
                    return True
        if isinstance(role, str):
            role_text = role.strip().lower()
            if "." in role_text:
                role_text = role_text.rsplit(".", 1)[-1]
            if role_text in (
                "owner",
                "admin",
                "administrator",
                "superuser",
                "super_admin",
                "root",
                "群主",
                "管理员",
            ):
                return True
            if role_text in (
                "member",
                "normal",
                "user",
                "guest",
                "成员",
                "群员",
                "普通用户",
            ):
                return False
            try:
                return int(role_text) >= 2
            except ValueError:
                return False
        try:
            return int(role) >= 2
        except (TypeError, ValueError):
            return False

    def _read_value(self, obj, key: str, default=None):
        """从对象或映射中读取字段。"""
        if obj is None:
            return default
        if isinstance(obj, dict):
            return obj.get(key, default)
        value = getattr(obj, key, default)
        if value is not default:
            return value
        try:
            return obj[key]
        except Exception:
            return default

    def _extract_sender_role(self, event: AstrMessageEvent):
        """尽量从事件中提取发送者角色。"""
        candidates = []

        role = getattr(event, "role", None)
        if role is not None:
            candidates.append(role)

        message_obj = getattr(event, "message_obj", None)
        sender = self._read_value(message_obj, "sender")
        for key in ("role", "permission"):
            value = self._read_value(sender, key)
            if value is not None:
                candidates.append(value)

        raw_message = self._read_value(message_obj, "raw_message")
        raw_sender = self._read_value(raw_message, "sender")
        for key in ("role", "permission"):
            value = self._read_value(raw_sender, key)
            if value is not None:
                candidates.append(value)
        raw_role = self._read_value(raw_message, "role")
        if raw_role is not None:
            candidates.append(raw_role)

        for candidate in candidates:
            if candidate not in (None, ""):
                return candidate
        return None

    def _is_event_admin(self, event: AstrMessageEvent) -> bool:
        """判断当前事件发送者是否为管理员。"""
        is_admin = getattr(event, "is_admin", None)
        if callable(is_admin):
            try:
                if is_admin():
                    return True
            except Exception as e:
                logger.debug("调用 event.is_admin() 失败，改用角色字段判断: %s", e)
        return self._is_admin_role(self._extract_sender_role(event))

    async def initialize(self):
        """插件初始化时输出提示。"""
        logger.info("OpenList 助手已加载")
        global_cfg = self.get_global_config()
        default_url = global_cfg.get("openlist_url", "")
        require_auth = global_cfg.get("require_user_auth", True)
        if not default_url and not require_auth:
            logger.warning("OpenList URL 为空，请通过 /ol config 或 WebUI 进行配置")

    def get_user_config_manager(self, user_id: str) -> UserConfigManager:
        """获取用户配置管理器。"""
        if user_id not in self.user_config_managers:
            self.user_config_managers[user_id] = UserConfigManager("astrbot_plugin_openlistfile", user_id)
        return self.user_config_managers[user_id]

    def get_user_config(self, user_id: str) -> Dict:
        """获取用户最终生效的配置。"""
        global_cfg = self.get_global_config()
        if not global_cfg.get("require_user_auth", False):
            return global_cfg

        user_config = self.get_user_config_manager(user_id).load_config()
        final_cfg = global_cfg.copy()
        for key, value in user_config.items():
            default_val = self.get_user_config_manager(user_id).default_config.get(key)
            is_default_value = value == default_val
            if key == "allowed_extensions":
                if isinstance(value, str):
                    normalized_exts = [ext.strip().lower() for ext in value.split(",") if ext.strip()]
                elif isinstance(value, list):
                    normalized_exts = [str(ext).strip().lower() for ext in value if str(ext).strip()]
                else:
                    normalized_exts = []
                normalized_exts = [ext if ext.startswith(".") else f".{ext}" for ext in normalized_exts]
                if set(normalized_exts) == LEGACY_ALLOWED_EXTENSIONS:
                    is_default_value = True
            if not is_default_value:
                final_cfg[key] = value
        return final_cfg

    def _validate_config(self, user_config: Dict) -> bool:
        """判断当前配置是否可用。"""
        return bool(user_config.get("openlist_url"))

    def _get_user_navigation_state(self, user_id: str) -> Dict:
        """获取用户导航状态。"""
        if user_id not in self.user_navigation_state:
            self.user_navigation_state[user_id] = {
                "current_path": "/",
                "items": [],
                "parent_paths": [],
                "current_page": 1,
            }
        return self.user_navigation_state[user_id]

    def _update_user_navigation_state(self, user_id: str, path: str, items: List[Dict]):
        """更新用户导航状态。"""
        nav_state = self._get_user_navigation_state(user_id)
        if path != nav_state["current_path"]:
            if self._is_forward_navigation(nav_state["current_path"], path):
                nav_state["parent_paths"].append(nav_state["current_path"])
            nav_state["current_path"] = path
            nav_state["current_page"] = 1
        nav_state["items"] = items

    def _is_forward_navigation(self, current_path: str, new_path: str) -> bool:
        """判断是否为向下进入子目录。"""
        current = current_path.rstrip("/")
        new = new_path.rstrip("/")
        return new.startswith(current + "/") if current != "/" else new.startswith("/")

    def _get_item_by_number(self, user_id: str, number: int) -> Optional[Dict]:
        """根据序号获取当前列表项。"""
        nav_state = self._get_user_navigation_state(user_id)
        items = nav_state.get("items")
        if items and 1 <= number <= len(items):
            return items[number - 1]
        return None

    def _get_upload_state_key(self, event: AstrMessageEvent) -> str:
        """按会话生成上传状态键。"""
        user_id = event.get_sender_id()
        message_obj = getattr(event, "message_obj", None)
        group_id = getattr(message_obj, "group_id", None)
        return get_upload_state_key(user_id, group_id)

    def _get_backup_retry_key(self, event: AstrMessageEvent) -> str:
        """生成备份重试状态键。"""
        user_id = event.get_sender_id()
        message_obj = getattr(event, "message_obj", None)
        group_id = getattr(message_obj, "group_id", None)
        if group_id:
            return f"group:{group_id}:user:{user_id}"
        return f"private:user:{user_id}"

    def _get_backup_retry_file(self, retry_key: str) -> Path:
        """获取备份重试状态文件路径。"""
        safe_key = "".join(c if c.isalnum() or c in "._-" else "_" for c in retry_key)
        retry_dir = Path(StarTools.get_data_dir("astrbot_plugin_openlistfile")) / "backup_retry"
        ensure_dir(retry_dir)
        return retry_dir / f"{safe_key}.json"

    def _load_backup_retry_state(self, retry_key: str) -> Optional[Dict]:
        """读取备份重试状态。"""
        retry_file = self._get_backup_retry_file(retry_key)
        try:
            if not retry_file.exists():
                return None
            with open(retry_file, "r", encoding="utf-8") as file:
                state = json.load(file)
            return state if isinstance(state, dict) else None
        except Exception as e:
            logger.warning("读取备份重试状态失败: %s (%s)", retry_file, e)
            return None

    def _save_backup_retry_state(self, retry_key: str, state: Dict):
        """保存备份重试状态。"""
        retry_file = self._get_backup_retry_file(retry_key)
        try:
            with open(retry_file, "w", encoding="utf-8") as file:
                json.dump(state, file, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("保存备份重试状态失败: %s (%s)", retry_file, e)

    def _delete_backup_retry_state(self, retry_key: str):
        """删除备份重试状态。"""
        retry_file = self._get_backup_retry_file(retry_key)
        try:
            if retry_file.exists():
                retry_file.unlink()
        except OSError as e:
            logger.warning("删除备份重试状态失败: %s (%s)", retry_file, e)

    def _normalize_openlist_path(self, path: str) -> str:
        """标准化 OpenList 路径。"""
        return normalize_openlist_path(path)

    def _resolve_target_path(self, user_id: str, path: str, default_to_current: bool = True) -> str:
        """解析目标绝对路径。"""
        current_path = self._get_user_navigation_state(user_id)["current_path"]
        return resolve_target_path(current_path, path, default_to_current=default_to_current)

    def _resolve_path_candidates(
        self,
        user_id: str,
        path: str,
        default_to_current: bool = True,
    ) -> List[str]:
        """生成兼容旧用法的候选路径列表。"""
        current_path = self._get_user_navigation_state(user_id)["current_path"]
        return resolve_path_candidates(current_path, path, default_to_current=default_to_current)

    def _strip_fixed_base_directory(self, path: str, user_config: Dict) -> str:
        """去掉固定根目录前缀。"""
        return strip_fixed_base_directory(path, user_config.get("fixed_base_directory", ""))

    def _get_item_full_path(self, user_id: str, item: Dict, user_config: Dict) -> str:
        """推导列表项的完整路径。"""
        current_path = self._get_user_navigation_state(user_id).get("current_path", "/")
        return get_item_full_path(item, current_path, user_config.get("fixed_base_directory", ""))

    def _is_regular_message_event(self, event: AstrMessageEvent) -> bool:
        """过滤 notice、自发消息等非普通消息事件。"""
        message_obj = getattr(event, "message_obj", None)
        raw_event_data = getattr(message_obj, "raw_message", None)
        self_id = getattr(message_obj, "self_id", None)
        sender = getattr(message_obj, "sender", None)
        sender_id = getattr(sender, "user_id", None) if sender else None
        astr_message_type = getattr(message_obj, "type", None)
        type_name = (
            getattr(astr_message_type, "name", str(astr_message_type))
            if astr_message_type is not None
            else None
        )
        return is_regular_message_event(raw_event_data, self_id, sender_id, type_name)

    def _get_user_upload_state(self, state_key: str) -> Dict:
        """获取用户上传状态。"""
        return get_user_upload_state(self.user_upload_state, state_key)

    def _set_user_upload_waiting(self, state_key: str, waiting: bool, target_path: str = "/"):
        """设置用户上传等待状态。"""
        set_user_upload_waiting(self.user_upload_state, state_key, waiting, target_path)

    def _get_quoted_upload_components(self, messages: List) -> List:
        """从引用消息中提取可上传组件。"""
        return extract_quoted_upload_components(messages, Reply, (File, Image, Video))

    def _format_file_size(self, size: int) -> str:
        """格式化文件大小。"""
        if size < 1024:
            return f"{size}B"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f}KB"
        if size < 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024):.1f}MB"
        return f"{size / (1024 * 1024 * 1024):.1f}GB"

    def _sanitize_filename(self, filename: str, fallback: str = "file") -> str:
        """生成安全的临时文件名片段。"""
        safe_name = "".join(c for c in (filename or "") if c.isalnum() or c in "._- ").strip(" .")
        return safe_name[:100] or fallback

    def _unique_suffix(self) -> str:
        """生成临时文件名唯一后缀。"""
        return f"{time.time_ns()}_{uuid.uuid4().hex[:12]}"

    def _render_backup_path(self, path_template: str, group_id) -> str:
        """渲染备份路径模板。"""
        group_id = str(group_id)
        template = (path_template or "").strip() or f"/backup/group_{group_id}"
        rendered = template.replace("{group_id}", group_id)
        return self._normalize_openlist_path(rendered)

    def _get_autobackup_target_path(self, global_cfg: Dict, group_id: str) -> Optional[str]:
        """解析当前群的自动备份目标路径。"""
        group_id = str(group_id)
        default_path = global_cfg.get("backup_default_path", "/backup/group_{group_id}")
        for item in global_cfg.get("autobackup_groups", []):
            if not isinstance(item, str):
                continue
            item = item.strip()
            if not item:
                continue
            if ":" in item:
                gid, path = item.split(":", 1)
                gid = gid.strip()
                path = path.strip()
            else:
                gid = item
                path = ""
            if gid == group_id:
                return self._render_backup_path(path or default_path, group_id)
        return None

    async def _cleanup_temp_file(self, file_path: str, delay: int = 10):
        """延迟清理临时文件。"""
        await download_handler.cleanup_temp_file(self, file_path, delay)

    def _normalize_download_headers(self, headers: Dict) -> Dict[str, str]:
        """把下载头规范成 aiohttp 可用格式。"""
        return normalize_download_headers(headers)

    def _format_file_list(
        self,
        files: List[Dict],
        current_path: str,
        user_config: Dict,
        user_id: str = None,
    ) -> str:
        """格式化目录列表或搜索结果。"""
        search_prefix = "🔍 搜索"
        is_search_result = current_path.startswith(search_prefix)
        title = current_path if is_search_result else f"📁 {current_path}"

        if not files:
            return f"{title}\n\n❌ 列表为空"

        nav_state = self._get_user_navigation_state(user_id)
        current_page = nav_state.get("current_page", 1)
        max_files_per_page = user_config.get("max_display_files", 20)
        total_items = len(files)
        total_pages = (total_items + max_files_per_page - 1) // max_files_per_page
        start_index = (current_page - 1) * max_files_per_page
        end_index = start_index + max_files_per_page
        items_to_display = files[start_index:end_index]

        result = f"{title}\n\n"
        for index, item in enumerate(items_to_display, start=start_index + 1):
            name = item.get("name", "")
            size = item.get("size", 0)
            modified = item.get("modified", "")
            is_dir = item.get("is_dir", False)

            if is_dir:
                icon = "📂"
            else:
                ext = Path(name).suffix.lower()
                if ext in [".jpg", ".jpeg", ".png", ".gif", ".bmp"]:
                    icon = "🖼️"
                elif ext in [".mp4", ".avi", ".mkv", ".mov"]:
                    icon = "🎬"
                elif ext in [".mp3", ".wav", ".flac", ".aac"]:
                    icon = "🎵"
                elif ext == ".pdf":
                    icon = "📄"
                elif ext in [".doc", ".docx"]:
                    icon = "📝"
                elif ext in [".zip", ".rar", ".7z"]:
                    icon = "📦"
                else:
                    icon = "📄"

            result += f"{index:2d}. {icon} {name}{'/' if is_dir else ''}\n"

            extra_info = []
            if is_search_result:
                parent = item.get("parent", "")
                if parent:
                    parent = self._strip_fixed_base_directory(parent, user_config)
                    extra_info.append(f"📍 {parent}")
                if not is_dir or size > 0:
                    extra_info.append(f"💾 {self._format_file_size(size)}")
            else:
                if not is_dir or size > 0:
                    extra_info.append(f"💾 {self._format_file_size(size)}")
                modified_date_part = modified.split("T")[0] if modified else ""
                if modified_date_part:
                    extra_info.append(f"📅 {modified_date_part}")

            if extra_info:
                result += f"      {' | '.join(extra_info)}\n"

        result += f"\n📄 第 {current_page} / {total_pages} 页"
        if is_search_result:
            result += f" | 📊 总计: {total_items} 个结果"
        else:
            dirs_count = len([item for item in files if item.get("is_dir", False)])
            files_only_count = total_items - dirs_count
            result += f" | 📊 总计: {dirs_count} 个文件夹, {files_only_count} 个文件"

        result += "\n\n💡 快速导航:"
        result += "\n   • /ol ls 序号 - 进入目录/获取链接"
        result += "\n   • /ol dl 序号 - 下载并发送文件"
        if not is_search_result:
            result += "\n   • /ol quit - 返回上级目录"
        if total_pages > 1:
            result += "\n   • /ol prev - ⬅️ 上一页"
            result += "\n   • /ol next - ➡️ 下一页"
        return result
