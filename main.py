import asyncio
import json
import os
import hashlib
import time
import tempfile
from typing import List, Dict, Optional
from urllib.parse import urljoin, quote, urlparse
import aiohttp

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api.message_components import Plain, Image, File
from astrbot.api import logger
from astrbot.api.event.filter import CustomFilter
from astrbot.core.config import AstrBotConfig

from .lib.client import OpenlistClient
from .lib.config import UserConfigManager, GlobalConfigManager
from .lib.cache import CacheManager


class OpenlistUploadFilter(CustomFilter):
    """文件上传自定义过滤器 - 处理包含文件或图片的消息"""

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        """检查消息是否包含文件或图片组件"""
        messages = event.get_messages()
        file_components = [msg for msg in messages if isinstance(msg, (File, Image))]
        return len(file_components) > 0


@register(
    "astrbot_plugin_openlistfile",
    "Foolllll",
    "OpenList助手",
    "1.1.3",
    "https://github.com/Foolllll-J/astrbot_plugin_openlistfile",
)
class OpenlistPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.user_config_managers = {}
        self.config = config
        self.global_config_manager = GlobalConfigManager("openlist")
        self.global_config = self.global_config_manager.load_config()
        self.cache_manager = CacheManager("openlist")
        self.user_navigation_state = {}
        self.user_upload_state = {}

    def get_webui_config(self, key: str, default=None):
        """获取WebUI配置项"""
        if self.config:
            return self.config.get("global_settings", {}).get(key, default)
        return default

    async def initialize(self):
        """插件初始化"""
        logger.info("Openlist文件管理插件已加载")
        default_url = self.get_webui_config("default_openlist_url", "")
        require_auth = self.get_webui_config("require_user_auth", True)
        if not default_url and not require_auth:
            logger.warning("Openlist URL未配置，请使用 /ol config 命令配置或在WebUI中配置")

    def get_user_config_manager(self, user_id: str) -> UserConfigManager:
        """获取用户配置管理器"""
        if user_id not in self.user_config_managers:
            self.user_config_managers[user_id] = UserConfigManager("openlist", user_id)
        return self.user_config_managers[user_id]

    def get_user_config(self, user_id: str) -> Dict:
        """获取用户配置"""
        require_user_auth = self.get_webui_config("require_user_auth", True)
        
        # 获取 WebUI/全局配置
        global_cfg = {
            "openlist_url": self.get_webui_config("default_openlist_url", ""),
            "public_openlist_url": self.get_webui_config("public_openlist_url", ""),
            "username": self.get_webui_config("default_username", ""),
            "password": self.get_webui_config("default_password", ""),
            "token": self.get_webui_config("default_token", ""),
            "fixed_base_directory": self.get_webui_config("fixed_base_directory", ""),
            "max_display_files": self.get_webui_config("max_display_files", 20),
            "allowed_extensions": self.get_webui_config(
                "allowed_extensions",
                ".txt,.pdf,.doc,.docx,.zip,.rar,.jpg,.png,.gif,.mp4,.mp3",
            ),
            "enable_preview": self.get_webui_config("enable_preview", True),
        }

        if require_user_auth:
            user_manager = self.get_user_config_manager(user_id)
            user_config = user_manager.load_config()
            
            # 合并逻辑：优先使用用户配置，如果用户配置为空则使用全局配置
            merged_config = user_config.copy()
            
            # 基础连接信息
            for key in ["openlist_url", "username", "password", "token", "public_openlist_url", "fixed_base_directory"]:
                if not merged_config.get(key) and global_cfg.get(key):
                    merged_config[key] = global_cfg[key]
            
            # 其他设置（如果用户配置中存在且不是默认值，则保留用户值；否则同步全局值）
            # 注意：UserConfigManager.default_config 中定义了这些项的初始值
            for key in ["max_display_files", "allowed_extensions", "enable_preview", "enable_cache", "cache_duration"]:
                # 如果用户没改过（还是默认值）且全局有配置，则同步全局配置
                if key == "allowed_extensions":
                    # 扩展名特殊处理：转为列表
                    if isinstance(merged_config.get(key), str):
                        merged_config[key] = merged_config[key].split(",")
                    elif not merged_config.get(key):
                        merged_config[key] = global_cfg[key].split(",") if isinstance(global_cfg[key], str) else global_cfg[key]
                else:
                    # 对于数值和布尔值，如果用户配置里没有或者我们认为需要同步全局，则合并
                    # 这里简单处理：如果用户配置里有，就用用户的。
                    if key not in merged_config and key in global_cfg:
                        merged_config[key] = global_cfg[key]
            
            # 确保 allowed_extensions 始终是列表
            if isinstance(merged_config.get("allowed_extensions"), str):
                merged_config["allowed_extensions"] = merged_config["allowed_extensions"].split(",")

            return merged_config
        else:
            # 未启用用户认证时直接使用全局配置
            if isinstance(global_cfg["allowed_extensions"], str):
                global_cfg["allowed_extensions"] = global_cfg["allowed_extensions"].split(",")
            return global_cfg

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
        if 1 <= number <= len(nav_state["items"]):
            return nav_state["items"][number - 1]
        return None

    def _get_user_upload_state(self, user_id: str) -> Dict:
        """获取用户上传状态"""
        if user_id not in self.user_upload_state:
            self.user_upload_state[user_id] = {"waiting": False, "target_path": "/"}
        return self.user_upload_state[user_id]

    def _set_user_upload_waiting(self, user_id: str, waiting: bool, target_path: str = "/"):
        """设置用户上传等待状态"""
        upload_state = self._get_user_upload_state(user_id)
        upload_state["waiting"] = waiting
        upload_state["target_path"] = target_path

    def _format_file_size(self, size: int) -> str:
        """格式化文件大小"""
        if size < 1024: return f"{size}B"
        elif size < 1024 * 1024: return f"{size / 1024:.1f}KB"
        elif size < 1024 * 1024 * 1024: return f"{size / (1024 * 1024):.1f}MB"
        else: return f"{size / (1024 * 1024 * 1024):.1f}GB"

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
                    fixed_base_dir = user_config.get("fixed_base_directory", "")
                    if fixed_base_dir and parent.startswith(fixed_base_dir):
                        parent = parent[len(fixed_base_dir):]
                        if not parent: parent = "/"
                        elif not parent.startswith("/"): parent = "/" + parent
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
        result += f"\n   • /ol ls <序号> - 进入目录/获取链接"
        result += f"\n   • /ol download <序号> - 下载并发送文件"
        if not is_search_result:
             result += f"\n   • /ol quit - 返回上级目录"
        if total_pages > 1:
            result += f"\n   • /ol prev - ⬅️ 上一页"
            result += f"\n   • /ol next - ➡️ 下一页"
        return result

    async def _download_file(self, event: AstrMessageEvent, file_item: Dict, user_config: Dict, full_path_override: str = None):
        """下载文件并作为附件发送给用户"""
        user_id = event.get_sender_id()
        file_name = file_item.get("name", "")
        file_size = file_item.get("size", 0)
        max_download_size_mb = self.get_webui_config("max_download_size", 50)
        max_download_size = max_download_size_mb * 1024 * 1024
        if file_size > max_download_size:
            size_mb = file_size / (1024 * 1024)
            yield event.plain_result(f"❌ 文件过大: {size_mb:.1f}MB > {max_download_size_mb}MB\n💡 请使用 /ol ls 获取下载链接")
            return
        try:
            if full_path_override:
                file_path = full_path_override
            else:
                parent_path = file_item.get("parent")
                if parent_path:
                    fixed_base_dir = user_config.get("fixed_base_directory", "")
                    if fixed_base_dir and parent_path.startswith(fixed_base_dir):
                        parent_path = parent_path[len(fixed_base_dir):]
                        if not parent_path: parent_path = "/"
                        elif not parent_path.startswith("/"): parent_path = "/" + parent_path
                    file_path = f"{parent_path.rstrip('/')}/{file_name}"
                else:
                    nav_state = self._get_user_navigation_state(user_id)
                    current_path = nav_state["current_path"]
                    if current_path.endswith("/"): file_path = f"{current_path}{file_name}"
                    else: file_path = f"{current_path}/{file_name}"

            async with OpenlistClient(user_config["openlist_url"], user_config.get("public_openlist_url", ""), user_config.get("username", ""), user_config.get("password", ""), user_config.get("token", ""), user_config.get("fixed_base_directory", "")) as client:
                download_url = await client.get_download_url(file_path)
                if not download_url:
                    yield event.plain_result("❌ 无法获取下载链接")
                    return
                downloads_dir = os.path.join(StarTools.get_data_dir("openlist"), "downloads")
                os.makedirs(downloads_dir, exist_ok=True)
                safe_filename = "".join(c for c in file_name if c.isalnum() or c in "._- ")[:100]
                temp_file_path = os.path.join(downloads_dir, f"{user_id}_{int(time.time())}_{safe_filename}")
                yield event.plain_result(f"📥 开始下载: {file_name}\n💾 大小: {self._format_file_size(file_size)}")
                async with aiohttp.ClientSession() as session:
                    async with session.get(download_url) as response:
                        if response.status == 200:
                            with open(temp_file_path, "wb") as f:
                                downloaded = 0
                                async for chunk in response.content.iter_chunked(8192):
                                    f.write(chunk)
                                    downloaded += len(chunk)
                                    if (file_size > 10 * 1024 * 1024 and downloaded % (10 * 1024 * 1024) < 8192):
                                        progress = (downloaded / file_size) * 100
                                        yield event.plain_result(f"📥 下载进度: {progress:.1f}% ({self._format_file_size(downloaded)}/{self._format_file_size(file_size)})")
                            yield event.plain_result(f"✅ 下载完成，正在发送文件...")
                            file_component = File(name=file_name, file=temp_file_path)
                            yield event.chain_result([file_component])
                            async def cleanup_file():
                                await asyncio.sleep(10)
                                try:
                                    if os.path.exists(temp_file_path): os.remove(temp_file_path)
                                except: pass
                            asyncio.create_task(cleanup_file())
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
            nav_state = self._get_user_navigation_state(user_id)
            file_name = item.get("name", "")
            parent_path = item.get("parent", nav_state.get("current_path", "/"))

            fixed_base_dir = user_config.get("fixed_base_directory", "")
            if item.get("parent") and fixed_base_dir and parent_path.startswith(fixed_base_dir):
                parent_path = parent_path[len(fixed_base_dir):]
                if not parent_path: parent_path = "/"
                elif not parent_path.startswith("/"): parent_path = "/" + parent_path

            file_path = f"{parent_path.rstrip('/')}/{file_name}"

        try:
            async with OpenlistClient(user_config["openlist_url"], user_config.get("public_openlist_url", ""), user_config.get("username", ""), user_config.get("password", ""), user_config.get("token", ""), user_config.get("fixed_base_directory", "")) as client:
                download_url = await client.get_download_url(file_path)
                if download_url:
                    name = item.get("name", "")
                    size = item.get("size", 0)
                    result_text = f"📥 下载链接\n\n"
                    result_text += f"📄 文件: {name}\n"
                    result_text += f"💾 大小: {self._format_file_size(size)}\n"
                    result_text += f"🔗 链接: {download_url}\n\n"
                    result_text += "💡 提示: 请复制链接并在浏览器中打开以下载文件。"
                    yield event.plain_result(result_text)
                else:
                    logger.warning(f"用户 {user_id} 无法获取下载链接 - 路径: {file_path}, 文件名: {item.get('name', '')}")
                    yield event.plain_result(f"❌ 无法获取下载链接，文件可能不存在或为目录: {file_path}")
        except Exception as e:
            logger.error(f"用户 {user_id} 获取下载链接失败: {e}, 路径: {file_path}, 文件名: {item.get('name', '')}", exc_info=True)
            yield event.plain_result(f"❌ 操作失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")

    async def _upload_file(self, event: AstrMessageEvent, file_component: File, user_config: Dict):
        user_id = event.get_sender_id()
        upload_state = self._get_user_upload_state(user_id)
        target_path = upload_state["target_path"]

        file_name = None
        raw_event_data = event.message_obj.raw_message
        message_list = raw_event_data.get("message")
        if isinstance(message_list, list):
            for segment_dict in message_list:
                if isinstance(segment_dict, dict) and segment_dict.get("type") == "file":
                    data_dict = segment_dict.get("data", {})
                    file_name = data_dict.get("file")
                    if file_name:
                        break

        if not file_name:
            yield event.plain_result("出现异常，请稍后尝试上传")
            logger.warning(f"用户 {user_id} 上传文件失败：无法从原始消息中解析出有效的文件名。")
            return

        try:
            file_path = await file_component.get_file()
            if not file_path or not os.path.exists(file_path):
                yield event.plain_result("❌ 无法获取文件，请重新发送")
                return

            file_size = os.path.getsize(file_path)
            max_upload_size_mb = self.get_webui_config("max_upload_size", 100)
            max_upload_size = max_upload_size_mb * 1024 * 1024
            if file_size > max_upload_size:
                size_mb = file_size / (1024 * 1024)
                yield event.plain_result(f"❌ 文件过大: {size_mb:.1f}MB > {max_upload_size_mb}MB")
                return

            yield event.plain_result(f"📤 开始上传: {file_name}\n💾 大小: {self._format_file_size(file_size)}\n📂 目标: {target_path}")
            async with OpenlistClient(user_config["openlist_url"], user_config.get("public_openlist_url", ""), user_config.get("username", ""), user_config.get("password", ""), user_config.get("token", ""), user_config.get("fixed_base_directory", "")) as client:
                success = await client.upload_file(file_path, target_path, file_name)
                if success:
                    yield event.plain_result(f"✅ 上传成功!\n📄 文件: {file_name}\n📂 路径: {target_path}")
                    self._set_user_upload_waiting(user_id, False)
                    result = await client.list_files(target_path)
                    if result:
                        files = result.get("content", [])
                        self._update_user_navigation_state(user_id, target_path, files)
                        formatted_list = self._format_file_list(files, target_path, user_config, user_id)
                        yield event.plain_result(f"📁 当前目录已更新:\n\n{formatted_list}")
                else:
                    yield event.plain_result(f"❌ 上传失败，请检查网络连接和权限\n💡 提示: 管理员可在后台日志中查看详细错误信息")
        except Exception as e:
            logger.error(f"用户 {user_id} 上传文件失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 上传失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")
            self._set_user_upload_waiting(user_id, False)

    async def _upload_image(self, event: AstrMessageEvent, image_component: Image, user_config: Dict):
        """上传图片到Openlist"""
        user_id = event.get_sender_id()
        upload_state = self._get_user_upload_state(user_id)
        target_path = upload_state["target_path"]
        try:
            image_path = await image_component.convert_to_file_path()
            if not image_path or not os.path.exists(image_path):
                yield event.plain_result("❌ 无法获取图片文件，请重新发送")
                return
            import time
            timestamp = int(time.time())
            if image_path.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")):
                ext = os.path.splitext(image_path)[1]
            else:
                ext = ".jpg"
            filename = f"image_{timestamp}{ext}"
            file_size = os.path.getsize(image_path)
            max_upload_size_mb = self.get_webui_config("max_upload_size", 100)
            max_upload_size = max_upload_size_mb * 1024 * 1024
            if file_size > max_upload_size:
                size_mb = file_size / (1024 * 1024)
                yield event.plain_result(f"❌ 图片过大: {size_mb:.1f}MB > {max_upload_size_mb}MB")
                return
            yield event.plain_result(f"📤 开始上传图片: {filename}\n💾 大小: {self._format_file_size(file_size)}\n📂 目标: {target_path}")
            async with OpenlistClient(user_config["openlist_url"], user_config.get("public_openlist_url", ""), user_config.get("username", ""), user_config.get("password", ""), user_config.get("token", ""), user_config.get("fixed_base_directory", "")) as client:
                success = await client.upload_file(image_path, target_path, filename)
                if success:
                    yield event.plain_result(f"✅ 图片上传成功!\n📄 文件: {filename}\n📂 路径: {target_path}")
                    self._set_user_upload_waiting(user_id, False)
                    result = await client.list_files(target_path)
                    if result:
                        files = result.get("content", [])
                        self._update_user_navigation_state(user_id, target_path, files)
                        formatted_list = self._format_file_list(files, target_path, user_config, user_id)
                        yield event.plain_result(f"📁 当前目录已更新:\n\n{formatted_list}")
                else:
                    yield event.plain_result(f"❌ 上传失败，请检查网络连接和权限\n💡 提示: 管理员可在后台日志中查看详细错误信息")
        except Exception as e:
            logger.error(f"用户 {user_id} 上传图片失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 上传失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")
            self._set_user_upload_waiting(user_id, False)

    @filter.command_group("ol")
    def openlist_group(self):
        """Openlist文件管理命令组"""
        pass

    @openlist_group.command("config")
    async def config_command(self, event: AstrMessageEvent, action: str = "show", key: str = "", value: str = ""):
        # 配置命令实现
        user_id = event.get_sender_id()
        if action == "show":
            user_config = self.get_user_config(user_id)
            config_text = f"📋 用户 {event.get_sender_name()} 的配置:\n\n"
            safe_config = user_config.copy()
            if safe_config.get("password"): safe_config["password"] = "***"
            if safe_config.get("token"): safe_config["token"] = "***"
            for k, v in safe_config.items():
                if k != "setup_completed": config_text += f"🔹 {k}: {v}\n"
            require_auth = self.get_webui_config("require_user_auth", True)
            default_url = self.get_webui_config("default_openlist_url", "")
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
                "fixed_base_directory", "allowed_extensions", "enable_preview",
                "enable_cache", "cache_duration"
            ]
            if key not in valid_keys:
                yield event.plain_result(f"❌ 未知的配置项: {key}。可用配置项: {', '.join(valid_keys)}")
                return
            
            if key in ["max_display_files", "cache_duration"]:
                try:
                    value = int(value)
                    if key == "max_display_files" and (value < 1 or value > 100):
                        yield event.plain_result("❌ max_display_files 必须在1-100之间")
                        return
                    if key == "cache_duration" and (value < 1):
                        yield event.plain_result("❌ cache_duration 必须大于0")
                        return
                except ValueError:
                    yield event.plain_result(f"❌ {key} 必须是数字")
                    return
            elif key in ["enable_preview", "enable_cache"]:
                value = value.lower() in ["true", "1", "yes", "on"]
            elif key == "allowed_extensions":
                # 允许输入逗号分隔的字符串，存为列表
                if isinstance(value, str):
                    value = [ext.strip() for ext in value.split(",") if ext.strip()]
            
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
                async with OpenlistClient(user_config["openlist_url"], user_config.get("public_openlist_url", ""), user_config.get("username", ""), user_config.get("password", ""), user_config.get("token", ""), user_config.get("fixed_base_directory", "")) as client:
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

    @openlist_group.command("ls")
    async def list_files(self, event: AstrMessageEvent, path: str = "/"):
        """列出文件和目录，或获取文件链接"""
        user_id = event.get_sender_id()
        user_config = self.get_user_config(user_id)
        if not self._validate_config(user_config):
            yield event.plain_result("❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导")
            return
        target_path = path
        if path.isdigit():
            number = int(path)
            item = self._get_item_by_number(user_id, number)
            if item:
                if item.get("is_dir", False):
                    nav_state = self._get_user_navigation_state(user_id)
                    current_path = nav_state["current_path"]
                    item_name = item.get("name", "")
                    target_path = f"{current_path.rstrip('/')}/{item_name}"
                else:
                    async for result in self._get_and_send_download_link(event, item, user_config):
                        yield result
                    return
            else:
                yield event.plain_result(f"❌ 序号 {number} 无效，请使用 /ol ls 查看当前目录")
                return
        try:
            async with OpenlistClient(user_config["openlist_url"], user_config.get("public_openlist_url", ""), user_config.get("username", ""), user_config.get("password", ""), user_config.get("token", ""), user_config.get("fixed_base_directory", "")) as client:
                file_info = await client.get_file_info(target_path)
                if file_info and not file_info.get("is_dir", False):
                    async for result in self._get_and_send_download_link(event, file_info, user_config, full_path=target_path):
                        yield result
                    return
                list_result = await client.list_files(target_path, per_page=0)
                if list_result is not None:
                    files = list_result.get("content", [])
                    self._update_user_navigation_state(user_id, target_path, files)
                    formatted_list = self._format_file_list(files, target_path, user_config, user_id)
                    yield event.plain_result(formatted_list)
                else:
                    logger.warning(f"用户 {user_id} 无法访问路径: {target_path}")
                    yield event.plain_result(f"❌ 无法访问路径: {target_path}")
        except Exception as e:
            logger.error(f"用户 {user_id} 列出文件失败: {e}, 路径: {target_path}", exc_info=True)
            yield event.plain_result(f"❌ 操作失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")

    @openlist_group.command("next")
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

    @openlist_group.command("prev")
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

    @openlist_group.command("search")
    async def search_files(self, event: AstrMessageEvent, keyword: str, path: str = "/"):
        """搜索文件"""
        if not keyword:
            yield event.plain_result("❌ 请提供搜索关键词")
            return
        user_id = event.get_sender_id()
        user_config = self.get_user_config(user_id)
        if not self._validate_config(user_config):
            yield event.plain_result("❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导")
            return
        try:
            yield event.plain_result(f'🔍 正在搜索 "{keyword}"...')
            async with OpenlistClient(user_config["openlist_url"], user_config.get("public_openlist_url", ""), user_config.get("username", ""), user_config.get("password", ""), user_config.get("token", ""), user_config.get("fixed_base_directory", "")) as client:
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

    @openlist_group.command("info")
    async def file_info(self, event: AstrMessageEvent, path: str):
        """获取文件详细信息"""
        if not path:
            yield event.plain_result("❌ 请提供文件路径")
            return
        user_id = event.get_sender_id()
        user_config = self.get_user_config(user_id)
        if not self._validate_config(user_config):
            yield event.plain_result("❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导")
            return
        try:
            async with OpenlistClient(user_config["openlist_url"], user_config.get("public_openlist_url", ""), user_config.get("username", ""), user_config.get("password", ""), user_config.get("token", ""), user_config.get("fixed_base_directory", "")) as client:
                file_info = await client.get_file_info(path)
                if file_info:
                    name = file_info.get("name", "")
                    size = file_info.get("size", 0)
                    modified = file_info.get("modified", "")
                    is_dir = file_info.get("is_dir", False)
                    provider = file_info.get("provider", "")
                    info_text = f"📋 文件信息\n\n"
                    info_text += f"📄 名称: {name}\n"
                    info_text += f"📁 类型: {'目录' if is_dir else '文件'}\n"
                    info_text += f"📍 路径: {path}\n"
                    if not is_dir: info_text += f"💾 大小: {self._format_file_size(size)}\n"
                    if modified: info_text += f"📅 修改时间: {modified.replace('T', ' ').split('.')[0]}\n"
                    if provider: info_text += f"🔗 存储: {provider}\n"
                    if not is_dir:
                        download_url = await client.get_download_url(path)
                        if download_url: info_text += f"\n🔗 下载链接:\n{download_url}"
                    yield event.plain_result(info_text)
                else:
                    logger.warning(f"用户 {user_id} 文件不存在: {path}")
                    yield event.plain_result(f"❌ 文件不存在: {path}")
        except Exception as e:
            logger.error(f"用户 {user_id} 获取文件信息失败: {e}, 路径: {path}", exc_info=True)
            yield event.plain_result(f"❌ 操作失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")

    @openlist_group.command("download")
    async def get_download_link(self, event: AstrMessageEvent, path: str):
        """直接下载指定的文件"""
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
            try:
                async with OpenlistClient(user_config["openlist_url"], user_config.get("public_openlist_url", ""), user_config.get("username", ""), user_config.get("password", ""), user_config.get("token", ""), user_config.get("fixed_base_directory", "")) as client:
                    file_info = await client.get_file_info(path)
                    if file_info and not file_info.get("is_dir", False):
                        item_to_download = file_info
                        full_path_override = path
                    else:
                        yield event.plain_result(f"❌ 无法下载，文件不存在或路径为目录: {path}")
                        return
            except Exception as e:
                logger.error(f"用户 {user_id} 获取文件信息失败: {e}, 路径: {path}", exc_info=True)
                yield event.plain_result(f"❌ 操作失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")
                return

        if item_to_download:
            yield event.plain_result(f"📥 正在准备下载文件: {item_to_download.get('name', '')}...")
            async for result in self._download_file(event, item_to_download, user_config, full_path_override=full_path_override):
                yield result

    @openlist_group.command("quit")
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
            async with OpenlistClient(user_config["openlist_url"], user_config.get("public_openlist_url", ""), user_config.get("username", ""), user_config.get("password", ""), user_config.get("token", ""), user_config.get("fixed_base_directory", "")) as client:
                result = await client.list_files(previous_path)
                if result is not None:
                    files = result.get("content", [])
                    nav_state["current_path"] = previous_path
                    nav_state["items"] = files[: self.get_webui_config("max_display_files", 20)]
                    formatted_list = self._format_file_list(files, previous_path, user_config, user_id)
                    yield event.plain_result(f"⬅️ 已返回上级目录\n\n{formatted_list}")
                else:
                    logger.warning(f"用户 {user_id} 无法访问上级目录: {previous_path}")
                    yield event.plain_result(f"❌ 无法访问上级目录: {previous_path}")
        except Exception as e:
            logger.error(f"用户 {user_id} 回退目录失败: {e}, 目标路径: {previous_path}", exc_info=True)
            yield event.plain_result(f"❌ 回退失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")

    @openlist_group.command("upload")
    async def upload_command(self, event: AstrMessageEvent, action: str = ""):
        """上传文件命令"""
        user_id = event.get_sender_id()
        if action == "cancel":
            upload_state = self._get_user_upload_state(user_id)
            if upload_state["waiting"]:
                self._set_user_upload_waiting(user_id, False)
                yield event.plain_result("✅ 已取消上传模式")
            else:
                yield event.plain_result("❌ 当前不在上传模式")
        elif not action:
            user_config = self.get_user_config(user_id)
            if not self._validate_config(user_config):
                yield event.plain_result("❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导")
                return
            nav_state = self._get_user_navigation_state(user_id)
            current_path = nav_state["current_path"]
            self._set_user_upload_waiting(user_id, True, current_path)
            upload_text = f"""📤 上传模式已启动

📂 目标目录: {current_path}

💡 请直接发送文件或图片，系统会自动上传到此目录
⏰ 上传模式将在10分钟后自动取消

📋 支持的操作:
• 直接发送文件 - 上传文件
• 直接发送图片 - 上传图片
• /ol upload cancel - 取消上传模式
• /ol ls - 查看当前目录"""
            yield event.plain_result(upload_text)
            async def auto_cancel_upload():
                await asyncio.sleep(600)
                upload_state = self._get_user_upload_state(user_id)
                if upload_state["waiting"]:
                    self._set_user_upload_waiting(user_id, False)
                    logger.info(f"用户 {user_id} 上传模式已自动取消（超时10分钟）")
            asyncio.create_task(auto_cancel_upload())
        else:
            yield event.plain_result("❌ 未知操作，支持: /ol upload 或 /ol upload cancel")

    @filter.custom_filter(OpenlistUploadFilter)
    async def handle_file_message(self, event: AstrMessageEvent):
        """处理文件消息"""
        user_id = event.get_sender_id()
        upload_state = self._get_user_upload_state(user_id)
        if not upload_state["waiting"]: return
        user_config = self.get_user_config(user_id)
        if not self._validate_config(user_config):
            yield event.plain_result("❌ 请先配置Openlist连接信息")
            self._set_user_upload_waiting(user_id, False)
            return

        target_path = upload_state["target_path"]
        messages = event.get_messages()
        file_components = [msg for msg in messages if isinstance(msg, (File, Image))]

        if not file_components:
            yield event.plain_result("❌ 未检测到文件或图片，请发送文件进行上传")
            return
        file_component = file_components[0]
        if isinstance(file_component, Image):
            async for result in self._upload_image(event, file_component, user_config):
                yield result
        else:
            async for result in self._upload_file(event, file_component, user_config):
                yield result

    @openlist_group.command("help")
    async def help_command(self, event: AstrMessageEvent):
        """显示全面且更新的帮助信息"""
        user_id = event.get_sender_id()
        user_config = self.get_user_config(user_id)
        is_user_auth_mode = self.get_webui_config("require_user_auth", True)

        help_text = f"""📚 Openlist 文件管理插件 帮助

---
核心导航指令
---
▶️ `/ol ls [路径|序号]`
   - 浏览目录: 列出内容，若文件过多会自动分页。
     - 示例: `/ol ls` 或 `/ol ls /movies`
   - 进入子目录:
     - 示例: `/ol ls 1` (如果1是目录)
   - 获取链接: 获取文件的下载链接。
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

📤 `/ol upload [cancel]`
   - `/ol upload`: 在当前目录开启上传模式。
   - `/ol upload cancel`: 取消上传。
   - `使用`: 开启后，直接向机器人发送文件或图片即可。

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
        logger.info("Openlist文件管理插件已卸载")
