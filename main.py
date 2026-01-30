import asyncio
import os
import time
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


@register(
    "astrbot_plugin_openlistfile",
    "Foolllll",
    "OpenList助手",
    "1.2.2",
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
            "require_user_auth": "require_user_auth",
            "autobackup_groups": "autobackup_groups",
            "backup_allowed_extensions": "backup_allowed_extensions",
            "backup_max_size": "backup_max_size",
        }
        
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
                # 其他项，只有当本地配置是空/默认时才使用 WebUI
                elif not config.get(local_key):
                    config[local_key] = webui_val

        # 统一将扩展名字符串转为列表
        for key in ["allowed_extensions", "backup_allowed_extensions"]:
            if isinstance(config.get(key), str):
                config[key] = [ext.strip().lower() for ext in config[key].split(",") if ext.strip()]
                config[key] = [ext if ext.startswith(".") else f".{ext}" for ext in config[key]]
                
        return config

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
            # 只要用户设置了非空且非默认值，就覆盖全局
            if v and v != self.get_user_config_manager(user_id).default_config.get(k):
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
        max_download_size_mb = user_config.get("max_download_size", 50)
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

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=2)
    async def handle_group_file_upload(self, event: AstrMessageEvent):
        """处理群文件上传事件（自动备份）"""
        raw_event_data = event.message_obj.raw_message
        message_list = raw_event_data.get("message")
        if not isinstance(message_list, list):
            return
        
        # 遍历消息段寻找文件段
        for segment_dict in message_list:
            if isinstance(segment_dict, dict) and segment_dict.get("type") == "file":
                data_dict = segment_dict.get("data", {})
                file_name = data_dict.get("file")
                file_id = data_dict.get("file_id")
                file_size = data_dict.get("file_size")
                
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
                autobackup_groups = global_cfg.get("autobackup_groups", [])
                
                target_path = None
                for item in autobackup_groups:
                    if ":" in item:
                        gid, path = item.split(":", 1)
                        if gid == group_id:
                            target_path = path
                            break
                    elif item == group_id:
                        target_path = f"/backup/group_{group_id}"
                        break
                
                if not target_path:
                    return
                
                user_id = event.get_sender_id()
                user_config = self.get_user_config(user_id)
                
                # 如果用户未配置 Openlist 地址，则使用全局配置中的备份相关参数
                if not self._validate_config(user_config):
                    user_config = global_cfg
                
                if not self._validate_config(user_config):
                    logger.warning(f"⚠️ [自动备份] 群 {group_id} 触发了自动备份，但未找到有效的 Openlist 配置。")
                    return
                
                # 预先检查大小限制 (从事件数据获取)
                if file_size is not None:
                    max_size_mb = user_config.get("backup_max_size", 0)
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
                allowed_exts = user_config.get("backup_allowed_extensions", [])
                if allowed_exts:
                    ext = os.path.splitext(file_name.lower())[1]
                    if ext not in allowed_exts:
                        logger.info(f"⏭️ [自动备份] 文件 {file_name} 后缀 {ext} 不在允许范围内，跳过。")
                        return
                
                try:
                    file_path = await file_component.get_file()
                    if not file_path or not os.path.exists(file_path):
                        logger.error(f"❌ [自动备份] 无法获取文件路径: {file_name}")
                        return
                    
                    try:
                        # 再次确认实际下载的文件大小
                        actual_size = os.path.getsize(file_path)
                        max_size_mb = user_config.get("backup_max_size", 0)
                        if max_size_mb > 0 and actual_size > (max_size_mb * 1024 * 1024):
                            logger.info(f"⏭️ [自动备份] 文件 {file_name} 实际下载大小 {actual_size} 超过限制 {max_size_mb}MB，跳过。")
                            return
                        
                        logger.info(f"🚀 [自动备份] 发现新文件: {file_name} -> {target_path}")
                        async with OpenlistClient(
                            user_config["openlist_url"], 
                            user_config.get("public_openlist_url", ""), 
                            user_config.get("username", ""), 
                            user_config.get("password", ""), 
                            user_config.get("token", ""), 
                            user_config.get("fixed_base_directory", "")
                        ) as client:
                            await client.mkdir(target_path)
                            success = await client.upload_file(file_path, target_path, file_name)
                            if success:
                                logger.info(f"✅ [自动备份] 文件 {file_name} 上传成功。")
                            else:
                                logger.error(f"❌ [自动备份] 文件 {file_name} 上传失败。")
                    finally:
                        if os.path.exists(file_path):
                            os.remove(file_path)
                    
                except Exception as e:
                    logger.error(f"❌ [自动备份] 处理文件 {file_name} 出错: {e}", exc_info=True)
                
                break # 已经处理了文件，跳出循环


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

            try:
                file_size = os.path.getsize(file_path)
                max_upload_size_mb = user_config.get("max_upload_size", 100)
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
            finally:
                if os.path.exists(file_path):
                    os.remove(file_path)
        except Exception as e:
            logger.error(f"用户 {user_id} 上传文件失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 上传失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")
            self._set_user_upload_waiting(user_id, False)

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
            
        allowed_exts = user_config.get("backup_allowed_extensions", [])
        max_size_mb = user_config.get("backup_max_size", 0)
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
        
        temp_dir = os.path.join(StarTools.get_data_dir("openlist"), "temp_backup")
        os.makedirs(temp_dir, exist_ok=True)
        
        async with OpenlistClient(
            user_config["openlist_url"], 
            user_config.get("public_openlist_url", ""), 
            user_config.get("username", ""), 
            user_config.get("password", ""), 
            user_config.get("token", ""), 
            user_config.get("fixed_base_directory", "")
        ) as client:
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
                        if file_dir:
                            parts = file_dir.split("/")
                            curr = target_path.rstrip("/")
                            for p in parts:
                                curr = f"{curr}/{p}"
                                await client.mkdir(curr)
                        else:
                            await client.mkdir(target_path)
                            
                        url_res = await bot.api.call_action("get_group_file_url", group_id=group_id, file_id=file_id, busid=item.get("busid", 0))
                        download_url = url_res.get("url")
                        if not download_url:
                            fail_count += 1
                            return
                            
                        local_path = os.path.join(temp_dir, f"{int(time.time())}_{file_id}_{file_name}")
                        try:
                            async with aiohttp.ClientSession() as session:
                                async with session.get(download_url) as resp:
                                    if resp.status == 200:
                                        with open(local_path, "wb") as f:
                                            f.write(await resp.read())
                                        
                                        up_res = await client.upload_file(local_path, target_dir, file_name)
                                        if up_res:
                                            success_count += 1
                                        else:
                                            fail_count += 1
                                    else:
                                        fail_count += 1
                        finally:
                            if os.path.exists(local_path):
                                os.remove(local_path)
                    except Exception as e:
                        logger.error(f"备份文件 {file_name} 失败: {e}")
                        fail_count += 1
            
            batch_size = 5
            for i in range(0, total, batch_size):
                batch_tasks = [upload_task(item, j) for j, item in enumerate(filtered_items[i:i+batch_size], start=i)]
                await asyncio.gather(*batch_tasks)
                logger.info(f"⏳ 备份进度: {min(i+batch_size, total)}/{total} (成功: {success_count}, 失败: {fail_count})")
                
        if not is_auto:
            yield event.plain_result(f"✅ 备份任务结束!\n📊 统计: 总计 {total}, 成功 {success_count}, 失败 {fail_count}\n📂 目标: {target_path}")
        else:
            logger.info(f"✅ [自动备份] 任务结束。群 {group_id}: 成功 {success_count}, 失败 {fail_count}")

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

            try:
                import time
                timestamp = int(time.time())
                if image_path.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")):
                    ext = os.path.splitext(image_path)[1]
                else:
                    ext = ".jpg"
                filename = f"image_{timestamp}{ext}"
                file_size = os.path.getsize(image_path)
                max_upload_size_mb = user_config.get("max_upload_size", 100)
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
            finally:
                if os.path.exists(image_path):
                    os.remove(image_path)
        except Exception as e:
            logger.error(f"用户 {user_id} 上传图片失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 上传失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")
            self._set_user_upload_waiting(user_id, False)

    @filter.command_group("ol", alias=["网盘"])
    def openlist_group(self):
        """Openlist文件管理命令组"""
        pass

    @openlist_group.command("config", alias=["配置"])
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
                "enable_cache", "cache_duration", "max_download_size", "max_upload_size",
                "backup_allowed_extensions", "backup_max_size"
            ]
            if key not in valid_keys:
                yield event.plain_result(f"❌ 未知的配置项: {key}。可用配置项: {', '.join(valid_keys)}")
                return
            
            if key in ["max_display_files", "cache_duration", "backup_max_size", "max_preview_size", "text_preview_length", "max_download_size", "max_upload_size"]:
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
                    if key == "max_preview_size" and (value < -1):
                        yield event.plain_result("❌ max_preview_size 必须大于等于 -1 (-1表示禁用, 0表示不限制)")
                        return
                    if key == "text_preview_length" and (value < 1):
                        yield event.plain_result("❌ text_preview_length 必须大于0")
                        return
                except ValueError:
                    yield event.plain_result(f"❌ {key} 必须是数字")
                    return
            elif key in ["enable_cache"]:
                value = value.lower() in ["true", "1", "yes", "on"]
            elif key in ["allowed_extensions", "backup_allowed_extensions"]:
                # 允许输入逗号分隔的字符串，存为列表
                if isinstance(value, str):
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

    @openlist_group.command("ls", alias=["列表", "直链"])
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
                    files = list_result.get("content") or []
                    self._update_user_navigation_state(user_id, target_path, files)
                    formatted_list = self._format_file_list(files, target_path, user_config, user_id)
                    yield event.plain_result(formatted_list)
                else:
                    logger.warning(f"用户 {user_id} 无法访问路径: {target_path}")
                    yield event.plain_result(f"❌ 无法访问路径: {target_path}")
        except Exception as e:
            logger.error(f"用户 {user_id} 列出文件失败: {e}, 路径: {target_path}", exc_info=True)
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

    @openlist_group.command("info", alias=["信息"])
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

    @openlist_group.command("download", alias=["下载"])
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
            async with OpenlistClient(user_config["openlist_url"], user_config.get("public_openlist_url", ""), user_config.get("username", ""), user_config.get("password", ""), user_config.get("token", ""), user_config.get("fixed_base_directory", "")) as client:
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

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_file_message(self, event: AstrMessageEvent):
        """处理文件消息"""
        if not isinstance(event, AstrMessageEvent): return
        
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

    @openlist_group.command("backup", alias=["备份"])
    async def backup_command(self, event: AstrMessageEvent, arg1: str = None, arg2: str = None):
        """群文件备份到 Openlist"""
        user_id = event.get_sender_id()
        user_config = self.get_user_config(user_id)
        if not self._validate_config(user_config):
            yield event.plain_result("❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导")
            return
            
        target_path = "/"
        target_group_id = 0
        
        # 1. 智能解析参数
        for arg in [arg1, arg2]:
            if not arg: continue
            if arg.startswith("/"):
                target_path = arg
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
                
        async for result in self._backup_group_files(event, target_group_id, target_path, user_config):
            yield result

    @openlist_group.command("autobackup", alias="自动备份")
    async def autobackup_command(self, event: AstrMessageEvent, action: str, arg1: str = None, arg2: str = None):
        """配置自动备份"""
        global_cfg = self.get_global_config()
        if not global_cfg.get("require_user_auth", True) and event.message_obj.sender.role < 5:
            yield event.plain_result("❌ 权限不足。")
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
            # enable 必须有路径，没有则用默认
            if not target_path:
                target_path = f"/backup/group_{target_gid}"
                
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
            async with OpenlistClient(user_config["openlist_url"], user_config.get("public_openlist_url", ""), user_config.get("username", ""), user_config.get("password", ""), user_config.get("token", ""), user_config.get("fixed_base_directory", "")) as client:
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
                
                downloads_dir = os.path.join(StarTools.get_data_dir("openlist"), "downloads")
                os.makedirs(downloads_dir, exist_ok=True)

                for i, item in enumerate(files_to_restore, 1):
                    file_name = item["name"]
                    full_path = item["full_path"]
                    rel_path = item["relative_path"]
                    
                    try:
                        # 1. 下载文件
                        download_url = await client.get_download_url(full_path)
                        if not download_url:
                            logger.warning(f"无法获取下载链接: {full_path}")
                            fail_count += 1
                            continue
                        
                        temp_file_path = os.path.join(downloads_dir, f"restore_{int(time.time())}_{file_name}")
                        
                        async with aiohttp.ClientSession() as session:
                            async with session.get(download_url) as response:
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
        if path_or_num.isdigit():
            number = int(path_or_num)
            item = self._get_item_by_number(user_id, number)
            if item:
                if item.get("is_dir"):
                    yield event.plain_result("❌ 无法预览目录，请指定一个文件。")
                    return
                nav_state = self._get_user_navigation_state(user_id)
                current_path = nav_state["current_path"]
                full_path = f"{current_path.rstrip('/')}/{item['name']}"
            else:
                yield event.plain_result(f"❌ 序号 {number} 无效")
                return
        else:
            full_path = path_or_num
        
        try:
            async with OpenlistClient(user_config["openlist_url"], user_config.get("public_openlist_url", ""), user_config.get("username", ""), user_config.get("password", ""), user_config.get("token", ""), user_config.get("fixed_base_directory", "")) as client:
                if not item:
                    item = await client.get_file_info(full_path)
                    if not item:
                        yield event.plain_result(f"❌ 未找到文件: {full_path}")
                        return
                    if item.get("is_dir"):
                        yield event.plain_result("❌ 无法预览目录，请指定一个文件。")
                        return

                file_name = item.get("name", "")
                file_size = item.get("size", 0)
                ext = os.path.splitext(file_name)[1].lower()
                
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
                
                # 获取下载链接
                download_url = await client.get_download_url(full_path)
                if not download_url:
                    yield event.plain_result("❌ 获取下载链接失败")
                    return

                # 下载到临时目录
                temp_dir = os.path.join(StarTools.get_data_dir("openlist"), "temp_preview")
                os.makedirs(temp_dir, exist_ok=True)
                temp_file_path = os.path.join(temp_dir, f"preview_{int(time.time())}_{file_name}")
                
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(download_url) as resp:
                            if resp.status == 200:
                                with open(temp_file_path, "wb") as f:
                                    f.write(await resp.read())
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
                nav_state = self._get_user_navigation_state(user_id)
                target_dir = nav_state["current_path"]
                target_names = [item["name"]]
                display_name = item["name"]
            else:
                yield event.plain_result(f"❌ 序号 {number} 无效。")
                return
        else:
            # 处理绝对路径
            full_path = path if path.startswith("/") else f"/{path}"
            target_dir = os.path.dirname(full_path)
            target_names = [os.path.basename(full_path)]
            display_name = path

        try:
            async with OpenlistClient(user_config["openlist_url"], user_config.get("public_openlist_url", ""), user_config.get("username", ""), user_config.get("password", ""), user_config.get("token", ""), user_config.get("fixed_base_directory", "")) as client:
                success = await client.remove(target_dir, target_names)
                if success:
                    yield event.plain_result(f"✅ 已删除: {display_name}")
                    
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
        if not name:
            yield event.plain_result("❌ 请提供文件夹名称或路径")
            return
        user_id = event.get_sender_id()
        user_config = self.get_user_config(user_id)
        if not self._validate_config(user_config):
            yield event.plain_result("❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导")
            return

        # 如果不是绝对路径，则在当前目录下创建
        if not name.startswith("/"):
            nav_state = self._get_user_navigation_state(user_id)
            full_path = f"{nav_state['current_path'].rstrip('/')}/{name}"
        else:
            full_path = name

        try:
            async with OpenlistClient(user_config["openlist_url"], user_config.get("public_openlist_url", ""), user_config.get("username", ""), user_config.get("password", ""), user_config.get("token", ""), user_config.get("fixed_base_directory", "")) as client:
                success = await client.mkdir(full_path)
                if success:
                    yield event.plain_result(f"✅ 已创建文件夹: {name}")
                    # 如果在当前目录下创建，刷新列表
                    nav_state = self._get_user_navigation_state(user_id)
                    current_path = nav_state["current_path"]
                    # 检查创建的文件夹是否在当前目录下（直接子目录）
                    if os.path.dirname(full_path) == current_path.rstrip("/") or (current_path == "/" and os.path.dirname(full_path) == "/"):
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

📤 `/ol upload [cancel]`
   - `/ol upload`: 在当前目录开启上传模式。
   - `/ol upload cancel`: 取消上传。
   - `使用`: 开启后，直接向机器人发送文件或图片即可。

📦 `/ol backup [/目标路径] [@群号]`
   - 将指定群聊的所有文件递归备份到 Openlist。
   - 示例: `/ol backup /群备份 @123456`
   - 提示: 路径须以 `/` 开头，群号须以 `@` 开头。默认备份当前群到根目录。

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
