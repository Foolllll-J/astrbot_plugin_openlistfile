import asyncio
import os
import posixpath
import time
import uuid
import chardet
from typing import List, Dict, Optional
import aiohttp

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api.message_components import Image, File
from astrbot.api import logger
from .lib.client import OpenlistClient
from .lib.config import UserConfigManager, GlobalConfigManager
from .lib.cache import CacheManager


class OpenlistPlugin(Star):
    LEGACY_ALLOWED_EXTENSIONS = {
        ".txt", ".pdf", ".doc", ".docx", ".zip", ".rar", ".jpg", ".png", ".gif", ".mp4", ".mp3"
    }

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.user_config_managers = {}
        self.config = config
        self.global_config_manager = GlobalConfigManager("openlist")
        self.global_config = self.global_config_manager.load_config()
        self.cache_manager = CacheManager("openlist")
        self.user_navigation_state = {}
        self.user_upload_state = {}
        self.autobackup_semaphore = asyncio.Semaphore(2)

    def get_webui_config(self, key: str, default=None):
        """获取WebUI配置项"""
        if self.config:
            return self.config.get("global_settings", {}).get(key, default)
        return default

    def get_global_config(self) -> Dict:
        """获取整合后的全局配置（WebUI + global_config.json）"""
        # 直接加载本地配置
        config = self.global_config_manager.load_config()
        
        # 基础配置项映射：如果 WebUI 有值且本地是默认值，则使用 WebUI 的
        mapping = {
            "default_openlist_url": "openlist_url",
            "public_openlist_url": "public_openlist_url",
            "default_username": "username",
            "default_password": "password",
            "default_token": "token",
            "fixed_base_directory": "fixed_base_directory",
            "max_display_files": "max_display_files",
            "allowed_extensions": "allowed_extensions",
            "max_preview_size": "max_preview_size",
            "text_preview_length": "text_preview_length",
            "enable_cache": "enable_cache",
            "cache_duration": "cache_duration",
            "max_download_size": "max_download_size",
            "max_upload_size": "max_upload_size",
            "upload_mode_timeout": "upload_mode_timeout",
            "upload_chunk_size_mb": "upload_chunk_size_mb",
            "upload_progress_step_mb": "upload_progress_step_mb",
            "upstream_connect_timeout": "upstream_connect_timeout",
            "upstream_read_timeout": "upstream_read_timeout",
            "openlist_connect_timeout": "openlist_connect_timeout",
            "openlist_upload_response_timeout": "openlist_upload_response_timeout",
            "debug_transfer_logging": "debug_transfer_logging",
            "backup_default_path": "backup_default_path",
            "autobackup_default_path": "autobackup_default_path",
            "require_user_auth": "require_user_auth",
            "autobackup_groups": "autobackup_groups",
            "backup_allowed_extensions": "backup_allowed_extensions",
            "backup_max_size": "backup_max_size",
        }
        
        defaults = self.global_config_manager.default_config
        for webui_key, local_key in mapping.items():
            webui_val = self.get_webui_config(webui_key)
            if webui_val is not None:
                # 如果是列表（autobackup_groups），合并
                if isinstance(webui_val, list) and local_key == "autobackup_groups":
                    local_val = config.get(local_key, [])
                    # 简单的去重合并
                    combined = list(local_val)
                    existing_gids = {item.split(":", 1)[0] for item in local_val if ":" in item}
                    existing_gids.update({item for item in local_val if ":" not in item})
                    for item in webui_val:
                        gid = item.split(":", 1)[0] if ":" in item else item
                        if gid not in existing_gids:
                            combined.append(item)
                    config[local_key] = combined
                # 其他项，只有当本地配置为空或仍为默认值时才使用 WebUI
                else:
                    current_val = config.get(local_key)
                    default_val = defaults.get(local_key, defaults.get(webui_key))
                    if current_val in (None, "") or current_val == default_val:
                        config[local_key] = webui_val

        # 兼容旧版 global_config.json 中的 default_* 字段
        for legacy_key, local_key in {
            "default_openlist_url": "openlist_url",
            "default_username": "username",
            "default_password": "password",
            "default_token": "token",
        }.items():
            if not config.get(local_key) and config.get(legacy_key):
                config[local_key] = config[legacy_key]

        # 统一将扩展名字符串转为列表
        for key in ["allowed_extensions", "backup_allowed_extensions"]:
            if isinstance(config.get(key), str):
                config[key] = [ext.strip().lower() for ext in config[key].split(",") if ext.strip()]
                config[key] = [ext if ext.startswith(".") else f".{ext}" for ext in config[key]]
                
        return config

    def _get_size_limit_mb(self, user_config: Dict, key: str, default: int) -> int:
        """读取大小限制配置；0 表示不限制。"""
        try:
            value = int(user_config.get(key, default))
        except (TypeError, ValueError):
            logger.warning(f"配置 {key} 的值无效: {user_config.get(key)!r}，已使用默认值 {default}MB")
            return default
        if value < 0:
            logger.warning(f"配置 {key} 的值不能为负数: {value}，已使用默认值 {default}MB")
            return default
        return value

    def _get_upload_mode_timeout_minutes(self, user_config: Dict) -> int:
        """读取上传模式持续时间，单位分钟。"""
        try:
            timeout = int(user_config.get("upload_mode_timeout", 10))
        except (TypeError, ValueError):
            logger.warning(f"配置 upload_mode_timeout 的值无效: {user_config.get('upload_mode_timeout')!r}，已使用默认值 10 分钟")
            return 10
        if timeout < 1:
            logger.warning(f"配置 upload_mode_timeout 的值过小: {timeout}，已使用默认值 10 分钟")
            return 10
        return timeout

    def _get_cache_duration_seconds(self, user_config: Dict) -> int:
        """读取缓存有效期，单位秒。"""
        try:
            duration = int(user_config.get("cache_duration", 300))
        except (TypeError, ValueError):
            logger.warning(f"配置 cache_duration 的值无效: {user_config.get('cache_duration')!r}，已使用默认值 300 秒")
            return 300
        if duration < 1:
            logger.warning(f"配置 cache_duration 的值过小: {duration}，已使用默认值 300 秒")
            return 300
        return duration

    def _get_positive_int_config(self, user_config: Dict, key: str, default: int, minimum: int = 1) -> int:
        """读取正整数配置。"""
        try:
            value = int(user_config.get(key, default))
        except (TypeError, ValueError):
            logger.warning(f"配置 {key} 的值无效: {user_config.get(key)!r}，已使用默认值 {default}")
            return default
        if value < minimum:
            logger.warning(f"配置 {key} 的值过小: {value}，已使用默认值 {default}")
            return default
        return value

    def _get_bool_config(self, user_config: Dict, key: str, default: bool = False) -> bool:
        """读取布尔配置。"""
        value = user_config.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")
        return bool(value)

    def _get_transfer_config(self, user_config: Dict) -> Dict:
        """读取上传/中转传输调优配置。"""
        mb = 1024 * 1024
        return {
            "upload_chunk_size": self._get_positive_int_config(user_config, "upload_chunk_size_mb", 4) * mb,
            "upload_progress_step": self._get_positive_int_config(user_config, "upload_progress_step_mb", 64) * mb,
            "upstream_connect_timeout": self._get_positive_int_config(user_config, "upstream_connect_timeout", 60),
            "upstream_read_timeout": self._get_positive_int_config(user_config, "upstream_read_timeout", 180),
            "openlist_connect_timeout": self._get_positive_int_config(user_config, "openlist_connect_timeout", 30),
            "openlist_upload_response_timeout": self._get_positive_int_config(user_config, "openlist_upload_response_timeout", 3000),
            "debug_transfer_logging": self._get_bool_config(user_config, "debug_transfer_logging", False),
        }

    def _create_openlist_client(self, user_config: Dict) -> OpenlistClient:
        """基于用户配置创建 OpenList 客户端。"""
        return OpenlistClient(
            user_config["openlist_url"],
            user_config.get("public_openlist_url", ""),
            user_config.get("username", ""),
            user_config.get("password", ""),
            user_config.get("token", ""),
            user_config.get("fixed_base_directory", ""),
            transfer_config=self._get_transfer_config(user_config),
        )

    def _get_extension_filter(self, user_config: Dict, key: str = "allowed_extensions") -> List[str]:
        """读取扩展名过滤配置；空列表表示不限制。"""
        value = user_config.get(key, [])
        if isinstance(value, str):
            extensions = [ext.strip().lower() for ext in value.split(",") if ext.strip()]
        elif isinstance(value, list):
            extensions = [str(ext).strip().lower() for ext in value if str(ext).strip()]
        else:
            return []
        extensions = [ext if ext.startswith(".") else f".{ext}" for ext in extensions]
        if key == "allowed_extensions" and set(extensions) == self.LEGACY_ALLOWED_EXTENSIONS:
            return []
        return extensions

    def _is_extension_allowed(self, filename: str, user_config: Dict, key: str = "allowed_extensions") -> bool:
        """判断文件扩展名是否通过配置过滤。"""
        allowed_exts = self._get_extension_filter(user_config, key)
        if not allowed_exts:
            return True
        return os.path.splitext((filename or "").lower())[1] in allowed_exts

    def _format_extension_filter(self, user_config: Dict, key: str = "allowed_extensions") -> str:
        allowed_exts = self._get_extension_filter(user_config, key)
        return ", ".join(allowed_exts) if allowed_exts else "不限制"

    def _is_admin_role(self, role) -> bool:
        """兼容 AstrBot/适配器可能返回的数字或字符串群角色。"""
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
            if role_text in ("owner", "admin", "administrator", "superuser", "super_admin", "root", "群主", "管理员"):
                return True
            if role_text in ("member", "normal", "user", "guest", "成员", "群员", "普通用户"):
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
        """从对象或映射中读取字段，兼容适配器原始事件对象。"""
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
        """尽量从 AstrBot 事件和平台原始事件中提取发送者群角色。"""
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
        """判断事件发送者是否为管理员，优先使用 AstrBot 能力，再回退到平台原始角色。"""
        is_admin = getattr(event, "is_admin", None)
        if callable(is_admin):
            try:
                if is_admin():
                    return True
            except Exception as e:
                logger.debug(f"调用 event.is_admin() 失败，继续使用角色字段判断: {e}")

        return self._is_admin_role(self._extract_sender_role(event))

    async def initialize(self):
        """插件初始化"""
        logger.info("Openlist文件管理插件已加载")
        global_cfg = self.get_global_config()
        default_url = global_cfg.get("openlist_url", "")
        require_auth = global_cfg.get("require_user_auth", True)
        if not default_url and not require_auth:
            logger.warning("Openlist URL未配置，请使用 /ol config 命令配置或在WebUI中配置")

    def get_user_config_manager(self, user_id: str) -> UserConfigManager:
        """获取用户配置管理器"""
        if user_id not in self.user_config_managers:
            self.user_config_managers[user_id] = UserConfigManager("openlist", user_id)
        return self.user_config_managers[user_id]

    def get_user_config(self, user_id: str) -> Dict:
        """获取用户配置"""
        global_cfg = self.get_global_config()
        if not global_cfg.get("require_user_auth", True):
            return global_cfg
            
        user_config = self.get_user_config_manager(user_id).load_config()
        
        # 简单的合并：用户配置优先，如果用户配置为空则使用全局配置
        final_cfg = global_cfg.copy()
        for k, v in user_config.items():
            default_val = self.get_user_config_manager(user_id).default_config.get(k)
            is_default_value = v == default_val
            if k == "allowed_extensions":
                if isinstance(v, str):
                    normalized_exts = [ext.strip().lower() for ext in v.split(",") if ext.strip()]
                elif isinstance(v, list):
                    normalized_exts = [str(ext).strip().lower() for ext in v if str(ext).strip()]
                else:
                    normalized_exts = []
                normalized_exts = [ext if ext.startswith(".") else f".{ext}" for ext in normalized_exts]
                if set(normalized_exts) == self.LEGACY_ALLOWED_EXTENSIONS:
                    is_default_value = True
            # 只要用户设置了非默认值，就覆盖全局；允许 0/False/[] 这类有效配置值。
            if not is_default_value:
                final_cfg[k] = v
                
        return final_cfg

    def _validate_config(self, user_config: Dict) -> bool:
        """验证配置是否有效"""
        return bool(user_config.get("openlist_url"))

    def _get_user_navigation_state(self, user_id: str) -> Dict:
        """获取用户导航状态"""
        if user_id not in self.user_navigation_state:
            self.user_navigation_state[user_id] = {
                "current_path": "/",
                "items": [],
                "parent_paths": [],
                "current_page": 1,
            }
        return self.user_navigation_state[user_id]

    def _update_user_navigation_state(self, user_id: str, path: str, items: List[Dict]):
        """更新用户导航状态"""
        nav_state = self._get_user_navigation_state(user_id)
        if path != nav_state["current_path"]:
            if self._is_forward_navigation(nav_state["current_path"], path):
                nav_state["parent_paths"].append(nav_state["current_path"])
            nav_state["current_path"] = path
            nav_state["current_page"] = 1
        nav_state["items"] = items

    def _is_forward_navigation(self, current_path: str, new_path: str) -> bool:
        """判断是否是前进导航"""
        current = current_path.rstrip("/")
        new = new_path.rstrip("/")
        return new.startswith(current + "/") if current != "/" else new.startswith("/")

    def _get_item_by_number(self, user_id: str, number: int) -> Optional[Dict]:
        """根据序号获取文件或目录项"""
        nav_state = self._get_user_navigation_state(user_id)
        items = nav_state.get("items")
        if items and 1 <= number <= len(items):
            return items[number - 1]
        return None

    def _get_upload_state_key(self, event: AstrMessageEvent) -> str:
        """按会话隔离上传模式，避免同一用户在不同群聊间串状态。"""
        user_id = event.get_sender_id()
        message_obj = getattr(event, "message_obj", None)
        group_id = getattr(message_obj, "group_id", None)
        if group_id:
            return f"group:{group_id}:user:{user_id}"
        return f"private:user:{user_id}"

    def _normalize_openlist_path(self, path: str) -> str:
        """标准化 OpenList 路径，统一为以 / 开头的绝对路径。"""
        normalized = (path or "").strip().replace("\\", "/")
        if not normalized:
            return "/"
        if not normalized.startswith("/"):
            normalized = "/" + normalized
        while "//" in normalized:
            normalized = normalized.replace("//", "/")
        normalized = posixpath.normpath(normalized)
        if normalized in ("", "."):
            return "/"
        if not normalized.startswith("/"):
            normalized = "/" + normalized
        return normalized

    def _resolve_target_path(self, user_id: str, path: str, default_to_current: bool = True) -> str:
        """将目标路径解析为 OpenList 绝对路径，支持当前目录相对路径。"""
        raw_path = (path or "").strip()
        current_path = self._get_user_navigation_state(user_id)["current_path"]
        if not isinstance(current_path, str) or not current_path.startswith("/"):
            current_path = "/"

        if not raw_path:
            if default_to_current:
                return self._normalize_openlist_path(current_path)
            return "/"

        if raw_path.startswith("/"):
            return self._normalize_openlist_path(raw_path)

        current_path = self._normalize_openlist_path(current_path)
        return self._normalize_openlist_path(f"{current_path.rstrip('/')}/{raw_path}")

    def _resolve_path_candidates(self, user_id: str, path: str, default_to_current: bool = True) -> List[str]:
        """生成候选路径: 先当前目录相对路径，再尝试根目录路径（用于兼容旧用法）。"""
        raw_path = (path or "").strip()
        primary_path = self._resolve_target_path(user_id, raw_path, default_to_current=default_to_current)
        candidates = [primary_path]
        if raw_path and not raw_path.startswith("/"):
            root_path = self._normalize_openlist_path(raw_path)
            if root_path not in candidates:
                candidates.append(root_path)
        return candidates

    def _strip_fixed_base_directory(self, path: str, user_config: Dict) -> str:
        """从 OpenList 返回路径中剥离下载链接前缀，得到用户视角路径。"""
        path = self._normalize_openlist_path(path)
        fixed_base_dir = self._normalize_openlist_path(user_config.get("fixed_base_directory", ""))
        if fixed_base_dir != "/" and (path == fixed_base_dir or path.startswith(fixed_base_dir + "/")):
            path = path[len(fixed_base_dir):]
            if not path:
                return "/"
            if not path.startswith("/"):
                path = "/" + path
        return self._normalize_openlist_path(path)

    def _get_item_full_path(self, user_id: str, item: Dict, user_config: Dict) -> str:
        """根据列表项生成 OpenList 绝对路径，兼容普通列表和搜索结果。"""
        item_name = item.get("name", "")
        parent_path = item.get("parent")
        if parent_path:
            parent_path = self._strip_fixed_base_directory(parent_path, user_config)
            return self._normalize_openlist_path(f"{parent_path.rstrip('/')}/{item_name}")

        current_path = self._get_user_navigation_state(user_id).get("current_path", "/")
        if not isinstance(current_path, str) or not current_path.startswith("/"):
            current_path = "/"
        return self._normalize_openlist_path(f"{current_path.rstrip('/')}/{item_name}")

    def _is_regular_message_event(self, event: AstrMessageEvent) -> bool:
        """过滤 notice、回调等非普通消息事件，避免上传模式误响应。"""
        message_obj = getattr(event, "message_obj", None)
        raw_event_data = getattr(message_obj, "raw_message", None)
        if isinstance(raw_event_data, dict):
            post_type = raw_event_data.get("post_type")
            if post_type is not None and post_type != "message":
                return False
            message_type = raw_event_data.get("message_type")
            if message_type is not None and message_type not in ("group", "private"):
                return False
            self_id = raw_event_data.get("self_id")
            sender_id = raw_event_data.get("user_id")
            if self_id is not None and sender_id is not None and str(self_id) == str(sender_id):
                return False

        self_id = getattr(message_obj, "self_id", None)
        sender = getattr(message_obj, "sender", None)
        sender_id = getattr(sender, "user_id", None) if sender else None
        if self_id is not None and sender_id is not None and str(self_id) == str(sender_id):
            return False

        astr_message_type = getattr(message_obj, "type", None)
        if astr_message_type is None:
            return True
        type_name = getattr(astr_message_type, "name", str(astr_message_type))
        return type_name in ("GROUP_MESSAGE", "PRIVATE_MESSAGE") or str(astr_message_type).endswith((".GROUP_MESSAGE", ".PRIVATE_MESSAGE"))

    def _get_user_upload_state(self, state_key: str) -> Dict:
        """获取用户上传状态"""
        if state_key not in self.user_upload_state:
            self.user_upload_state[state_key] = {"waiting": False, "target_path": "/"}
        return self.user_upload_state[state_key]

    def _set_user_upload_waiting(self, state_key: str, waiting: bool, target_path: str = "/"):
        """设置用户上传等待状态"""
        upload_state = self._get_user_upload_state(state_key)
        upload_state["waiting"] = waiting
        upload_state["target_path"] = target_path

    def _format_file_size(self, size: int) -> str:
        """格式化文件大小"""
        if size < 1024: return f"{size}B"
        elif size < 1024 * 1024: return f"{size / 1024:.1f}KB"
        elif size < 1024 * 1024 * 1024: return f"{size / (1024 * 1024):.1f}MB"
        else: return f"{size / (1024 * 1024 * 1024):.1f}GB"

    def _sanitize_filename(self, filename: str, fallback: str = "file") -> str:
        """生成可用于临时附件名的文件名片段。"""
        safe_name = "".join(c for c in (filename or "") if c.isalnum() or c in "._- ").strip(" .")
        return (safe_name[:100] or fallback)

    def _unique_suffix(self) -> str:
        """生成临时文件名后缀，避免同一秒内并发请求撞名。"""
        return f"{time.time_ns()}_{uuid.uuid4().hex[:12]}"

    def _render_backup_path(self, path_template: str, group_id) -> str:
        """渲染备份目录模板，支持 {group_id}、{gid}、{group} 占位符。"""
        group_id = str(group_id)
        template = (path_template or "").strip() or f"/backup/group_{group_id}"
        rendered = (
            template
            .replace("{group_id}", group_id)
            .replace("{gid}", group_id)
            .replace("{group}", group_id)
        )
        return self._normalize_openlist_path(rendered)

    def _get_autobackup_target_path(self, global_cfg: Dict, group_id: str) -> Optional[str]:
        """从自动备份群配置中解析目标路径。"""
        group_id = str(group_id)
        default_path = global_cfg.get("autobackup_default_path", "/backup/group_{group_id}")
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
        """延迟清理已发送的临时文件。"""
        await asyncio.sleep(delay)
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except OSError as e:
            logger.debug(f"清理临时文件失败: {file_path}, err={e}")

    def _normalize_download_headers(self, headers: Dict) -> Dict[str, str]:
        """将 OpenList link.header 转为 aiohttp 可用的单值请求头。"""
        normalized = {}
        if not isinstance(headers, dict):
            return normalized
        for key, value in headers.items():
            if value is None:
                continue
            if isinstance(value, list):
                values = [str(v) for v in value if v is not None]
                if not values:
                    continue
                normalized[key] = "; ".join(values) if key.lower() == "cookie" else ",".join(values)
            else:
                normalized[key] = str(value)
        return normalized

    async def _send_download_link_txt(
        self,
        event: AstrMessageEvent,
        file_name: str,
        file_size: int,
        file_path: str,
        download_url: str,
    ):
        """将下载链接写入 txt 附件发送，避免长文本被平台转为图片。"""
        links_dir = os.path.join(StarTools.get_data_dir("openlist"), "links")
        os.makedirs(links_dir, exist_ok=True)
        safe_base = self._sanitize_filename(file_name, "download")
        attachment_name = f"{safe_base}_download_link.txt"
        temp_file_path = os.path.join(
            links_dir,
            f"{event.get_sender_id()}_{self._unique_suffix()}_{attachment_name}",
        )
        content = (
            "OpenList 下载链接\n\n"
            f"文件: {file_name}\n"
            f"路径: {file_path}\n"
            f"大小: {self._format_file_size(file_size)}\n"
            f"链接: {download_url}\n"
        )
        with open(temp_file_path, "w", encoding="utf-8") as f:
            f.write(content)

        yield event.plain_result(f"✅ 已获取下载链接，正在作为 txt 文件发送: {file_name}")
        yield event.chain_result([File(name=attachment_name, file=temp_file_path)])
        asyncio.create_task(self._cleanup_temp_file(temp_file_path))

    def _format_file_list(self, files: List[Dict], current_path: str, user_config: Dict, user_id: str = None) -> str:
        """格式化文件列表或搜索结果"""
        is_search_result = current_path.startswith("🔍 搜索") 
        title = f"📁 {current_path}" if not is_search_result else current_path

        if not files: return f"{title}\n\n❌ 列表为空"

        nav_state = self._get_user_navigation_state(user_id)
        current_page = nav_state.get("current_page", 1)
        max_files_per_page = user_config.get("max_display_files", 20)
        total_items = len(files)
        total_pages = (total_items + max_files_per_page - 1) // max_files_per_page
        start_index = (current_page - 1) * max_files_per_page
        end_index = start_index + max_files_per_page
        items_to_display = files[start_index:end_index]

        result = f"{title}\n\n"

        dirs_count = 0
        files_only_count = 0
        if not is_search_result:
            dirs_count = len([f for f in files if f.get("is_dir", False)])
            files_only_count = total_items - dirs_count 

        for i, item in enumerate(items_to_display, start=start_index + 1):
            name = item.get("name", "")
            size = item.get("size", 0)
            modified = item.get("modified", "")
            is_dir = item.get("is_dir", False)

            if is_dir: icon = "📂"
            else:
                ext = os.path.splitext(name)[1].lower()
                if ext in [".jpg", ".jpeg", ".png", ".gif", ".bmp"]: icon = "🖼️"
                elif ext in [".mp4", ".avi", ".mkv", ".mov"]: icon = "🎬"
                elif ext in [".mp3", ".wav", ".flac", ".aac"]: icon = "🎵"
                elif ext in [".pdf"]: icon = "📄"
                elif ext in [".doc", ".docx"]: icon = "📝"
                elif ext in [".zip", ".rar", ".7z"]: icon = "📦"
                else: icon = "📄"

            result += f"{i:2d}. {icon} {name}{'/' if is_dir else ''}\n"

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

                modified_date_part = modified.split('T')[0] if modified else ''
                if modified_date_part:
                    extra_info.append(f"📅 {modified_date_part}")

            if extra_info:
                result += f"      {' | '.join(extra_info)}\n"

        result += f"\n📄 第 {current_page} / {total_pages} 页"
        if is_search_result:
            result += f" | 📊 总计: {total_items} 个结果"
        else:
            dirs_count = len([f for f in files if f.get("is_dir", False)])
            files_only_count = total_items - dirs_count
            result += f" | 📊 总计: {dirs_count} 个文件夹, {files_only_count} 个文件"

        result += f"\n\n💡 快速导航:"
        result += f"\n\n   • /ol ls 序号 - 进入目录/获取链接"
        result += f"\n\n   • /ol download 序号 - 下载并发送文件"
        if not is_search_result:
             result += f"\n\n   • /ol quit - 返回上级目录"
        if total_pages > 1:
            result += f"\n   • /ol prev - ⬅️ 上一页"
            result += f"\n   • /ol next - ➡️ 下一页"
        return result

    async def _download_file(self, event: AstrMessageEvent, file_item: Dict, user_config: Dict, full_path_override: str = None):
        """下载文件并作为附件发送给用户"""
        user_id = event.get_sender_id()
        file_name = file_item.get("name", "")
        file_size = file_item.get("size", 0)
        if not self._is_extension_allowed(file_name, user_config):
            yield event.plain_result(
                f"❌ 文件类型不允许下载: {file_name}\n"
                f"💡 当前允许: {self._format_extension_filter(user_config)}"
            )
            return
        max_download_size_mb = self._get_size_limit_mb(user_config, "max_download_size", 50)
        max_download_size = max_download_size_mb * 1024 * 1024
        if max_download_size_mb > 0 and file_size > max_download_size:
            size_mb = file_size / (1024 * 1024)
            yield event.plain_result(f"❌ 文件过大: {size_mb:.1f}MB > {max_download_size_mb}MB\n💡 请使用 /ol ls 获取下载链接")
            return
        try:
            if full_path_override:
                file_path = full_path_override
            else:
                file_path = self._get_item_full_path(user_id, file_item, user_config)

            async with self._create_openlist_client(user_config) as client:
                link = await client.get_direct_download_link(file_path)
                if not link:
                    yield event.plain_result("❌ 无法获取真实下载链接，请确认配置账号为 OpenList 管理员或具有 /api/fs/link 权限")
                    return
                download_url = link["url"]
                download_headers = self._normalize_download_headers(link.get("header", {}))
                link_size = link.get("content_length")
                try:
                    link_size = int(link_size) if link_size is not None else 0
                except (TypeError, ValueError):
                    link_size = 0
                if not file_size and link_size > 0:
                    file_size = link_size
                if max_download_size_mb > 0 and link_size > max_download_size:
                    size_mb = link_size / (1024 * 1024)
                    yield event.plain_result(f"❌ 文件过大: {size_mb:.1f}MB > {max_download_size_mb}MB\n💡 请使用 /ol ls 获取下载链接")
                    return
                downloads_dir = os.path.join(StarTools.get_data_dir("openlist"), "downloads")
                os.makedirs(downloads_dir, exist_ok=True)
                safe_filename = self._sanitize_filename(file_name)
                temp_file_path = os.path.join(downloads_dir, f"{user_id}_{self._unique_suffix()}_{safe_filename}")
                yield event.plain_result(f"📥 开始下载: {file_name}\n💾 大小: {self._format_file_size(file_size)}")
                async with aiohttp.ClientSession() as session:
                    async with session.get(download_url, headers=download_headers) as response:
                        if response.status == 200:
                            with open(temp_file_path, "wb") as f:
                                downloaded = 0
                                async for chunk in response.content.iter_chunked(8192):
                                    f.write(chunk)
                                    downloaded += len(chunk)
                                    if (
                                        self._get_bool_config(user_config, "debug_transfer_logging", False)
                                        and file_size > 10 * 1024 * 1024
                                        and downloaded % (10 * 1024 * 1024) < 8192
                                    ):
                                        progress = (downloaded / file_size) * 100
                                        logger.info(
                                            f"下载进度: {file_name} {progress:.1f}% "
                                            f"({self._format_file_size(downloaded)}/{self._format_file_size(file_size)})"
                                        )
                            yield event.plain_result(f"✅ 下载完成，正在发送文件...")
                            file_component = File(name=file_name, file=temp_file_path)
                            yield event.chain_result([file_component])
                            asyncio.create_task(self._cleanup_temp_file(temp_file_path))
                        else:
                            error_text = await response.text()
                            logger.error(f"用户 {user_id} 下载文件失败 - HTTP状态: {response.status}, 响应: {error_text}, 文件: {file_name}, URL: {download_url}")
                            yield event.plain_result(f"❌ 下载失败: HTTP {response.status}\n💡 提示: 管理员可在后台日志中查看详细错误信息")
        except Exception as e:
            logger.error(f"用户 {user_id} 下载文件失败: {e}, 文件: {file_name}, 路径: {file_path}", exc_info=True)
            yield event.plain_result(f"❌ 下载失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")

    async def _get_and_send_download_link(self, event: AstrMessageEvent, item: Dict, user_config: Dict, full_path: str = None):
        """获取指定项目的文件链接并发送"""
        user_id = event.get_sender_id()
        yield event.plain_result(f"🔗 正在获取文件链接: {item.get('name', '')}...")

        # 如果提供了 full_path，则直接使用；否则，根据 item 信息构建路径
        if full_path:
            file_path = full_path
        else:
            file_path = self._get_item_full_path(user_id, item, user_config)

        file_name = item.get("name", "")
        if not self._is_extension_allowed(file_name, user_config):
            yield event.plain_result(
                f"❌ 文件类型不允许获取链接: {file_name}\n"
                f"💡 当前允许: {self._format_extension_filter(user_config)}"
            )
            return

        try:
            async with self._create_openlist_client(user_config) as client:
                download_url = await client.get_download_url(file_path)
                if download_url:
                    name = item.get("name", "")
                    size = item.get("size", 0)
                    async for result in self._send_download_link_txt(event, name, size, file_path, download_url):
                        yield result
                else:
                    logger.warning(f"用户 {user_id} 无法获取下载链接 - 路径: {file_path}, 文件名: {item.get('name', '')}")
                    yield event.plain_result(f"❌ 无法获取下载链接，文件可能不存在或为目录: {file_path}")
        except Exception as e:
            logger.error(f"用户 {user_id} 获取下载链接失败: {e}, 路径: {file_path}, 文件名: {item.get('name', '')}", exc_info=True)
            yield event.plain_result(f"❌ 操作失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")

    async def _run_group_file_autobackup(
        self,
        event: AstrMessageEvent,
        file_component: File,
        file_name: str,
        file_size: Optional[int],
        file_url: str,
        target_path: str,
        user_config: Dict,
        group_id: str,
    ) -> None:
        """后台执行群文件自动备份，避免阻塞同一条消息上的其他处理器。"""
        async with self.autobackup_semaphore:
            file_path = None
            try:
                max_size_mb = self._get_size_limit_mb(user_config, "backup_max_size", 0)
                if max_size_mb > 0 and file_size is not None and file_size > (max_size_mb * 1024 * 1024):
                    logger.info(f"⏭️ [自动备份] 文件 {file_name} 事件大小 {file_size} 超过限制 {max_size_mb}MB，跳过。")
                    return

                logger.info(f"🚀 [自动备份] 发现新文件: {file_name} -> {target_path}")
                async with self._create_openlist_client(user_config) as client:
                    if not await client.ensure_dir(target_path):
                        logger.error(f"❌ [自动备份] 创建目标目录失败: {target_path}")
                        return
                    if file_url and file_size is not None:
                        logger.info(f"🚀 [自动备份] 使用 URL 流式中转: {file_name}, size={file_size}, target={target_path}")
                        success = await client.upload_url_stream(file_url, target_path, file_name, file_size)
                    else:
                        get_file_started_at = time.monotonic()
                        file_path = await file_component.get_file()
                        logger.info(
                            f"📥 [自动备份] 本地获取完成: {file_name}, path={file_path}, "
                            f"elapsed={time.monotonic() - get_file_started_at:.2f}s"
                        )

                        if not file_path or not os.path.exists(file_path):
                            logger.error(f"❌ [自动备份] 无法获取文件路径: {file_name}")
                            return

                        actual_size = os.path.getsize(file_path)
                        if max_size_mb > 0 and actual_size > (max_size_mb * 1024 * 1024):
                            logger.info(f"⏭️ [自动备份] 文件 {file_name} 实际下载大小 {actual_size} 超过限制 {max_size_mb}MB，跳过。")
                            return

                        success = await client.upload_file(file_path, target_path, file_name)

                    if success:
                        logger.info(f"✅ [自动备份] 文件 {file_name} 上传成功。")
                        self.cache_manager.clear_cache()
                    else:
                        logger.error(f"❌ [自动备份] 文件 {file_name} 上传失败。")
            except Exception as e:
                logger.error(f"❌ [自动备份] 处理文件 {file_name} 出错: {e}", exc_info=True)
            finally:
                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except OSError as e:
                        logger.warning(f"⚠️ [自动备份] 清理临时文件失败: group={group_id}, file={file_name}, err={e}")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=2)
    async def handle_group_file_upload(self, event: AstrMessageEvent):
        """处理群文件上传事件（自动备份）"""
        raw_event_data = event.message_obj.raw_message
        message_list = raw_event_data.get("message") if isinstance(raw_event_data, dict) else None
        if not isinstance(message_list, list):
            return
        
        # 遍历消息段寻找文件段
        for segment_dict in message_list:
            if isinstance(segment_dict, dict) and segment_dict.get("type") == "file":
                data_dict = segment_dict.get("data", {})
                file_name = data_dict.get("file")
                file_id = data_dict.get("file_id")
                file_size = data_dict.get("file_size")
                file_url = data_dict.get("url")
                
                if not file_name or not file_id:
                    continue
                
                # 转换文件大小
                if isinstance(file_size, str):
                    try:
                        file_size = int(file_size)
                    except ValueError:
                        file_size = None
                
                # 命中文件，开始执行自动备份检查
                group_id = str(event.message_obj.group_id)
                if not group_id:
                    return
                
                global_cfg = self.get_global_config()
                target_path = self._get_autobackup_target_path(global_cfg, group_id)
                
                if not target_path:
                    return
                
                user_config = global_cfg
                if not self._validate_config(user_config):
                    logger.warning(f"⚠️ [自动备份] 群 {group_id} 触发了自动备份，但未找到有效的 Openlist 配置。")
                    return
                
                # 预先检查大小限制 (从事件数据获取)
                if file_size is not None:
                    max_size_mb = self._get_size_limit_mb(user_config, "backup_max_size", 0)
                    if max_size_mb > 0 and file_size > (max_size_mb * 1024 * 1024):
                        logger.info(f"⏭️ [自动备份] 文件 {file_name} 超过限制 {max_size_mb}MB (事件报送大小: {file_size})，跳过。")
                        return

                # 获取对应的 File 组件
                file_component = None
                for msg in event.get_messages():
                    if isinstance(msg, File):
                        file_component = msg
                        break
                
                if not file_component:
                    return
                
                # 使用配置中的备份过滤条件
                allowed_exts = self._get_extension_filter(user_config, "backup_allowed_extensions")
                if allowed_exts:
                    ext = os.path.splitext(file_name.lower())[1]
                    if ext not in allowed_exts:
                        logger.info(f"⏭️ [自动备份] 文件 {file_name} 后缀 {ext} 不在允许范围内，跳过。")
                        return
                
                task_user_config = dict(user_config)
                asyncio.create_task(
                    self._run_group_file_autobackup(
                        event=event,
                        file_component=file_component,
                        file_name=file_name,
                        file_size=file_size,
                        file_url=file_url,
                        target_path=target_path,
                        user_config=task_user_config,
                        group_id=group_id,
                    )
                )
                logger.debug(f"🧵 [自动备份] 已转入后台任务: group={group_id}, file={file_name}")

                break # 已经处理了文件，跳出循环


    async def _upload_file(self, event: AstrMessageEvent, file_component: File, user_config: Dict):
        user_id = event.get_sender_id()
        upload_state_key = self._get_upload_state_key(event)
        upload_state = self._get_user_upload_state(upload_state_key)
        target_path = upload_state["target_path"]

        file_name = None
        raw_file_id = None
        raw_file_size = None
        raw_file_url = None
        component_name = getattr(file_component, "name", None)
        component_url = getattr(file_component, "url", None)
        component_file = getattr(file_component, "file_", None)
        raw_event_data = event.message_obj.raw_message
        message_list = raw_event_data.get("message") if isinstance(raw_event_data, dict) else None
        if isinstance(message_list, list):
            for segment_dict in message_list:
                if isinstance(segment_dict, dict) and segment_dict.get("type") == "file":
                    data_dict = segment_dict.get("data", {})
                    file_name = data_dict.get("file")
                    raw_file_id = data_dict.get("file_id")
                    raw_file_size = data_dict.get("file_size")
                    raw_file_url = data_dict.get("url")
                    if file_name:
                        break

        file_name = file_name or component_name
        if not file_name:
            yield event.plain_result("出现异常，请稍后尝试上传")
            logger.warning(f"用户 {user_id} 上传文件失败：无法从原始消息中解析出有效的文件名。")
            return
        if not self._is_extension_allowed(file_name, user_config):
            yield event.plain_result(
                f"❌ 文件类型不允许上传: {file_name}\n"
                f"💡 当前允许: {self._format_extension_filter(user_config)}"
            )
            return

        raw_file_size_int = None
        if raw_file_size not in (None, ""):
            try:
                raw_file_size_int = int(raw_file_size)
            except (TypeError, ValueError):
                logger.warning(f"用户 {user_id} 上传文件大小解析失败: name={file_name}, raw_size={raw_file_size}")

        try:
            logger.info(
                f"用户 {user_id} 准备处理上传文件: name={file_name}, target={target_path}, "
                f"raw_size={raw_file_size}, file_id={raw_file_id}, raw_has_url={bool(raw_file_url)}, "
                f"component_name={component_name}, component_has_url={bool(component_url)}, "
                f"component_file={component_file}"
            )
            max_upload_size_mb = self._get_size_limit_mb(user_config, "max_upload_size", 100)
            max_upload_size = max_upload_size_mb * 1024 * 1024
            if max_upload_size_mb > 0 and raw_file_size_int is not None and raw_file_size_int > max_upload_size:
                size_mb = raw_file_size_int / (1024 * 1024)
                yield event.plain_result(f"❌ 文件过大: {size_mb:.1f}MB > {max_upload_size_mb}MB")
                return

            upload_url = raw_file_url or component_url
            if upload_url and (raw_file_size_int is not None or max_upload_size_mb == 0):
                yield event.plain_result(f"📤 开始上传: {file_name}\n💾 大小: {self._format_file_size(raw_file_size_int) if raw_file_size_int is not None else '未知'}\n📂 目标: {target_path}")
                logger.info(
                    f"用户 {user_id} 使用 URL 流式中转上传: name={file_name}, "
                    f"size={raw_file_size_int}, target={target_path}, openlist_url={user_config.get('openlist_url')}"
                )
                async with self._create_openlist_client(user_config) as client:
                    success = await client.upload_url_stream(upload_url, target_path, file_name, raw_file_size_int)
                    if success:
                        yield event.plain_result(f"✅ 上传成功!\n📄 文件: {file_name}\n📂 路径: {target_path}")
                        self.cache_manager.clear_cache(user_id)
                        self._set_user_upload_waiting(upload_state_key, False)
                        result = await client.list_files(target_path)
                        if result:
                            files = result.get("content", [])
                            self._update_user_navigation_state(user_id, target_path, files)
                            formatted_list = self._format_file_list(files, target_path, user_config, user_id)
                            yield event.plain_result(f"📁 当前目录已更新:\n\n{formatted_list}")
                    else:
                        yield event.plain_result("❌ 上传失败，请检查网络连接和权限\n💡 提示: 管理员可在后台日志中查看详细错误信息")
                return

            if upload_url and raw_file_size_int is None and max_upload_size_mb > 0:
                logger.warning(f"用户 {user_id} 上传文件缺少有效大小，无法预先执行大小限制，回退到本地临时文件上传: name={file_name}")

            yield event.plain_result(f"📥 正在获取文件: {file_name}\n💾 大小: {self._format_file_size(raw_file_size_int) if raw_file_size_int is not None else '未知'}")
            get_file_started_at = time.monotonic()
            file_path = await file_component.get_file()
            get_file_elapsed = time.monotonic() - get_file_started_at

            if not file_path or not os.path.exists(file_path):
                logger.error(
                    f"用户 {user_id} 获取上传文件失败: name={file_name}, returned_path={file_path}, "
                    f"elapsed={get_file_elapsed:.2f}s"
                )
                yield event.plain_result("❌ 无法获取文件，请重新发送")
                return

            try:
                file_size = os.path.getsize(file_path)
                logger.info(
                    f"用户 {user_id} 获取上传文件完成: name={file_name}, local_path={file_path}, "
                    f"actual_size={file_size}, elapsed={get_file_elapsed:.2f}s"
                )
                if max_upload_size_mb > 0 and file_size > max_upload_size:
                    size_mb = file_size / (1024 * 1024)
                    yield event.plain_result(f"❌ 文件过大: {size_mb:.1f}MB > {max_upload_size_mb}MB")
                    return

                yield event.plain_result(f"📤 开始上传: {file_name}\n💾 大小: {self._format_file_size(file_size)}\n📂 目标: {target_path}")
                logger.info(
                    f"用户 {user_id} 开始调用 OpenList 上传: name={file_name}, local_path={file_path}, "
                    f"target={target_path}, openlist_url={user_config.get('openlist_url')}"
                )
                async with self._create_openlist_client(user_config) as client:
                    success = await client.upload_file(file_path, target_path, file_name)
                    if success:
                        yield event.plain_result(f"✅ 上传成功!\n📄 文件: {file_name}\n📂 路径: {target_path}")
                        self.cache_manager.clear_cache(user_id)
                        self._set_user_upload_waiting(upload_state_key, False)
                        result = await client.list_files(target_path)
                        if result:
                            files = result.get("content", [])
                            self._update_user_navigation_state(user_id, target_path, files)
                            formatted_list = self._format_file_list(files, target_path, user_config, user_id)
                            yield event.plain_result(f"📁 当前目录已更新:\n\n{formatted_list}")
                    else:
                        yield event.plain_result(f"❌ 上传失败，请检查网络连接和权限\n💡 提示: 管理员可在后台日志中查看详细错误信息")
            finally:
                if os.path.exists(file_path):
                    os.remove(file_path)
        except Exception as e:
            logger.error(f"用户 {user_id} 上传文件失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 上传失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")
            self._set_user_upload_waiting(upload_state_key, False)

    async def _get_group_files_recursive(self, bot, group_id: int, folder_id: str = "/", current_path: str = "") -> List[Dict]:
        """递归获取群文件列表"""
        all_files = []
        try:
            if folder_id == "/":
                res = await bot.api.call_action("get_group_root_files", group_id=group_id)
            else:
                res = await bot.api.call_action("get_group_files_by_folder", group_id=group_id, folder_id=folder_id)
            
            if not res:
                return []
            
            files = res.get("files", [])
            folders = res.get("folders", [])
            
            for f in files:
                f["relative_path"] = f"{current_path}/{f['file_name']}".lstrip("/")
                all_files.append(f)
                
            for folder in folders:
                sub_folder_id = folder.get("folder_id")
                sub_folder_name = folder.get("folder_name")
                if sub_folder_id:
                    sub_files = await self._get_group_files_recursive(
                        bot, group_id, sub_folder_id, f"{current_path}/{sub_folder_name}"
                    )
                    all_files.extend(sub_files)
                    
            return all_files
        except Exception as e:
            logger.error(f"递归获取群 {group_id} 文件失败: {e}", exc_info=True)
            return all_files

    async def _backup_group_files(self, event: AstrMessageEvent, group_id: int, target_path: str, user_config: Dict):
        """执行群文件备份"""
        bot = event.bot
        async for result in self._do_backup_logic(bot, event, group_id, target_path, user_config):
            yield result

    async def _do_backup_logic(self, bot, event: AstrMessageEvent, group_id: int, target_path: str, user_config: Dict, is_auto: bool = False):
        """核心备份逻辑，支持手动和自动备份"""
        if not is_auto:
            yield event.plain_result(f"🔍 正在扫描群 {group_id} 的所有文件，请稍候...")
        
        all_items = await self._get_group_files_recursive(bot, group_id)
        if not all_items:
            if not is_auto:
                yield event.plain_result("❌ 未找到任何群文件或获取失败。")
            return
            
        allowed_exts = self._get_extension_filter(user_config, "backup_allowed_extensions")
        max_size_mb = self._get_size_limit_mb(user_config, "backup_max_size", 0)
        max_size = max_size_mb * 1024 * 1024 if max_size_mb > 0 else 0
        
        filtered_items = []
        for item in all_items:
            name = item.get("file_name", "").lower()
            size = item.get("file_size", 0)
            
            if allowed_exts:
                ext = os.path.splitext(name)[1]
                if ext not in allowed_exts:
                    continue
            
            if max_size > 0 and size > max_size:
                continue
                
            filtered_items.append(item)
            
        if not filtered_items:
            if not is_auto:
                yield event.plain_result("⚠️ 扫描完成，但没有符合过滤条件的文件需要备份。")
            return
            
        total = len(filtered_items)
        if not is_auto:
            yield event.plain_result(f"📦 扫描完成，共发现 {total} 个文件需要备份。\n🚀 开始备份到 Openlist: {target_path}")
        else:
            logger.info(f"🚀 [自动备份] 发现 {total} 个新文件，准备备份到群 {group_id} 的目标路径: {target_path}")
        
        success_count = 0
        fail_count = 0

        async with self._create_openlist_client(user_config) as client:
            semaphore = asyncio.Semaphore(3)
            
            async def upload_task(item, idx):
                nonlocal success_count, fail_count
                async with semaphore:
                    file_id = item.get("file_id")
                    file_name = item.get("file_name")
                    rel_path = item.get("relative_path")
                    file_dir = os.path.dirname(rel_path)
                    target_dir = f"{target_path.rstrip('/')}/{file_dir}".rstrip("/")
                    
                    try:
                        if not await client.ensure_dir(target_dir or target_path):
                            fail_count += 1
                            return
                            
                        url_res = await bot.api.call_action("get_group_file_url", group_id=group_id, file_id=file_id, busid=item.get("busid", 0))
                        download_url = url_res.get("url")
                        if not download_url:
                            fail_count += 1
                            return

                        upload_size = item.get("file_size")
                        try:
                            upload_size = int(upload_size) if upload_size is not None else None
                        except (TypeError, ValueError):
                            upload_size = None

                        target_dir = target_dir or "/"
                        logger.info(
                            f"🚀 [群备份] 使用 URL 流式中转: {file_name}, "
                            f"size={upload_size}, target={target_dir}"
                        )
                        up_res = await client.upload_url_stream(download_url, target_dir, file_name, upload_size)
                        if up_res:
                            success_count += 1
                        else:
                            fail_count += 1
                    except Exception as e:
                        logger.error(f"备份文件 {file_name} 失败: {e}")
                        fail_count += 1
            
            batch_size = 5
            for i in range(0, total, batch_size):
                batch_tasks = [upload_task(item, j) for j, item in enumerate(filtered_items[i:i+batch_size], start=i)]
                await asyncio.gather(*batch_tasks)
                logger.info(f"⏳ 备份进度: {min(i+batch_size, total)}/{total} (成功: {success_count}, 失败: {fail_count})")
                
        if not is_auto:
            if success_count:
                self.cache_manager.clear_cache()
            yield event.plain_result(f"✅ 备份任务结束!\n📊 统计: 总计 {total}, 成功 {success_count}, 失败 {fail_count}\n📂 目标: {target_path}")
        else:
            if success_count:
                self.cache_manager.clear_cache()
            logger.info(f"✅ [自动备份] 任务结束。群 {group_id}: 成功 {success_count}, 失败 {fail_count}")

    async def _upload_image(self, event: AstrMessageEvent, image_component: Image, user_config: Dict):
        """上传图片到Openlist"""
        user_id = event.get_sender_id()
        upload_state_key = self._get_upload_state_key(event)
        upload_state = self._get_user_upload_state(upload_state_key)
        target_path = upload_state["target_path"]
        try:
            image_path = await image_component.convert_to_file_path()
            if not image_path or not os.path.exists(image_path):
                yield event.plain_result("❌ 无法获取图片文件，请重新发送")
                return

            try:
                if image_path.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")):
                    ext = os.path.splitext(image_path)[1]
                else:
                    ext = ".jpg"
                filename = f"image_{self._unique_suffix()}{ext}"
                if not self._is_extension_allowed(filename, user_config):
                    yield event.plain_result(
                        f"❌ 图片类型不允许上传: {filename}\n"
                        f"💡 当前允许: {self._format_extension_filter(user_config)}"
                    )
                    return
                file_size = os.path.getsize(image_path)
                max_upload_size_mb = self._get_size_limit_mb(user_config, "max_upload_size", 100)
                max_upload_size = max_upload_size_mb * 1024 * 1024
                if max_upload_size_mb > 0 and file_size > max_upload_size:
                    size_mb = file_size / (1024 * 1024)
                    yield event.plain_result(f"❌ 图片过大: {size_mb:.1f}MB > {max_upload_size_mb}MB")
                    return
                yield event.plain_result(f"📤 开始上传图片: {filename}\n💾 大小: {self._format_file_size(file_size)}\n📂 目标: {target_path}")
                async with self._create_openlist_client(user_config) as client:
                    success = await client.upload_file(image_path, target_path, filename)
                    if success:
                        yield event.plain_result(f"✅ 图片上传成功!\n📄 文件: {filename}\n📂 路径: {target_path}")
                        self.cache_manager.clear_cache(user_id)
                        self._set_user_upload_waiting(upload_state_key, False)
                        result = await client.list_files(target_path)
                        if result:
                            files = result.get("content", [])
                            self._update_user_navigation_state(user_id, target_path, files)
                            formatted_list = self._format_file_list(files, target_path, user_config, user_id)
                            yield event.plain_result(f"📁 当前目录已更新:\n\n{formatted_list}")
                    else:
                        yield event.plain_result(f"❌ 上传失败，请检查网络连接和权限\n💡 提示: 管理员可在后台日志中查看详细错误信息")
            finally:
                if os.path.exists(image_path):
                    os.remove(image_path)
        except Exception as e:
            logger.error(f"用户 {user_id} 上传图片失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 上传失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")
            self._set_user_upload_waiting(upload_state_key, False)

    @filter.command_group("ol", alias=["网盘"])
    def openlist_group(self):
        """Openlist文件管理命令组"""
        pass

    @openlist_group.command("config", alias=["配置"])
    async def config_command(self, event: AstrMessageEvent, action: str = "show", key: str = "", value: str = ""):
        """配置 Openlist 连接与插件参数"""
        user_id = event.get_sender_id()
        if action == "show":
            user_config = self.get_user_config(user_id)
            config_text = f"📋 用户 {event.get_sender_name()} 的配置:\n\n"
            safe_config = user_config.copy()
            if safe_config.get("password"): safe_config["password"] = "***"
            if safe_config.get("token"): safe_config["token"] = "***"
            for k, v in safe_config.items():
                if k != "setup_completed": config_text += f"🔹 {k}: {v}\n"
            global_cfg = self.get_global_config()
            require_auth = global_cfg.get("require_user_auth", True)
            default_url = global_cfg.get("openlist_url", "")
            if require_auth:
                config_text += f"\n💡 提示: 当前启用了用户独立配置模式"
                if default_url: config_text += f"\n🌐 默认服务器: {default_url}"
            else:
                config_text += f"\n💡 提示: 当前使用全局配置模式"
            yield event.plain_result(config_text)
        elif action == "setup":
            user_manager = self.get_user_config_manager(user_id)
            user_config = user_manager.load_config()
            setup_text = """🛠️ Openlist配置向导

请按以下步骤配置:

1️⃣ 设置Openlist服务器地址:
   /ol config set openlist_url http://your-server:5244

2️⃣ 设置用户名(可选):
   /ol config set username your_username

3️⃣ 设置密码(可选):
   /ol config set password your_password

4️⃣ 测试连接:
   /ol config test

5️⃣ 开始使用:
   /ol ls /

💡 如果服务器不需要登录，只需要设置openlist_url即可"""
            yield event.plain_result(setup_text)
        elif action == "set":
            if not key:
                yield event.plain_result("❌ 请指定配置项名称")
                return
            if not value:
                yield event.plain_result("❌ 请指定配置项值")
                return
            user_manager = self.get_user_config_manager(user_id)
            user_config = user_manager.load_config()
            valid_keys = [
                "openlist_url", "username", "password", "token", 
                "max_display_files", "public_openlist_url", 
                "fixed_base_directory", "allowed_extensions", "max_preview_size", "text_preview_length",
                "enable_cache", "cache_duration", "max_download_size", "max_upload_size", "upload_mode_timeout",
                "upload_chunk_size_mb", "upload_progress_step_mb", "upstream_connect_timeout",
                "upstream_read_timeout", "openlist_connect_timeout", "openlist_upload_response_timeout",
                "debug_transfer_logging", "debug_upload_logging",
                "backup_default_path", "backup_allowed_extensions", "backup_max_size"
            ]
            if key not in valid_keys:
                yield event.plain_result(f"❌ 未知的配置项: {key}。可用配置项: {', '.join(valid_keys)}")
                return
            if key == "debug_upload_logging":
                key = "debug_transfer_logging"
            
            if key in [
                "max_display_files", "cache_duration", "backup_max_size", "max_preview_size",
                "text_preview_length", "max_download_size", "max_upload_size", "upload_mode_timeout",
                "upload_chunk_size_mb", "upload_progress_step_mb", "upstream_connect_timeout",
                "upstream_read_timeout", "openlist_connect_timeout", "openlist_upload_response_timeout"
            ]:
                try:
                    value = int(value)
                    if key == "max_display_files" and (value < 1 or value > 100):
                        yield event.plain_result("❌ max_display_files 必须在1-100之间")
                        return
                    if key == "cache_duration" and (value < 1):
                        yield event.plain_result("❌ cache_duration 必须大于0")
                        return
                    if key == "backup_max_size" and (value < 0):
                        yield event.plain_result("❌ backup_max_size 必须大于等于0")
                        return
                    if key == "max_download_size" and (value < 0):
                        yield event.plain_result("❌ max_download_size 必须大于等于0")
                        return
                    if key == "max_upload_size" and (value < 0):
                        yield event.plain_result("❌ max_upload_size 必须大于等于0")
                        return
                    if key == "upload_mode_timeout" and (value < 1):
                        yield event.plain_result("❌ upload_mode_timeout 必须大于0")
                        return
                    if key in ["upload_chunk_size_mb", "upload_progress_step_mb"] and value < 1:
                        yield event.plain_result(f"❌ {key} 必须大于0")
                        return
                    if key in ["upstream_connect_timeout", "upstream_read_timeout", "openlist_connect_timeout", "openlist_upload_response_timeout"] and value < 1:
                        yield event.plain_result(f"❌ {key} 必须大于0")
                        return
                    if key == "max_preview_size" and (value < -1):
                        yield event.plain_result("❌ max_preview_size 必须大于等于 -1 (-1表示禁用, 0表示不限制)")
                        return
                    if key == "text_preview_length" and (value < 1):
                        yield event.plain_result("❌ text_preview_length 必须大于0")
                        return
                except ValueError:
                    yield event.plain_result(f"❌ {key} 必须是数字")
                    return
            elif key in ["enable_cache", "debug_transfer_logging"]:
                value = value.lower() in ["true", "1", "yes", "on"]
            elif key in ["allowed_extensions", "backup_allowed_extensions"]:
                # 允许输入逗号分隔的字符串，存为列表
                if isinstance(value, str):
                    if value.strip().lower() in ("none", "null", "empty", "clear", "all", "*", "空", "不限", "不限制"):
                        value = []
                    else:
                        value = [ext.strip().lower() for ext in value.split(",") if ext.strip()]
                    # 确保后缀带点
                    value = [ext if ext.startswith(".") else f".{ext}" for ext in value]
            
            user_config[key] = value
            if key == "openlist_url" and value:
                user_config["setup_completed"] = True
            user_manager.save_config(user_config)
            
            display_value = "***" if key in ["password", "token"] else str(value)
            yield event.plain_result(f"✅ 已为用户 {event.get_sender_name()} 设置 {key} = {display_value}")
        elif action == "test":
            user_config = self.get_user_config(user_id)
            if not self._validate_config(user_config):
                yield event.plain_result("❌ 请先配置Openlist URL\n💡 使用 /ol config setup 开始配置向导")
                return
            try:
                async with self._create_openlist_client(user_config) as client:
                    files = await client.list_files("/")
                    if files is not None:
                        yield event.plain_result("✅ Openlist连接测试成功!")
                    else:
                        yield event.plain_result("❌ Openlist连接失败，请检查配置")
            except Exception as e:
                logger.error(f"用户 {user_id} 连接测试失败: {e}, 服务器: {user_config.get('openlist_url')}", exc_info=True)
                yield event.plain_result(f"❌ 连接测试失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")
        elif action == "clear_cache":
            self.cache_manager.clear_cache(user_id)
            yield event.plain_result("✅ 已清理您的文件列表缓存")
        else:
            yield event.plain_result("❌ 未知的操作，支持: show, set, test, setup, clear_cache")

    @openlist_group.command("ls", alias=["列表", "直链"])
    async def list_files(self, event: AstrMessageEvent, path: str = ""):
        """列出文件和目录，或获取文件链接"""
        user_id = event.get_sender_id()
        user_config = self.get_user_config(user_id)
        if not self._validate_config(user_config):
            yield event.plain_result("❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导")
            return
        path = (path or "").strip()
        target_path = self._resolve_target_path(user_id, path)
        path_candidates = [target_path]
        if path.isdigit():
            number = int(path)
            item = self._get_item_by_number(user_id, number)
            if item:
                if item.get("is_dir", False):
                    target_path = self._get_item_full_path(user_id, item, user_config)
                    path_candidates = [target_path]
                else:
                    async for result in self._get_and_send_download_link(event, item, user_config):
                        yield result
                    return
            else:
                yield event.plain_result(f"❌ 序号 {number} 无效，请使用 /ol ls 查看当前目录")
                return
        else:
            path_candidates = self._resolve_path_candidates(user_id, path)
        try:
            cache_enabled = str(user_config.get("enable_cache", True)).lower() not in ("false", "0", "no", "off")
            cache_duration = self._get_cache_duration_seconds(user_config)
            async with self._create_openlist_client(user_config) as client:
                for candidate_path in path_candidates:
                    file_info = await client.get_file_info(candidate_path)
                    if file_info and not file_info.get("is_dir", False):
                        async for result in self._get_and_send_download_link(event, file_info, user_config, full_path=candidate_path):
                            yield result
                        return

                    list_result = None
                    if cache_enabled:
                        list_result = self.cache_manager.get_cache(
                            user_config["openlist_url"],
                            candidate_path,
                            user_id,
                            cache_duration,
                        )
                    if list_result is None:
                        list_result = await client.list_files(candidate_path, per_page=0)
                        if list_result is not None and cache_enabled:
                            self.cache_manager.set_cache(
                                user_config["openlist_url"],
                                candidate_path,
                                user_id,
                                list_result,
                            )
                    if list_result is not None:
                        files = list_result.get("content") or []
                        self._update_user_navigation_state(user_id, candidate_path, files)
                        formatted_list = self._format_file_list(files, candidate_path, user_config, user_id)
                        yield event.plain_result(formatted_list)
                        return

                display_path = " / ".join(path_candidates)
                logger.warning(f"用户 {user_id} 无法访问路径候选: {display_path}")
                yield event.plain_result(f"❌ 无法访问路径: {display_path}")
        except Exception as e:
            logger.error(f"用户 {user_id} 列出文件失败: {e}, 路径候选: {path_candidates}", exc_info=True)
            yield event.plain_result(f"❌ 操作失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")

    @openlist_group.command("next", alias=["下一页"])
    async def next_page(self, event: AstrMessageEvent):
        """下一页"""
        user_id = event.get_sender_id()
        user_config = self.get_user_config(user_id)
        nav_state = self._get_user_navigation_state(user_id)
        if not nav_state.get("items"):
            yield event.plain_result("🤔 没有可供翻页的列表，请先使用 /ol ls 查看一个目录。")
            return
        current_page = nav_state.get("current_page", 1)
        all_items = nav_state.get("items", [])
        max_files_per_page = user_config.get("max_display_files", 20)
        total_pages = (len(all_items) + max_files_per_page - 1) // max_files_per_page

        if current_page < total_pages:
            nav_state["current_page"] += 1
        else:
            yield event.plain_result("➡️ 已经是最后一页了。")
            return

        formatted_list = self._format_file_list(
            all_items, nav_state["current_path"], user_config, user_id
        )
        yield event.plain_result(formatted_list)

    @openlist_group.command("prev", alias=["上一页"])
    async def prev_page(self, event: AstrMessageEvent):
        """上一页"""
        user_id = event.get_sender_id()
        user_config = self.get_user_config(user_id)
        nav_state = self._get_user_navigation_state(user_id)
        if not nav_state.get("items"):
            yield event.plain_result("🤔 没有可供翻页的列表，请先使用 /ol ls 查看一个目录。")
            return
        current_page = nav_state.get("current_page", 1)
        all_items = nav_state.get("items", [])
        max_files_per_page = user_config.get("max_display_files", 20)
        total_pages = (len(all_items) + max_files_per_page - 1) // max_files_per_page

        if current_page > 1:
            nav_state["current_page"] -= 1
        else:
            yield event.plain_result("⬅️ 已经是第一页了。")
            return

        formatted_list = self._format_file_list(
            all_items, nav_state["current_path"], user_config, user_id
        )
        yield event.plain_result(formatted_list)

    @openlist_group.command("search", alias=["搜索"])
    async def search_files(self, event: AstrMessageEvent, keyword: str, path: str = "/"):
        """搜索文件"""
        if not keyword:
            yield event.plain_result("❌ 请提供搜索关键词")
            return
        user_id = event.get_sender_id()
        path = self._resolve_target_path(user_id, path)
        user_config = self.get_user_config(user_id)
        if not self._validate_config(user_config):
            yield event.plain_result("❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导")
            return
        try:
            yield event.plain_result(f'🔍 正在搜索 "{keyword}"...')
            async with self._create_openlist_client(user_config) as client:
                files = await client.search_files(keyword, path)
                if files:
                    search_title = f'🔍 搜索 "{keyword}"' 
                    self._update_user_navigation_state(user_id, search_title, files)

                    # 使用通用的列表格式化函数显示第一页
                    formatted_list = self._format_file_list(files, search_title, user_config, user_id)
                    yield event.plain_result(formatted_list)
                else:
                    yield event.plain_result(f"🔍 未找到包含 '{keyword}' 的文件")
        except Exception as e:
            logger.error(f"用户 {user_id} 搜索文件失败: {e}, 关键词: {keyword}, 路径: {path}", exc_info=True)
            yield event.plain_result(f"❌ 搜索失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")

    @openlist_group.command("info", alias=["信息"])
    async def file_info(self, event: AstrMessageEvent, path: str):
        """获取文件详细信息"""
        path = (path or "").strip()
        if not path:
            yield event.plain_result("❌ 请提供文件路径")
            return
        user_id = event.get_sender_id()
        user_config = self.get_user_config(user_id)
        if not self._validate_config(user_config):
            yield event.plain_result("❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导")
            return
        path_candidates = self._resolve_path_candidates(user_id, path)
        target_path = path_candidates[0]
        try:
            async with self._create_openlist_client(user_config) as client:
                file_info = None
                for candidate_path in path_candidates:
                    file_info = await client.get_file_info(candidate_path)
                    if file_info:
                        target_path = candidate_path
                        break
                if file_info:
                    name = file_info.get("name", "")
                    size = file_info.get("size", 0)
                    modified = file_info.get("modified", "")
                    is_dir = file_info.get("is_dir", False)
                    provider = file_info.get("provider", "")
                    download_url = None
                    info_text = f"📋 文件信息\n\n"
                    info_text += f"📄 名称: {name}\n"
                    info_text += f"📁 类型: {'目录' if is_dir else '文件'}\n"
                    info_text += f"📍 路径: {target_path}\n"
                    if not is_dir: info_text += f"💾 大小: {self._format_file_size(size)}\n"
                    if modified: info_text += f"📅 修改时间: {modified.replace('T', ' ').split('.')[0]}\n"
                    if provider: info_text += f"🔗 存储: {provider}\n"
                    if not is_dir:
                        if self._is_extension_allowed(name, user_config):
                            download_url = await client.get_download_url(target_path)
                            if download_url:
                                info_text += "\n🔗 下载链接将作为 txt 附件发送。"
                        else:
                            info_text += f"\n🔗 下载链接: 文件类型不允许（当前允许: {self._format_extension_filter(user_config)}）"
                    yield event.plain_result(info_text)
                    if download_url:
                        async for result in self._send_download_link_txt(event, name, size, target_path, download_url):
                            yield result
                else:
                    display_path = " / ".join(path_candidates)
                    logger.warning(f"用户 {user_id} 文件不存在: {display_path}")
                    yield event.plain_result(f"❌ 文件不存在: {display_path}")
        except Exception as e:
            logger.error(f"用户 {user_id} 获取文件信息失败: {e}, 路径候选: {path_candidates}", exc_info=True)
            yield event.plain_result(f"❌ 操作失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")

    @openlist_group.command("download", alias=["下载"])
    async def get_download_link(self, event: AstrMessageEvent, path: str):
        """直接下载指定的文件"""
        path = (path or "").strip()
        if not path:
            yield event.plain_result("❌ 请提供文件路径或序号")
            return
        user_id = event.get_sender_id()
        user_config = self.get_user_config(user_id)
        if not self._validate_config(user_config):
            yield event.plain_result("❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导")
            return

        item_to_download = None
        full_path_override = None

        if path.isdigit():
            number = int(path)
            item = self._get_item_by_number(user_id, number)
            if item:
                if item.get("is_dir", False):
                    yield event.plain_result(f"❌ 序号 {number} 是目录，无法下载。")
                    return
                item_to_download = item
            else:
                yield event.plain_result(f"❌ 序号 {number} 无效。")
                return
        else:
            path_candidates = self._resolve_path_candidates(user_id, path)
            try:
                async with self._create_openlist_client(user_config) as client:
                    for candidate_path in path_candidates:
                        file_info = await client.get_file_info(candidate_path)
                        if file_info and not file_info.get("is_dir", False):
                            item_to_download = file_info
                            full_path_override = candidate_path
                            break
                    if not item_to_download:
                        display_path = " / ".join(path_candidates)
                        yield event.plain_result(f"❌ 无法下载，文件不存在或路径为目录: {display_path}")
                        return
            except Exception as e:
                logger.error(f"用户 {user_id} 获取文件信息失败: {e}, 路径候选: {path_candidates}", exc_info=True)
                yield event.plain_result(f"❌ 操作失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")
                return

        if item_to_download:
            yield event.plain_result(f"📥 正在准备下载文件: {item_to_download.get('name', '')}...")
            async for result in self._download_file(event, item_to_download, user_config, full_path_override=full_path_override):
                yield result

    @openlist_group.command("quit", alias=["上一级", "返回"])
    async def quit_navigation(self, event: AstrMessageEvent):
        """返回上级目录"""
        user_id = event.get_sender_id()
        user_config = self.get_user_config(user_id)
        if not self._validate_config(user_config):
            yield event.plain_result("❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导")
            return
        nav_state = self._get_user_navigation_state(user_id)
        if not nav_state["parent_paths"]:
            yield event.plain_result("📂 已经在根目录，无法继续回退。")
            return
        previous_path = nav_state["parent_paths"].pop()
        try:
            async with self._create_openlist_client(user_config) as client:
                result = await client.list_files(previous_path)
                if result is not None:
                    files = result.get("content") or []
                    nav_state["current_path"] = previous_path
                    nav_state["items"] = files
                    formatted_list = self._format_file_list(files, previous_path, user_config, user_id)
                    yield event.plain_result(f"⬅️ 已返回上级目录\n\n{formatted_list}")
                else:
                    logger.warning(f"用户 {user_id} 无法访问上级目录: {previous_path}")
                    yield event.plain_result(f"❌ 无法访问上级目录: {previous_path}")
        except Exception as e:
            logger.error(f"用户 {user_id} 回退目录失败: {e}, 目标路径: {previous_path}", exc_info=True)
            yield event.plain_result(f"❌ 回退失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")

    @openlist_group.command("upload", alias=["上传"])
    async def upload_command(self, event: AstrMessageEvent, target: str = ""):
        """上传文件命令"""
        user_id = event.get_sender_id()
        upload_state_key = self._get_upload_state_key(event)
        target = (target or "").strip()
        if target.lower() in ("cancel", "取消"):
            upload_state = self._get_user_upload_state(upload_state_key)
            if upload_state["waiting"]:
                self._set_user_upload_waiting(upload_state_key, False)
                yield event.plain_result("✅ 已取消上传模式")
            else:
                yield event.plain_result("❌ 当前不在上传模式")
            return

        user_config = self.get_user_config(user_id)
        if not self._validate_config(user_config):
            yield event.plain_result("❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导")
            return

        upload_timeout_minutes = self._get_upload_mode_timeout_minutes(user_config)
        target_path = self._resolve_target_path(user_id, target)
        try:
            async with self._create_openlist_client(user_config) as client:
                result = await client.list_files(target_path, per_page=1)
                if result is None:
                    yield event.plain_result(f"❌ 无法访问上传目标目录: {target_path}")
                    return
        except Exception as e:
            logger.error(f"用户 {user_id} 检查上传目标目录失败: {e}, 路径: {target_path}", exc_info=True)
            yield event.plain_result(f"❌ 无法访问上传目标目录: {target_path}\n💡 提示: 管理员可在后台日志中查看详细错误信息")
            return

        self._set_user_upload_waiting(upload_state_key, True, target_path)
        upload_text = f"""📤 上传模式已启动

📂 目标目录: {target_path}

💡 请直接发送文件或图片，系统会自动上传到此目录

⏰ 上传模式将在{upload_timeout_minutes}分钟后自动取消

📋 支持的操作:

• 直接发送文件 - 上传文件

• 直接发送图片 - 上传图片

• /ol upload 路径 - 切换上传目标目录

• /ol upload cancel - 取消上传模式

• /ol ls - 查看当前目录"""
        yield event.plain_result(upload_text)
        async def auto_cancel_upload():
            await asyncio.sleep(upload_timeout_minutes * 60)
            upload_state = self._get_user_upload_state(upload_state_key)
            if upload_state["waiting"] and upload_state.get("target_path") == target_path:
                self._set_user_upload_waiting(upload_state_key, False)
                logger.info(f"用户 {user_id} 在会话 {upload_state_key} 的上传模式已自动取消（超时{upload_timeout_minutes}分钟）")
        asyncio.create_task(auto_cancel_upload())

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_file_message(self, event: AstrMessageEvent):
        """处理文件消息"""
        if not isinstance(event, AstrMessageEvent): return

        if not self._is_regular_message_event(event):
            return

        messages = event.get_messages()
        file_components = [msg for msg in messages if isinstance(msg, (File, Image))]
        if not file_components:
            return
        
        user_id = event.get_sender_id()
        upload_state_key = self._get_upload_state_key(event)
        upload_state = self._get_user_upload_state(upload_state_key)
        if not upload_state["waiting"]: return
        
        user_config = self.get_user_config(user_id)
        if not self._validate_config(user_config):
            yield event.plain_result("❌ 请先配置Openlist连接信息")
            self._set_user_upload_waiting(upload_state_key, False)
            return

        file_component = file_components[0]
        if isinstance(file_component, Image):
            async for result in self._upload_image(event, file_component, user_config):
                yield result
        else:
            async for result in self._upload_file(event, file_component, user_config):
                yield result

    @openlist_group.command("backup", alias=["备份"])
    async def backup_command(self, event: AstrMessageEvent, arg1: str = None, arg2: str = None):
        """群文件备份到 Openlist"""
        user_id = event.get_sender_id()
        user_config = self.get_user_config(user_id)
        if not self._validate_config(user_config):
            yield event.plain_result("❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导")
            return
            
        target_path_arg = None
        target_group_id = 0
        
        # 1. 智能解析参数
        for arg in [arg1, arg2]:
            if not arg: continue
            if arg.startswith("/"):
                target_path_arg = arg
            elif arg.startswith("@"):
                try:
                    target_group_id = int(arg[1:])
                except ValueError:
                    yield event.plain_result(f"❌ 无效的群号格式: {arg}")
                    return
            else:
                yield event.plain_result(f"⚠️ 无法识别参数 '{arg}'。路径请以 / 开头，群号请以 @ 开头。")
                return
        
        # 2. 确定群号 (手动指定优先，否则用当前群)
        if not target_group_id:
            if event.message_obj.group_id:
                target_group_id = int(event.message_obj.group_id)
            else:
                yield event.plain_result("❌ 请指定群号（以 @ 开头）或在群聊中使用。")
                return

        target_path = self._render_backup_path(
            target_path_arg or user_config.get("backup_default_path", "/backup/group_{group_id}"),
            target_group_id,
        )
                
        async for result in self._backup_group_files(event, target_group_id, target_path, user_config):
            yield result

    @openlist_group.command("autobackup", alias="自动备份")
    async def autobackup_command(self, event: AstrMessageEvent, action: str = "show", arg1: str = None, arg2: str = None):
        """配置自动备份"""
        global_cfg = self.get_global_config()
        if not self._is_event_admin(event):
            logger.warning(
                f"自动备份配置权限不足: user={event.get_sender_id()}, "
                f"group={getattr(event.message_obj, 'group_id', '')}, "
                f"role={self._extract_sender_role(event)!r}"
            )
            yield event.plain_result("❌ 权限不足。")
            return

        action = (action or "show").lower()
        if action in ("show", "status", "list", "状态", "列表"):
            effective_groups = global_cfg.get("autobackup_groups", [])
            lines = ["🔄 自动备份配置", ""]
            if effective_groups:
                lines.append("已启用群组:")
                for item in effective_groups:
                    if not isinstance(item, str):
                        continue
                    if ":" in item:
                        gid, path = item.split(":", 1)
                        path = self._render_backup_path(path, gid)
                    else:
                        gid = item
                        path = self._render_backup_path(
                            global_cfg.get("autobackup_default_path", "/backup/group_{group_id}"),
                            gid,
                        )
                    lines.append(f"• 群 {gid} -> {path}")
            else:
                lines.append("当前没有启用自动备份的群组。")
            lines.extend([
                "",
                "用法:",
                "/ol autobackup enable [@群号] [/OpenList路径]",
                "/ol autobackup disable [@群号]",
                "未指定群号时使用当前群；未指定路径时使用 autobackup_default_path。",
            ])
            yield event.plain_result("\n".join(lines))
            return
        
        target_gid = None
        target_path = None
        
        # 1. 智能解析参数: 路径必须以 / 开头，群号必须以 @ 开头
        for arg in [arg1, arg2]:
            if not arg: continue
            if arg.startswith("/"):
                target_path = arg
            elif arg.startswith("@"):
                target_gid = arg[1:]
            else:
                yield event.plain_result(f"⚠️ 无法识别参数 '{arg}'。路径请以 / 开头，群号请以 @ 开头。")
                return
        
        # 2. 确定群号 (手动指定优先，否则用当前群)
        if not target_gid:
            if event.message_obj.group_id:
                target_gid = str(event.message_obj.group_id)
            else:
                yield event.plain_result("❌ 请指定群号（以 @ 开头）或在群聊中使用。")
                return

        local_cfg = self.global_config_manager.load_config()
        groups = local_cfg.get("autobackup_groups", [])

        if action == "enable":
            target_path = self._render_backup_path(
                target_path or global_cfg.get("autobackup_default_path", "/backup/group_{group_id}"),
                target_gid,
            )
                
            new_entry = f"{target_gid}:{target_path}"
            # 过滤掉旧的该群配置
            new_groups = [item for item in groups if (item.split(":", 1)[0] if ":" in item else item) != target_gid]
            new_groups.append(new_entry)
            local_cfg["autobackup_groups"] = new_groups
            self.global_config_manager.save_config(local_cfg)
            yield event.plain_result(f"✅ 群 {target_gid} 自动备份已开启 -> {target_path}")
            
        elif action == "disable":
            # disable 只需要群号，忽略路径
            new_groups = [item for item in groups if (item.split(":", 1)[0] if ":" in item else item) != target_gid]
            if len(new_groups) < len(groups):
                local_cfg["autobackup_groups"] = new_groups
                self.global_config_manager.save_config(local_cfg)
                yield event.plain_result(f"✅ 群 {target_gid} 自动备份已禁用。")
            else:
                yield event.plain_result(f"💡 群 {target_gid} 当前未开启自动备份。")
        else:
            yield event.plain_result("❌ 未知操作。请使用 enable 或 disable。")

    @openlist_group.command("restore", alias=["恢复"])
    async def restore_command(self, event: AstrMessageEvent, path: str, target: str = None):
        """将 Openlist 路径中的文件恢复到群组或私聊"""
        user_id = event.get_sender_id()
        user_config = self.get_user_config(user_id)
        if not self._validate_config(user_config):
            yield event.plain_result("❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导")
            return

        # 1. 确定目标群号
        target_group_id = None
        if target:
            if target.startswith("@"):
                try:
                    target_group_id = int(target[1:])
                except ValueError:
                    yield event.plain_result(f"❌ 群号格式错误: {target}")
                    return
            else:
                yield event.plain_result(f"⚠️ 无法识别目标参数 '{target}'。群号请以 @ 开头。")
                return
        
        # 如果未指定群号，尝试获取当前会话群号
        if not target_group_id:
            if event.message_obj.group_id:
                target_group_id = int(event.message_obj.group_id)
        
        is_group = target_group_id is not None
        target_desc = f"群 {target_group_id}" if is_group else "私聊会话"
        
        yield event.plain_result(f"🚀 正在启动恢复任务...\n📂 来源路径: {path}\n🎯 目标: {target_desc}")
        
        try:
            async with self._create_openlist_client(user_config) as client:
                # 递归搜集文件
                files_to_restore = []
                base_path = path.rstrip('/')
                
                async def collect(current_path):
                    res = await client.list_files(current_path, per_page=0)
                    if not res: return
                    for item in res.get("content", []):
                        full_item_path = f"{current_path.rstrip('/')}/{item['name']}"
                        if item.get("is_dir"):
                            await collect(full_item_path)
                        else:
                            item["full_path"] = full_item_path
                            # 计算相对于基础路径的相对路径
                            rel = full_item_path[len(base_path):].lstrip('/')
                            item["relative_path"] = rel
                            files_to_restore.append(item)
                
                # 检查路径是否存在及类型
                file_info = await client.get_file_info(path)
                if not file_info:
                    yield event.plain_result(f"❌ 路径不存在: {path}")
                    return
                
                if file_info.get("is_dir"):
                    await collect(base_path)
                else:
                    file_info["full_path"] = path
                    file_info["relative_path"] = file_info["name"]
                    files_to_restore.append(file_info)
                
                if not files_to_restore:
                    yield event.plain_result(f"📂 路径下没有可恢复的文件。")
                    return
                
                total = len(files_to_restore)
                yield event.plain_result(f"📦 找到 {total} 个文件，开始下载并发送...")
                
                created_folders = {} # {folder_name: folder_id}
                
                # 如果是群组，预先获取根目录下的文件夹，避免重复创建并获取正确的 ID
                if is_group:
                    try:
                        root_files = await event.bot.api.call_action("get_group_root_files", group_id=target_group_id)
                        if root_files and "folders" in root_files:
                            for f in root_files["folders"]:
                                created_folders[f["folder_name"]] = f["folder_id"]
                    except Exception as e:
                        logger.warning(f"获取群根目录文件列表失败: {e}")

                success_count = 0
                fail_count = 0
                max_download_size_mb = self._get_size_limit_mb(user_config, "max_download_size", 50)
                max_download_size = max_download_size_mb * 1024 * 1024
                
                downloads_dir = os.path.join(StarTools.get_data_dir("openlist"), "downloads")
                os.makedirs(downloads_dir, exist_ok=True)

                for i, item in enumerate(files_to_restore, 1):
                    file_name = item["name"]
                    full_path = item["full_path"]
                    rel_path = item["relative_path"]
                    
                    try:
                        if not self._is_extension_allowed(file_name, user_config):
                            logger.info(f"跳过恢复文件 {file_name}: 后缀不在允许范围内。")
                            fail_count += 1
                            continue

                        item_size = item.get("size", 0)
                        if max_download_size_mb > 0 and item_size and item_size > max_download_size:
                            logger.info(f"跳过恢复文件 {file_name}: 大小 {item_size} 超过限制 {max_download_size_mb}MB。")
                            fail_count += 1
                            continue

                        # 1. 下载文件
                        link = await client.get_direct_download_link(full_path)
                        if not link:
                            logger.warning(f"无法获取真实下载链接: {full_path}")
                            fail_count += 1
                            continue
                        download_url = link["url"]
                        download_headers = self._normalize_download_headers(link.get("header", {}))
                        link_size = link.get("content_length")
                        try:
                            link_size = int(link_size) if link_size is not None else 0
                        except (TypeError, ValueError):
                            link_size = 0
                        if max_download_size_mb > 0 and link_size > max_download_size:
                            logger.info(f"跳过恢复文件 {file_name}: 下载链接大小 {link_size} 超过限制 {max_download_size_mb}MB。")
                            fail_count += 1
                            continue
                        
                        safe_filename = self._sanitize_filename(file_name)
                        temp_file_path = os.path.join(downloads_dir, f"restore_{self._unique_suffix()}_{safe_filename}")
                        
                        async with aiohttp.ClientSession() as session:
                            async with session.get(download_url, headers=download_headers) as response:
                                if response.status == 200:
                                    with open(temp_file_path, "wb") as f:
                                        async for chunk in response.content.iter_chunked(8192):
                                            f.write(chunk)
                                else:
                                    logger.error(f"下载失败 {file_name}: HTTP {response.status}")
                                    fail_count += 1
                                    continue
                        
                        # 2. 发送/上传文件
                        if is_group:
                            # 处理文件夹逻辑 (仅限一层)
                            folder_id = None
                            if "/" in rel_path:
                                folder_name = rel_path.split("/")[0]
                                if folder_name not in created_folders:
                                    # 创建文件夹
                                    try:
                                        # 接口不返回 ID，直接尝试创建
                                        await event.bot.api.call_action("create_group_file_folder", group_id=target_group_id, folder_name=folder_name)
                                        
                                        # 创建后刷新列表以获取 ID
                                        root_files = await event.bot.api.call_action("get_group_root_files", group_id=target_group_id)
                                        if root_files and "folders" in root_files:
                                            for f in root_files["folders"]:
                                                if f["folder_name"] == folder_name:
                                                    created_folders[folder_name] = f["folder_id"]
                                                    break
                                    except Exception as e:
                                        # 可能是文件夹已存在，尝试从列表匹配
                                        try:
                                            root_files = await event.bot.api.call_action("get_group_root_files", group_id=target_group_id)
                                            if root_files and "folders" in root_files:
                                                for f in root_files["folders"]:
                                                    if f["folder_name"] == folder_name:
                                                        created_folders[folder_name] = f["folder_id"]
                                                        break
                                        except:
                                            logger.error(f"无法获取群文件夹 {folder_name} 的 ID: {e}")
                                
                                folder_id = created_folders.get(folder_name)
                            
                            # 上传群文件
                            try:
                                await event.bot.api.call_action("upload_group_file", 
                                    group_id=target_group_id, 
                                    file=os.path.abspath(temp_file_path), 
                                    name=file_name, 
                                    folder=folder_id,
                                    folder_id=folder_id # 兼容不同平台的参数名
                                )
                                success_count += 1
                            except Exception as e:
                                logger.error(f"上传群文件 {file_name} 失败: {e}")
                                fail_count += 1
                        else:
                            # 私聊发送
                            try:
                                file_comp = File(name=file_name, file=temp_file_path)
                                await event.send(MessageChain([file_comp]))
                                success_count += 1
                                # 私聊发送后稍作停顿，避免触发频率限制
                                await asyncio.sleep(1)
                            except Exception as e:
                                logger.error(f"私聊发送文件 {file_name} 失败: {e}")
                                fail_count += 1
                                
                        # 3. 清理临时文件
                        if os.path.exists(temp_file_path):
                            os.remove(temp_file_path)
                            
                        if i % 5 == 0 or i == total:
                            logger.info(f"🔄 恢复进度: {i}/{total} (成功: {success_count}, 失败: {fail_count})")
                            
                    except Exception as e:
                        logger.error(f"处理文件 {file_name} 时发生错误: {e}")
                        fail_count += 1
                        if 'temp_file_path' in locals() and os.path.exists(temp_file_path):
                            os.remove(temp_file_path)

                yield event.plain_result(f"✅ 恢复任务完成!\n📊 统计: 总计 {total}, 成功 {success_count}, 失败 {fail_count}\n🎯 目标: {target_desc}")
                
        except Exception as e:
            logger.error(f"恢复任务失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 恢复失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")

    @openlist_group.command("preview", alias=["预览"])
    async def preview_command(self, event: AstrMessageEvent, path: str):
        """预览文件内容"""
        path = (path or "").strip()
        if not path:
            yield event.plain_result("❌ 请提供文件路径或序号")
            return
        user_id = event.get_sender_id()
        user_config = self.get_user_config(user_id)
        
        # 检查配置
        max_preview_size_mb = user_config.get("max_preview_size", 0)
        if max_preview_size_mb == -1:
            yield event.plain_result("❌ 预览功能已禁用。")
            return

        if not self._validate_config(user_config):
            yield event.plain_result("❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导")
            return

        # 获取文件信息
        item = None
        path_or_num = path
        path_candidates = []
        if path_or_num.isdigit():
            number = int(path_or_num)
            item = self._get_item_by_number(user_id, number)
            if item:
                if item.get("is_dir"):
                    yield event.plain_result("❌ 无法预览目录，请指定一个文件。")
                    return
                full_path = self._get_item_full_path(user_id, item, user_config)
            else:
                yield event.plain_result(f"❌ 序号 {number} 无效")
                return
        else:
            path_candidates = self._resolve_path_candidates(user_id, path_or_num)
            full_path = path_candidates[0]
        
        try:
            async with self._create_openlist_client(user_config) as client:
                if not item:
                    for candidate_path in path_candidates:
                        item = await client.get_file_info(candidate_path)
                        if item:
                            full_path = candidate_path
                            break
                    if not item:
                        display_path = " / ".join(path_candidates)
                        yield event.plain_result(f"❌ 未找到文件: {display_path}")
                        return
                    if item.get("is_dir"):
                        yield event.plain_result("❌ 无法预览目录，请指定一个文件。")
                        return

                file_name = item.get("name", "")
                file_size = item.get("size", 0)
                ext = os.path.splitext(file_name)[1].lower()
                if not self._is_extension_allowed(file_name, user_config):
                    yield event.plain_result(
                        f"❌ 文件类型不允许预览: {file_name}\n"
                        f"💡 当前允许: {self._format_extension_filter(user_config)}"
                    )
                    return
                
                # 压缩包预览支持 (使用 API)
                archive_extensions = [".zip", ".tar", ".gz", ".7z", ".rar", ".bz2", ".xz"]
                if ext in archive_extensions:
                    yield event.plain_result(f"🔍 正在读取压缩包内容: {file_name}...")
                    archive_data = await client.list_archive_contents(full_path)
                    if archive_data and "content" in archive_data:
                        contents = archive_data["content"]
                        if not contents:
                            yield event.plain_result(f"📦 压缩包 {file_name} 为空。")
                            return
                        
                        file_list = []
                        for f in contents:
                            prefix = "📁" if f.get("is_dir") else "📄"
                            size_str = f" ({f['size'] / 1024:.1f} KB)" if not f.get("is_dir") else ""
                            file_list.append(f"{prefix} {f['name']}{size_str}")
                        
                        max_display = 20
                        display_list = file_list[:max_display]
                        result_text = f"📦 压缩包预览: {file_name}\n---\n" + "\n".join(display_list)
                        if len(file_list) > max_display:
                            result_text += f"\n\n...(及其他 {len(file_list) - max_display} 个文件)"
                        
                        yield event.plain_result(result_text)
                        return
                    else:
                        yield event.plain_result(f"❌ 无法读取压缩包内容或该格式暂不支持。")
                        return

                # 检查文件大小限制
                if max_preview_size_mb > 0:
                    if file_size > max_preview_size_mb * 1024 * 1024:
                        yield event.plain_result(f"❌ 文件过大 ({file_size / (1024*1024):.2f} MB)，超过了最大预览限制 ({max_preview_size_mb} MB)。")
                        return

                yield event.plain_result(f"🔍 正在获取预览: {file_name}...")
                
                # 获取真实下载链接
                link = await client.get_direct_download_link(full_path)
                if not link:
                    yield event.plain_result("❌ 获取真实下载链接失败，请确认配置账号为 OpenList 管理员或具有 /api/fs/link 权限")
                    return
                download_url = link["url"]
                download_headers = self._normalize_download_headers(link.get("header", {}))

                # 下载到临时目录
                temp_dir = os.path.join(StarTools.get_data_dir("openlist"), "temp_preview")
                os.makedirs(temp_dir, exist_ok=True)
                safe_filename = self._sanitize_filename(file_name)
                temp_file_path = os.path.join(temp_dir, f"preview_{self._unique_suffix()}_{safe_filename}")
                
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(download_url, headers=download_headers) as resp:
                            if resp.status == 200:
                                with open(temp_file_path, "wb") as f:
                                    async for chunk in resp.content.iter_chunked(1024 * 1024):
                                        f.write(chunk)
                            else:
                                yield event.plain_result(f"❌ 下载文件失败: HTTP {resp.status}")
                                return

                    # 仅支持文本预览
                    text_extensions = [".txt", ".md", ".log", ".json", ".xml", ".yaml", ".yml", ".ini", ".conf", ".cfg", ".toml", ".py", ".js", ".java", ".c", ".cpp", ".h", ".go", ".rs", ".php", ".rb", ".sh", ".bash", ".html", ".htm", ".css", ".jsx", ".tsx", ".ts", ".vue", ".sql", ".csv", ".properties", ".env"]
                    
                    if ext in text_extensions:
                        text_length = user_config.get("text_preview_length", 1000)
                        try:
                            with open(temp_file_path, "rb") as f:
                                content_bytes = f.read(text_length * 4) # 多读一点以防编码问题
                                
                                # 使用 chardet 检测编码
                                detection = chardet.detect(content_bytes)
                                encoding = detection.get('encoding', 'utf-8') or 'utf-8'
                                confidence = detection.get('confidence', 0)
                                logger.debug(f"文本预览编码检测: {encoding}, 置信度: {confidence:.2f}")
                                
                                try:
                                    decoded_text = content_bytes.decode(encoding, errors='ignore').strip()
                                except:
                                    # 如果检测出的编码失败，回退到 utf-8
                                    encoding = 'utf-8'
                                    decoded_text = content_bytes.decode('utf-8', errors='ignore').strip()
                                    
                                preview_text = decoded_text[:text_length]
                                if len(decoded_text) > text_length:
                                    preview_text += "\n\n..."
                                
                                yield event.plain_result(f"📝 文本预览:\n---\n{preview_text}")
                        except Exception as e:
                            logger.error(f"文本预览失败: {e}")
                            yield event.plain_result(f"❌ 文本解析失败: {e}")
                    else:
                        yield event.plain_result(f"❓ 该格式 ({ext}) 不在支持的文本预览列表中。")

                finally:
                    # 清理临时文件
                    if os.path.exists(temp_file_path):
                        os.remove(temp_file_path)

        except Exception as e:
            logger.error(f"预览失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 预览失败: {str(e)}")

    @openlist_group.command("rm", alias=["删除"])
    async def remove_command(self, event: AstrMessageEvent, path: str):
        """删除文件或文件夹"""
        path = (path or "").strip()
        if not path:
            yield event.plain_result("❌ 请提供文件路径或序号")
            return
        user_id = event.get_sender_id()
        user_config = self.get_user_config(user_id)
        if not self._validate_config(user_config):
            yield event.plain_result("❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导")
            return

        target_dir = None
        target_names = []
        display_name = ""

        if path.isdigit():
            number = int(path)
            item = self._get_item_by_number(user_id, number)
            if item:
                full_path = self._get_item_full_path(user_id, item, user_config)
                if full_path == "/":
                    yield event.plain_result("❌ 不允许删除根目录。")
                    return
                target_dir = posixpath.dirname(full_path) or "/"
                target_names = [posixpath.basename(full_path)]
                display_name = full_path
            else:
                yield event.plain_result(f"❌ 序号 {number} 无效。")
                return
        else:
            full_path = self._resolve_target_path(user_id, path)
            if full_path == "/":
                yield event.plain_result("❌ 不允许删除根目录。")
                return
            target_dir = posixpath.dirname(full_path) or "/"
            target_names = [posixpath.basename(full_path)]
            display_name = full_path

        try:
            async with self._create_openlist_client(user_config) as client:
                success = await client.remove(target_dir, target_names)
                if success:
                    yield event.plain_result(f"✅ 已删除: {display_name}")
                    self.cache_manager.clear_cache(user_id)
                    
                    # 检查是否删除了当前路径或其父目录
                    nav_state = self._get_user_navigation_state(user_id)
                    current_path = nav_state["current_path"]
                    
                    # 构建被删除项目的完整路径列表
                    deleted_full_paths = []
                    for name in target_names:
                        p = f"{target_dir.rstrip('/')}/{name}"
                        if not p.startswith("/"): p = "/" + p
                        deleted_full_paths.append(p)
                    
                    # 如果当前路径被删除（或当前路径是其子目录），返回根目录
                    is_current_path_deleted = False
                    for deleted_path in deleted_full_paths:
                        if current_path == deleted_path or current_path.startswith(deleted_path + "/"):
                            is_current_path_deleted = True
                            break
                    
                    if is_current_path_deleted:
                        # 返回根目录并刷新
                        result = await client.list_files("/")
                        if result is not None:
                            files = result.get("content") or []
                            self.user_navigation_state[user_id] = {
                                "current_path": "/",
                                "items": files,
                                "parent_paths": [],
                                "current_page": 1,
                            }
                            yield event.plain_result("⚠️ 当前目录已被删除，已自动返回根目录。")
                    elif target_dir == current_path:
                        # 如果在当前目录下删除了某个项目，刷新当前目录
                        result = await client.list_files(current_path)
                        if result is not None:
                            files = result.get("content") or []
                            self._update_user_navigation_state(user_id, current_path, files)
                else:
                    yield event.plain_result(f"❌ 删除失败，请检查权限或路径是否正确")
        except Exception as e:
            logger.error(f"用户 {user_id} 删除失败: {e}, 路径: {path}", exc_info=True)
            yield event.plain_result(f"❌ 删除失败: {str(e)}")

    @openlist_group.command("mkdir", alias=["新建"])
    async def mkdir_command(self, event: AstrMessageEvent, name: str):
        """创建文件夹"""
        name = (name or "").strip()
        if not name:
            yield event.plain_result("❌ 请提供文件夹名称或路径")
            return
        user_id = event.get_sender_id()
        user_config = self.get_user_config(user_id)
        if not self._validate_config(user_config):
            yield event.plain_result("❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导")
            return

        full_path = self._resolve_target_path(user_id, name)
        if full_path == "/":
            yield event.plain_result("❌ 不允许创建根目录。")
            return

        try:
            async with self._create_openlist_client(user_config) as client:
                success = await client.mkdir(full_path)
                if success:
                    yield event.plain_result(f"✅ 已创建文件夹: {name}")
                    self.cache_manager.clear_cache(user_id)
                    # 如果在当前目录下创建，刷新列表
                    nav_state = self._get_user_navigation_state(user_id)
                    current_path = self._normalize_openlist_path(nav_state["current_path"])
                    # 检查创建的文件夹是否在当前目录下（直接子目录）
                    parent_path = posixpath.dirname(full_path) or "/"
                    if parent_path == current_path.rstrip("/") or (current_path == "/" and parent_path == "/"):
                        result = await client.list_files(current_path)
                        if result:
                            files = result.get("content") or []
                            self._update_user_navigation_state(user_id, current_path, files)
                else:
                    yield event.plain_result(f"❌ 创建文件夹失败")
        except Exception as e:
            logger.error(f"用户 {user_id} 创建文件夹失败: {e}, 名称: {name}", exc_info=True)
            yield event.plain_result(f"❌ 创建失败: {str(e)}")

    @openlist_group.command("help", alias=["帮助"])
    async def help_command(self, event: AstrMessageEvent):
        """显示帮助信息"""
        user_id = event.get_sender_id()
        user_config = self.get_user_config(user_id)
        global_cfg = self.get_global_config()
        is_user_auth_mode = global_cfg.get("require_user_auth", True)

        help_text = f"""📚 OpenList 助手帮助
💡 您也可以使用别名 `/网盘` 代替 `/ol`。

---
核心导航指令
---
▶️ `/ol ls [路径|序号]`
   - 浏览目录: 列出内容，若文件过多会自动分页。
     - 示例: `/ol ls` 或 `/ol ls /movies`
   - 进入子目录:
     - 示例: `/ol ls 1` (如果1是目录)
   - 获取链接: 获取文件的下载链接，并以 txt 附件发送。
     - 示例: `/ol ls 2` (如果2是文件)

▶️ `/ol next` - 下一页
▶️ `/ol prev` - 上一页

▶️ `/ol quit`
   - 返回到上级目录。

---
文件操作指令
---
📥 `/ol download <路径|序号>`
   - 直接下载: 将文件作为附件发送给您。
     - 示例: `/ol download 3` (下载列表中的3号文件)
     - 示例: `/ol download /docs/report.pdf`

🔍 `/ol search <关键词> [路径]`
   - 搜索文件。注意：搜索依赖服务器索引，可能不是最新的。
     - 示例: `/ol search "年度报告"`

ℹ️ `/ol info <路径>`
   - 查看文件或目录的详细信息，不支持序号。
     - 示例: `/ol info /docs/report.pdf`

👁️ `/ol preview <路径|序号>`
   - 预览内容: 支持文本文件内容预览或压缩包目录查看。
     - 示例: `/ol preview 1`
     - 示例: `/ol preview /data/config.txt`

📂 `/ol mkdir <名称|路径>`
   - 新建文件夹: 在当前目录或指定路径创建。
     - 示例: `/ol mkdir new_folder`

🗑️ `/ol rm <路径|序号>`
   - 删除项目: 删除文件或文件夹（谨慎操作）。
     - 示例: `/ol rm 4`
     - 示例: `/ol rm /tmp/old_file.txt`

📤 `/ol upload [路径|cancel]`
   - `/ol upload`: 在当前目录开启上传模式。
   - `/ol upload /目标目录`: 在指定目录开启上传模式。
   - `/ol upload 子目录`: 在当前目录下的子目录开启上传模式。
   - `/ol upload cancel`: 取消上传。
   - `使用`: 开启后，直接向机器人发送文件或图片即可。

📦 `/ol backup [/目标路径] [@群号]`
   - 将指定群聊的所有文件递归备份到 Openlist。
   - 示例: `/ol backup /群备份 @123456`
   - 提示: 路径须以 `/` 开头，群号须以 `@` 开头。未指定路径时使用 `backup_default_path`。

🔄 `/ol autobackup <enable|disable> [@群号] [/路径]`
   - 配置群文件自动备份（新上传文件自动同步）。
   - 示例: `/ol autobackup enable` (开启当前群备份到默认路径)
   - 示例: `/ol autobackup enable @123456 /backup` (指定群号和路径)
   - 示例: `/ol autobackup disable @123456` (禁用指定群的自动备份)
   - 提示: 禁用时无需提供路径。路径须以 `/` 开头，群号须以 `@` 开头。

🚚 `/ol restore <路径> [@群号]`
   - 将 Openlist 路径中的文件恢复（发送）到目标群组或私聊。
   - 示例: `/ol restore /backup/group_123456` (恢复到当前会话)
   - 示例: `/ol restore /docs @987654` (恢复到指定群)
   - 提示: 目标为群组时会尝试保持一级目录结构。

---
插件配置指令
---
⚙️ `/ol config setup` - 推荐新用户使用，启动交互式配置向导。
⚙️ `/ol config show` - 显示您当前的配置。
⚙️ `/ol config set <键> <值>` - 修改配置项。
⚙️ `/ol config test` - 测试与服务器的连接。
⚙️ `/ol config clear_cache` - 清除文件列表缓存。
"""

        if is_user_auth_mode:
            help_text += f"""

👤 当前模式: 用户独立认证
   - 每位用户都需要使用 `/ol config setup` 单独配置自己的 Openlist 账户信息。"""

            if not self._validate_config(user_config):
                help_text += f"""

⚠️ 操作提示
   您尚未完成配置，请发送 `/ol config setup` 开始配置向导。"""
        else:
            help_text += f"""

🌐 当前模式: 全局共享
   - 所有用户共享管理员预设的 Openlist 服务器连接，无需单独配置。"""

        help_text += f"""

💡 通用提示:
1.  路径区分大小写，以 `/` 开头表示根目录。
2.  `ls` 获取链接，`download` 直接发送文件。
3.  管理员可在机器人后台的插件配置页面调整全局设置。"""

        yield event.plain_result(help_text)

    async def terminate(self):
        """插件卸载时执行的清理操作"""
        logger.info("OpenList助手已卸载")
