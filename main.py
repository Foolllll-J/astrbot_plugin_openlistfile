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
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Plain, Image, File
from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.api.event.filter import CustomFilter
from astrbot.core.config import AstrBotConfig


class OpenlistUploadFilter(CustomFilter):
    """文件上传自定义过滤器 - 处理包含文件或图片的消息"""

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        """检查消息是否包含文件或图片组件"""
        messages = event.get_messages()
        file_components = [msg for msg in messages if isinstance(msg, (File, Image))]
        return len(file_components) > 0


class OpenlistClient:
    """Openlist API 客户端"""

    def __init__(
        self, base_url: str, username: str = "", password: str = "", token: str = "",fixed_base_directory: str = ""
    ):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.token = token
        self.fixed_base_directory = fixed_base_directory
        self.session = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        if not self.token and self.username and self.password:
            await self.login()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def login(self) -> bool:
        """登录获取token
        
        发送登录请求到Openlist API，获取认证令牌
        
        Returns:
            bool: 登录成功返回True，失败返回False
        """
        try:
            login_data = {"username": self.username, "password": self.password}

            async with self.session.post(
                f"{self.base_url}/api/auth/login", json=login_data
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if result.get("code") == 200:
                        self.token = result.get("data", {}).get("token", "")
                        return True
                return False
        except Exception as e:
            logger.error(f"OpenList登录失败: {e}")
            return False

    async def list_files(
        self, path: str = "/", page: int = 1, per_page: int = 30
    ) -> Optional[Dict]:
        """获取文件列表
        
        获取指定路径下的文件和目录列表，支持分页
        
        Args:
            path: 目标路径，默认为根目录
            page: 页码，默认为第1页
            per_page: 每页显示数量，默认为30条
        
        Returns:
            Optional[Dict]: 成功返回文件列表数据，失败返回None
        """
        try:
            headers = {}
            if self.token:
                headers["Authorization"] = self.token

            list_data = {
                "path": path,
                "password": "",
                "page": page,
                "per_page": per_page,
                "refresh": False,
            }

            async with self.session.post(
                f"{self.base_url}/api/fs/list", json=list_data, headers=headers
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if result.get("code") == 200:
                        return result.get("data")
                return None
        except Exception as e:
            logger.error(f"获取文件列表失败: {e}")
            return None

    async def get_file_info(self, path: str) -> Optional[Dict]:
        """获取文件信息
        
        获取指定路径下文件或目录的详细信息
        
        Args:
            path: 目标文件或目录路径
        
        Returns:
            Optional[Dict]: 成功返回文件信息，失败返回None
        """
        try:
            headers = {}
            if self.token:
                headers["Authorization"] = self.token

            get_data = {"path": path, "password": ""}

            async with self.session.post(
                f"{self.base_url}/api/fs/get", json=get_data, headers=headers
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if result.get("code") == 200:
                        return result.get("data")
                return None
        except Exception as e:
            logger.error(f"获取文件信息失败: {e}")
            return None

    async def search_files(self, keyword: str, path: str = "/") -> Optional[List[Dict]]:
        """搜索文件
        
        在指定路径下搜索包含关键词的文件
        
        Args:
            keyword: 搜索关键词
            path: 搜索范围路径，默认为根目录
        
        Returns:
            Optional[List[Dict]]: 成功返回搜索结果列表，失败返回空列表
        """
        try:
            headers = {}
            if self.token:
                headers["Authorization"] = self.token

            search_data = {
                "parent": path,
                "keywords": keyword,
                "scope": 0,  # 0: 当前目录及子目录
                "page": 1,
                "per_page": 100,
            }

            async with self.session.post(
                f"{self.base_url}/api/fs/search", json=search_data, headers=headers
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if result.get("code") == 200:
                        return result.get("data", {}).get("content", [])
                return []
        except Exception as e:
            logger.error(f"搜索文件失败: {e}")
            return []

    async def get_download_url(self, path: str) -> Optional[str]:
        """获取文件下载链接
        
        生成可直接下载文件的链接，支持带签名和不带签名的情况
        
        Args:
            path: 目标文件路径
        
        Returns:
            Optional[str]: 成功返回下载链接，失败返回None
        """

        file_info = await self.get_file_info(path)

        if file_info and not file_info.get("is_dir", True):

            sign = file_info.get("sign")
            
            if self.fixed_base_directory:
                full_path = f"{self.fixed_base_directory.rstrip('/')}/{path.lstrip('/')}"
            else:
                full_path = path

            encoded_url_path = quote(full_path.encode("utf-8"))

            if not sign:

                logger.warning(
                    f"无法为 {path} 获取签名，可能需要开启 '全部签名' 选项。返回无签名链接。"
                )

                return f"{self.base_url}/d{encoded_url_path}"

            return f"{self.base_url}/d{encoded_url_path}?sign={sign}"

        return None

    async def upload_file(
        self, file_path: str, target_path: str, filename: str = None
    ) -> bool:
        """上传文件到Openlist
        
        将本地文件上传到指定的Openlist路径
        
        Args:
            file_path: 本地文件路径
            target_path: 目标存储路径
            filename: 自定义文件名，默认为原文件名
        
        Returns:
            bool: 上传成功返回True，失败返回False
        """
        try:
            if not os.path.exists(file_path):
                logger.error(f"文件不存在: {file_path}")
                return False

            if filename is None:
                filename = os.path.basename(file_path)

            upload_url = f"{self.base_url}/api/fs/put"

            with open(file_path, "rb") as f:
                file_data = f.read()

            headers = {
                "Content-Type": "application/octet-stream",
                "File-Path": quote(f"{target_path.rstrip('/')}/{filename}", safe="/"),
            }

            if hasattr(self, "token") and self.token:
                headers["Authorization"] = self.token

            async with self.session.put(
                upload_url, data=file_data, headers=headers
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    return result.get("code") == 200
                else:
                    logger.error(f"上传失败，HTTP状态: {response.status}")
                    return False

        except Exception as e:
            logger.error(f"上传文件失败: {e}")
            return False


class UserConfigManager:
    """用户配置管理器 - 每个用户独立配置"""

    def __init__(self, plugin_name: str, user_id: str):
        self.plugin_name = plugin_name
        self.user_id = user_id
        self.config_dir = os.path.join(
            get_astrbot_data_path(), "plugins_data", plugin_name, "users"
        )
        os.makedirs(self.config_dir, exist_ok=True)
        self.config_file = os.path.join(self.config_dir, f"{user_id}.json")
        self.default_config = {
            "openlist_url": "",
            "username": "",
            "password": "",
            "token": "",
            "max_display_files": 20,
            "allowed_extensions": [
                ".txt", ".pdf", ".doc", ".docx", ".zip", ".rar",
                ".jpg", ".png", ".gif", ".mp4", ".mp3",
            ],
            "enable_preview": True,
            "setup_completed": False,
        }

    def load_config(self) -> Dict:
        """加载用户配置
        
        从本地文件加载用户配置，若文件不存在则返回默认配置
        
        Returns:
            Dict: 合并后的用户配置
        """
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, "r", encoding="utf-8") as f:
                    config = json.load(f)
                merged_config = self.default_config.copy()
                merged_config.update(config)
                return merged_config
            return self.default_config.copy()
        except Exception as e:
            logger.error(f"加载用户 {self.user_id} 配置失败: {e}")
            return self.default_config.copy()

    def save_config(self, config: Dict):
        """保存用户配置
        
        将用户配置保存到本地文件
        
        Args:
            config: 要保存的配置字典
        """
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存用户 {self.user_id} 配置失败: {e}")

    def is_configured(self) -> bool:
        """检查用户是否已配置
        
        判断用户是否完成基础配置
        
        Returns:
            bool: 已配置返回True，未配置返回False
        """
        config = self.load_config()
        return config.get("setup_completed", False) and bool(config.get("openlist_url"))


class CacheManager:
    """文件缓存管理器"""

    def __init__(self, plugin_name: str):
        self.plugin_name = plugin_name
        self.cache_dir = os.path.join(
            get_astrbot_data_path(), "plugins_data", plugin_name, "cache"
        )
        os.makedirs(self.cache_dir, exist_ok=True)

    def _get_cache_key(self, url: str, path: str, user_id: str) -> str:
        """生成缓存键
        
        根据URL、路径和用户ID生成唯一缓存键
        
        Args:
            url: Openlist服务器URL
            path: 文件路径
            user_id: 用户ID
        
        Returns:
            str: MD5哈希值作为缓存键
        """
        content = f"{url}:{path}:{user_id}"
        return hashlib.md5(content.encode("utf-8")).hexdigest()

    def _get_cache_file(self, cache_key: str) -> str:
        """获取缓存文件路径
        
        根据缓存键生成缓存文件路径
        
        Args:
            cache_key: 缓存键
        
        Returns:
            str: 缓存文件的完整路径
        """
        return os.path.join(self.cache_dir, f"{cache_key}.json")

    def get_cache(
        self, url: str, path: str, user_id: str, max_age: int = 300
    ) -> Optional[Dict]:
        """获取缓存
        
        从本地获取缓存数据，检查缓存是否过期
        
        Args:
            url: Openlist服务器URL
            path: 文件路径
            user_id: 用户ID
            max_age: 缓存最大有效期（秒），默认为300秒
        
        Returns:
            Optional[Dict]: 缓存数据，过期或不存在返回None
        """
        try:
            cache_key = self._get_cache_key(url, path, user_id)
            cache_file = self._get_cache_file(cache_key)

            if not os.path.exists(cache_file):
                return None

            if time.time() - os.path.getmtime(cache_file) > max_age:
                try:
                    os.remove(cache_file)
                except:
                    pass
                return None

            with open(cache_file, "r", encoding="utf-8") as f:
                cache_data = json.load(f)
                return cache_data.get("data")
        except Exception as e:
            logger.debug(f"读取缓存失败: {e}")
            return None

    def set_cache(self, url: str, path: str, user_id: str, data: Dict):
        """设置缓存
        
        将数据保存到本地缓存
        
        Args:
            url: Openlist服务器URL
            path: 文件路径
            user_id: 用户ID
            data: 要缓存的数据
        """
        try:
            cache_key = self._get_cache_key(url, path, user_id)
            cache_file = self._get_cache_file(cache_key)

            cache_data = {"timestamp": time.time(), "data": data}

            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug(f"写入缓存失败: {e}")

    def clear_cache(self, user_id: str = None):
        """清理缓存"""
        try:
            if user_id:
                # 清理指定用户的缓存
                for filename in os.listdir(self.cache_dir):
                    if filename.endswith(".json"):
                        cache_key = filename[:-5]  # 移除.json
                        # 简单检查缓存键是否包含用户ID（通过MD5不完美但够用）
                        test_key = self._get_cache_key("test", "test", user_id)
                        if user_id in test_key or cache_key.startswith(test_key[:8]):
                            try:
                                os.remove(os.path.join(self.cache_dir, filename))
                            except:
                                pass
            else:
                # 清理所有缓存
                for filename in os.listdir(self.cache_dir):
                    if filename.endswith(".json"):
                        try:
                            os.remove(os.path.join(self.cache_dir, filename))
                        except:
                            pass
        except Exception as e:
            logger.debug(f"清理缓存失败: {e}")


class GlobalConfigManager:
    """全局配置管理器"""

    def __init__(self, plugin_name: str):
        self.config_dir = os.path.join(
            get_astrbot_data_path(), "plugins_data", plugin_name
        )
        os.makedirs(self.config_dir, exist_ok=True)
        self.config_file = os.path.join(self.config_dir, "global_config.json")
        self.default_config = {
            "default_openlist_url": "",
            "max_display_files": 20,
            "allowed_extensions": ".txt,.pdf,.doc,.docx,.zip,.rar,.jpg,.png,.gif,.mp4,.mp3",
            "enable_preview": True,
            "require_user_auth": True,
        }

    def load_config(self) -> Dict:
        """加载全局配置
        
        从本地文件加载全局配置，若文件不存在则返回默认配置
        
        Returns:
            Dict: 合并后的全局配置
        """
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, "r", encoding="utf-8") as f:
                    config = json.load(f)
                merged_config = self.default_config.copy()
                merged_config.update(config)
                return merged_config
            return self.default_config.copy()
        except Exception as e:
            logger.error(f"加载全局配置失败: {e}")
            return self.default_config.copy()

    def save_config(self, config: Dict):
        """保存全局配置
        
        将全局配置保存到本地文件
        
        Args:
            config: 要保存的配置字典
        """
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存全局配置失败: {e}")


@register(
    "openlist",
    "Foolllll",
    "Openlist文件管理插件",
    "1.0.2",
    "https://github.com/AstrBotDevs/astrbot_plugin_openlist",
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
        """获取WebUI配置项
        
        从插件配置中获取WebUI相关配置
        
        Args:
            key: 配置项键名
            default: 默认值
        
        Returns:
            配置项值，不存在时返回默认值
        """
        if self.config:
            return self.config.get("global_settings", {}).get(key, default)
        return default

    async def initialize(self):
        """插件初始化
        
        插件加载时执行的初始化操作
        """
        logger.info("Openlist文件管理插件已加载")
        default_url = self.get_webui_config("default_openlist_url", "")
        require_auth = self.get_webui_config("require_user_auth", True)
        if not default_url and not require_auth:
            logger.warning("Openlist URL未配置，请使用 /ol config 命令配置或在WebUI中配置")

    def get_user_config_manager(self, user_id: str) -> UserConfigManager:
        """获取用户配置管理器
        
        根据用户ID获取对应的配置管理器，不存在时创建
        
        Args:
            user_id: 用户ID
        
        Returns:
            UserConfigManager: 用户配置管理器实例
        """
        if user_id not in self.user_config_managers:
            self.user_config_managers[user_id] = UserConfigManager("openlist", user_id)
        return self.user_config_managers[user_id]

    def get_user_config(self, user_id: str) -> Dict:
        """获取用户配置
        
        获取用户配置，若用户未独立配置则应用全局设置
        
        Args:
            user_id: 用户ID
        
        Returns:
            Dict: 合并后的用户配置
        """
        require_user_auth = self.get_webui_config("require_user_auth", True)
        default_openlist_url = self.get_webui_config("default_openlist_url", "")
        default_username = self.get_webui_config("default_username", "")
        default_password = self.get_webui_config("default_password", "")
        default_token = self.get_webui_config("default_token", "")
        fixed_base_directory = self.get_webui_config("fixed_base_directory", "")
        max_display_files = self.get_webui_config("max_display_files", 20)
        allowed_extensions = self.get_webui_config(
            "allowed_extensions",
            ".txt,.pdf,.doc,.docx,.zip,.rar,.jpg,.png,.gif,.mp4,.mp3",
        )
        enable_preview = self.get_webui_config("enable_preview", True)

        if require_user_auth:
            user_manager = self.get_user_config_manager(user_id)
            user_config = user_manager.load_config()
            if not user_config.get("openlist_url") and default_openlist_url:
                user_config["openlist_url"] = default_openlist_url
            if not user_config.get("username") and default_username:
                user_config["username"] = default_username
            if not user_config.get("password") and default_password:
                user_config["password"] = default_password
            if not user_config.get("token") and default_token:
                user_config["token"] = default_token
            user_config["fixed_base_directory"] = fixed_base_directory
            user_config["max_display_files"] = max_display_files
            user_config["allowed_extensions"] = (
                allowed_extensions.split(",")
                if isinstance(allowed_extensions, str)
                else allowed_extensions
            )
            user_config["enable_preview"] = enable_preview
            return user_config
        else:
            # 未启用用户认证时使用全局配置
            return {
                "openlist_url": default_openlist_url,
                "username": default_username,
                "password": default_password,
                "token": default_token,
                "fixed_base_directory": fixed_base_directory,
                "max_display_files": max_display_files,
                "allowed_extensions": allowed_extensions.split(",")
                if isinstance(allowed_extensions, str)
                else allowed_extensions,
                "enable_preview": enable_preview,
            }

    def _validate_config(self, user_config: Dict) -> bool:
        """验证配置是否有效
        
        检查Openlist URL是否配置
        
        Args:
            user_config: 用户配置字典
        
        Returns:
            bool: 配置有效返回True，否则返回False
        """
        return bool(user_config.get("openlist_url"))

    def _get_user_navigation_state(self, user_id: str) -> Dict:
        """获取用户导航状态
        
        获取用户当前导航状态，不存在时初始化
        
        Args:
            user_id: 用户ID
        
        Returns:
            Dict: 用户导航状态字典
        """
        if user_id not in self.user_navigation_state:
            self.user_navigation_state[user_id] = {
                "current_path": "/",
                "items": [],
                "parent_paths": [],
                "current_page": 1,
            }
        return self.user_navigation_state[user_id]

    def _update_user_navigation_state(self, user_id: str, path: str, items: List[Dict]):
        """更新用户导航状态
        
        更新用户当前浏览路径和文件列表
        
        Args:
            user_id: 用户ID
            path: 新路径
            items: 文件列表
        """
        nav_state = self._get_user_navigation_state(user_id)
        if path != nav_state["current_path"]:
            if self._is_forward_navigation(nav_state["current_path"], path):
                nav_state["parent_paths"].append(nav_state["current_path"])
            nav_state["current_path"] = path
            nav_state["current_page"] = 1
        nav_state["items"] = items

    def _is_forward_navigation(self, current_path: str, new_path: str) -> bool:
        """判断是否是前进导航
        
        判断是否从当前路径进入子目录
        
        Args:
            current_path: 当前路径
            new_path: 新路径
        
        Returns:
            bool: 是前进导航返回True，否则返回False
        """
        current = current_path.rstrip("/")
        new = new_path.rstrip("/")
        return new.startswith(current + "/") if current != "/" else new.startswith("/")

    def _get_item_by_number(self, user_id: str, number: int) -> Optional[Dict]:
        """根据序号获取文件或目录项
        
        根据列表序号获取对应的文件或目录
        
        Args:
            user_id: 用户ID
            number: 列表序号
        
        Returns:
            Optional[Dict]: 找到返回项目字典，否则返回None
        """
        nav_state = self._get_user_navigation_state(user_id)
        if 1 <= number <= len(nav_state["items"]):
            return nav_state["items"][number - 1]
        return None

    def _get_user_upload_state(self, user_id: str) -> Dict:
        """获取用户上传状态
        
        获取用户上传状态，不存在时初始化
        
        Args:
            user_id: 用户ID
        
        Returns:
            Dict: 用户上传状态字典
        """
        if user_id not in self.user_upload_state:
            self.user_upload_state[user_id] = {"waiting": False, "target_path": "/"}
        return self.user_upload_state[user_id]

    def _set_user_upload_waiting(self, user_id: str, waiting: bool, target_path: str = "/"):
        """设置用户上传等待状态
        
        设置用户是否处于上传等待状态
        
        Args:
            user_id: 用户ID
            waiting: 是否等待上传
            target_path: 上传目标路径
        """
        upload_state = self._get_user_upload_state(user_id)
        upload_state["waiting"] = waiting
        upload_state["target_path"] = target_path

    def _format_file_size(self, size: int) -> str:
        """格式化文件大小
        
        将字节数格式化为易读的文件大小字符串
        
        Args:
            size: 字节数
        
        Returns:
            str: 格式化后的文件大小字符串
        """
        if size < 1024: return f"{size}B"
        elif size < 1024 * 1024: return f"{size / 1024:.1f}KB"
        elif size < 1024 * 1024 * 1024: return f"{size / (1024 * 1024):.1f}MB"
        else: return f"{size / (1024 * 1024 * 1024):.1f}GB"

    def _format_file_list(self, files: List[Dict], current_path: str, user_config: Dict, user_id: str = None) -> str:
        """格式化文件列表
        
        将文件列表格式化为可读的文本消息，支持分页显示
        
        Args:
            files: 文件列表数据
            current_path: 当前路径
            user_config: 用户配置
            user_id: 用户ID（用于获取导航状态）
        
        Returns:
            str: 格式化后的文件列表消息
        """
        if not files: return f"📁 {current_path}\n\n❌ 目录为空"
        nav_state = self._get_user_navigation_state(user_id)
        current_page = nav_state.get("current_page", 1)
        max_files_per_page = user_config.get("max_display_files", 20)
        total_items = len(files)
        total_pages = (total_items + max_files_per_page - 1) // max_files_per_page
        start_index = (current_page - 1) * max_files_per_page
        end_index = start_index + max_files_per_page
        items_to_display = files[start_index:end_index]
        result = f"📁 {current_path}\n\n"
        dirs = [f for f in files if f.get("is_dir", False)]
        files_only = [f for f in files if not f.get("is_dir", False)]
        for i, item in enumerate(items_to_display, start=start_index + 1):
            name = item.get("name", "")
            size = item.get("size", 0)
            modified = item.get("modified", "")
            is_dir = item.get("is_dir", False)
            if modified: modified = modified.split("T")[0]
            if is_dir:
                result += f"{i:2d}. 📂 {name}/\n"
                if modified: result += f"      📅 {modified}\n"
            else:
                ext = os.path.splitext(name)[1].lower()
                if ext in [".jpg", ".jpeg", ".png", ".gif", ".bmp"]: icon = "🖼️"
                elif ext in [".mp4", ".avi", ".mkv", ".mov"]: icon = "🎬"
                elif ext in [".mp3", ".wav", ".flac", ".aac"]: icon = "🎵"
                elif ext in [".pdf"]: icon = "📄"
                elif ext in [".doc", ".docx"]: icon = "📝"
                elif ext in [".zip", ".rar", ".7z"]: icon = "📦"
                else: icon = "📄"
                result += f"{i:2d}. {icon} {name}\n"
                result += f"      💾 {self._format_file_size(size)}"
                if modified: result += f" | 📅 {modified}"
                result += "\n"
        result += f"\n📄 第 {current_page} / {total_pages} 页"
        result += f" | 📊 总计: {len(dirs)} 个目录, {len(files_only)} 个文件"
        result += f"\n\n💡 快速导航:"
        result += f"\n   • /ol ls <序号> - 进入目录/获取链接"
        result += f"\n   • /ol quit - 返回上级目录"
        if total_pages > 1:
            if current_page > 1: result += f"\n   • /ol page prev - ⬅️ 上一页"
            if current_page < total_pages: result += f"\n   • /ol page next - ➡️ 下一页"
        return result

    async def _download_file(self, event: AstrMessageEvent, file_item: Dict, user_config: Dict):
        """下载文件并作为附件发送给用户
        
        下载Openlist文件并通过消息发送给用户，支持大小限制
        
        Args:
            event: 消息事件
            file_item: 文件信息字典
            user_config: 用户配置
        
        Yields:
            MessageEventResult: 消息事件结果
        """
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
            parent_path = file_item.get("parent")
            if parent_path:
                 file_path = f"{parent_path.rstrip('/')}/{file_name}"
            else:
                nav_state = self._get_user_navigation_state(user_id)
                current_path = nav_state["current_path"]
                if current_path.endswith("/"): file_path = f"{current_path}{file_name}"
                else: file_path = f"{current_path}/{file_name}"

            async with OpenlistClient(user_config["openlist_url"], user_config.get("username", ""), user_config.get("password", ""), user_config.get("token", ""), user_config.get("fixed_base_directory", "")) as client:
                download_url = await client.get_download_url(file_path)
                if not download_url:
                    yield event.plain_result("❌ 无法获取下载链接")
                    return
                downloads_dir = os.path.join(get_astrbot_data_path(), "plugins_data", "openlist", "downloads")
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
                            yield event.plain_result(f"❌ 下载失败: HTTP {response.status}")
        except Exception as e:
            logger.error(f"用户 {user_id} 下载文件失败: {e}")
            yield event.plain_result(f"❌ 下载失败: {str(e)}")

    async def _get_and_send_download_link(self, event: AstrMessageEvent, item: Dict, user_config: Dict):
        """获取指定项目的文件链接并发送
        
        生成文件下载链接并通过消息发送给用户
        
        Args:
            event: 消息事件
            item: 文件或目录信息字典
            user_config: 用户配置
        
        Yields:
            MessageEventResult: 消息事件结果
        """
        user_id = event.get_sender_id()
        yield event.plain_result(f"🔗 正在获取文件链接: {item.get('name', '')}...")
        nav_state = self._get_user_navigation_state(user_id)
        file_name = item.get("name", "")
        parent_path = item.get("parent", nav_state.get("current_path", "/"))
        file_path = f"{parent_path.rstrip('/')}/{file_name}"
        try:
            async with OpenlistClient(user_config["openlist_url"], user_config.get("username", ""), user_config.get("password", ""), user_config.get("token", ""), user_config.get("fixed_base_directory", "")) as client:
                download_url = await client.get_download_url(file_path)
                if download_url:
                    name = item.get("name", "")
                    size = item.get("size", 0)
                    result_text = f"📥 下载链接\n\n"
                    result_text += f"📄 文件: {name}\n"
                    result_text += f"💾 大小: {self._format_file_size(size)}\n"
                    result_text += f"🔗 链接: {download_url}\n\n"
                    result_text += "💡 提示: 点击链接即可下载文件"
                    yield event.plain_result(result_text)
                else:
                    yield event.plain_result(f"❌ 无法获取下载链接，文件可能不存在或为目录: {file_path}")
        except Exception as e:
            logger.error(f"用户 {user_id} 获取下载链接失败: {e}")
            yield event.plain_result(f"❌ 操作失败: {str(e)}")

    async def _upload_file(self, event: AstrMessageEvent, file_component: File, user_config: Dict):
        """上传文件到Openlist"""
        user_id = event.get_sender_id()
        upload_state = self._get_user_upload_state(user_id)
        target_path = upload_state["target_path"]
        try:
            file_name = file_component.name
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
            async with OpenlistClient(user_config["openlist_url"], user_config.get("username", ""), user_config.get("password", ""), user_config.get("token", ""), user_config.get("fixed_base_directory", "")) as client:
                success = await client.upload_file(file_path, target_path, file_name)
                if success:
                    yield event.plain_result(f"✅ 上传成功!\n📄 文件: {file_name}\n📂 路径: {target_path}")
                    self._set_user_upload_waiting(user_id, False)
                    result = await client.list_files(target_path)
                    if result:
                        files = result.get("content", [])
                        formatted_list = self._format_file_list(files, target_path, user_config, user_id)
                        yield event.plain_result(f"📁 当前目录已更新:\n\n{formatted_list}")
                else:
                    yield event.plain_result(f"❌ 上传失败，请检查网络连接和权限")
        except Exception as e:
            logger.error(f"用户 {user_id} 上传文件失败: {e}")
            yield event.plain_result(f"❌ 上传失败: {str(e)}")
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
            async with OpenlistClient(user_config["openlist_url"], user_config.get("username", ""), user_config.get("password", ""), user_config.get("token", ""), user_config.get("fixed_base_directory", "")) as client:
                success = await client.upload_file(image_path, target_path, filename)
                if success:
                    yield event.plain_result(f"✅ 图片上传成功!\n📄 文件: {filename}\n📂 路径: {target_path}")
                    self._set_user_upload_waiting(user_id, False)
                    result = await client.list_files(target_path)
                    if result:
                        files = result.get("content", [])
                        formatted_list = self._format_file_list(files, target_path, user_config, user_id)
                        yield event.plain_result(f"📁 当前目录已更新:\n\n{formatted_list}")
                else:
                    yield event.plain_result(f"❌ 上传失败，请检查网络连接和权限")
        except Exception as e:
            logger.error(f"用户 {user_id} 上传图片失败: {e}")
            yield event.plain_result(f"❌ 上传失败: {str(e)}")
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
            valid_keys = ["openlist_url", "username", "password", "token", "max_display_files"]
            if key not in valid_keys:
                yield event.plain_result(f"❌ 未知的配置项: {key}。可用配置项: {', '.join(valid_keys)}")
                return
            if key == "max_display_files":
                try:
                    value = int(value)
                    if value < 1 or value > 100:
                        yield event.plain_result("❌ max_display_files 必须在1-100之间")
                        return
                except ValueError:
                    yield event.plain_result("❌ max_display_files 必须是数字")
                    return
            user_config[key] = value
            if key == "openlist_url" and value:
                user_config["setup_completed"] = True
            user_manager.save_config(user_config)
            yield event.plain_result(f"✅ 已为用户 {event.get_sender_name()} 设置 {key} = {value}")
        elif action == "test":
            user_config = self.get_user_config(user_id)
            if not self._validate_config(user_config):
                yield event.plain_result("❌ 请先配置Openlist URL\n💡 使用 /ol config setup 开始配置向导")
                return
            try:
                async with OpenlistClient(user_config["openlist_url"], user_config.get("username", ""), user_config.get("password", ""), user_config.get("token", ""), user_config.get("fixed_base_directory", "")) as client:
                    files = await client.list_files("/")
                    if files is not None:
                        yield event.plain_result("✅ Openlist连接测试成功!")
                    else:
                        yield event.plain_result("❌ Openlist连接失败，请检查配置")
            except Exception as e:
                yield event.plain_result(f"❌ 连接测试失败: {str(e)}")
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
            async with OpenlistClient(user_config["openlist_url"], user_config.get("username", ""), user_config.get("password", ""), user_config.get("token", ""), user_config.get("fixed_base_directory", "")) as client:
                file_info = await client.get_file_info(target_path)
                if file_info and not file_info.get("is_dir", False):
                    async for result in self._get_and_send_download_link(event, file_info, user_config):
                        yield result
                    return
                list_result = await client.list_files(target_path, per_page=0)
                if list_result is not None:
                    files = list_result.get("content", [])
                    self._update_user_navigation_state(user_id, target_path, files)
                    formatted_list = self._format_file_list(files, target_path, user_config, user_id)
                    yield event.plain_result(formatted_list)
                else:
                    yield event.plain_result(f"❌ 无法访问路径: {target_path}")
        except Exception as e:
            logger.error(f"用户 {user_id} 列出文件失败: {e}")
            yield event.plain_result(f"❌ 操作失败: {str(e)}")

    @openlist_group.command("page")
    async def page_command(self, event: AstrMessageEvent, action: str = "next"):
        """在文件列表中进行翻页"""
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

        if action == "next":
            if current_page < total_pages:
                nav_state["current_page"] += 1
            else:
                yield event.plain_result("➡️ 已經是最後一頁了。")
                return
        elif action == "prev":
            if current_page > 1:
                nav_state["current_page"] -= 1
            else:
                yield event.plain_result("⬅️ 已經是第一頁了。")
                return
        else:
            yield event.plain_result(f"❌ 未知的翻頁操作: {action}。請使用 'next' 或 'prev'。")
            return
            
        # 重新格式化並顯示新頁面的列表
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
            async with OpenlistClient(user_config["openlist_url"], user_config.get("username", ""), user_config.get("password", ""), user_config.get("token", ""), user_config.get("fixed_base_directory", "")) as client:
                files = await client.search_files(keyword, path)
                if files:
                    max_files = user_config.get("max_display_files", 20)
                    result = f"🔍 搜索结果 (关键词: {keyword})\n搜索路径: {path}\n\n"
                    for i, file_item in enumerate(files[:max_files], 1):
                        name = file_item.get("name", "")
                        parent = file_item.get("parent", "")
                        size = file_item.get("size", 0)
                        is_dir = file_item.get("is_dir", False)
                        icon = "📂" if is_dir else "📄"
                        result += f"{i}. {icon} {name}\n"
                        result += f"   📍 {parent}\n"
                        if not is_dir: result += f"   💾 {self._format_file_size(size)}\n"
                        result += "\n"
                    if len(files) > max_files:
                        result += f"... 还有 {len(files) - max_files} 个结果未显示"
                    yield event.plain_result(result)
                else:
                    yield event.plain_result(f"🔍 未找到包含 '{keyword}' 的文件")
        except Exception as e:
            logger.error(f"用户 {user_id} 搜索文件失败: {e}")
            yield event.plain_result(f"❌ 搜索失败: {str(e)}")

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
            async with OpenlistClient(user_config["openlist_url"], user_config.get("username", ""), user_config.get("password", ""), user_config.get("token", ""), user_config.get("fixed_base_directory", "")) as client:
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
                    yield event.plain_result(f"❌ 文件不存在: {path}")
        except Exception as e:
            logger.error(f"用户 {user_id} 获取文件信息失败: {e}")
            yield event.plain_result(f"❌ 操作失败: {str(e)}")

# (替换原有的 get_download_link 函数)
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
                async with OpenlistClient(user_config["openlist_url"], user_config.get("username", ""), user_config.get("password", ""), user_config.get("token", ""), user_config.get("fixed_base_directory", "")) as client:
                    file_info = await client.get_file_info(path)
                    if file_info and not file_info.get("is_dir", False):
                        item_to_download = file_info
                    else:
                        yield event.plain_result(f"❌ 无法下载，文件不存在或路径为目录: {path}")
                        return
            except Exception as e:
                logger.error(f"用户 {user_id} 获取文件信息失败: {e}")
                yield event.plain_result(f"❌ 操作失败: {str(e)}")
                return
        
        if item_to_download:
            yield event.plain_result(f"📥 正在准备下载文件: {item_to_download.get('name', '')}...")
            async for result in self._download_file(event, item_to_download, user_config):
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
            async with OpenlistClient(user_config["openlist_url"], user_config.get("username", ""), user_config.get("password", ""), user_config.get("token", ""), user_config.get("fixed_base_directory", "")) as client:
                result = await client.list_files(previous_path)
                if result is not None:
                    files = result.get("content", [])
                    nav_state["current_path"] = previous_path
                    nav_state["items"] = files[: self.get_webui_config("max_display_files", 20)]
                    formatted_list = self._format_file_list(files, previous_path, user_config, user_id)
                    yield event.plain_result(f"⬅️ 已返回上级目录\n\n{formatted_list}")
                else:
                    yield event.plain_result(f"❌ 无法访问上级目录: {previous_path}")
        except Exception as e:
            logger.error(f"用户 {user_id} 回退目录失败: {e}")
            yield event.plain_result(f"❌ 回退失败: {str(e)}")

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
        """处理文件消息
        
        当用户发送文件或图片时自动触发的处理逻辑
        
        Args:
            event: 消息事件
        
        Yields:
            MessageEventResult: 消息事件结果
        """
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
        
        help_text = f"""📚 Openlist 文件管理插件 v1.0.2 帮助

---
**核心导航指令**
---
▶️ `/ol ls [路径|序号]`
   - **浏览目录**: 列出内容，若文件过多会自动分页。
     - 示例: `/ol ls` 或 `/ol ls /movies`
   - **进入子目录**:
     - 示例: `/ol ls 1` (如果1是目录)
   - **获取链接**: 获取文件的下载链接。
     - 示例: `/ol ls 2` (如果2是文件)

▶️ `/ol page <next|prev>`
   - 在 `ls` 的文件列表中进行翻页。
     - 示例: `/ol page next` (下一页)

▶️ `/ol quit`
   - 返回到上级目录。

---
**文件操作指令**
---
📥 `/ol download <路径|序号>`
   - **直接下载**: 将文件作为附件发送给您。
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
**插件配置指令**
---
⚙️ `/ol config setup` - **推荐新用户使用**，启动交互式配置向导。
⚙️ `/ol config show` - 显示您当前的配置。
⚙️ `/ol config set <键> <值>` - 修改配置项。
⚙️ `/ol config test` - 测试与服务器的连接。
⚙️ `/ol config clear_cache` - 清除文件列表缓存。
"""

        if is_user_auth_mode:
            help_text += f"""

👤 **当前模式: 用户独立认证**
   - 每位用户都需要使用 `/ol config setup` 单独配置自己的 Openlist 账户信息。"""

            if not self._validate_config(user_config):
                help_text += f"""

⚠️ **操作提示**
   您尚未完成配置，请发送 `/ol config setup` 开始配置向导。"""
        else:
            help_text += f"""

🌐 **当前模式: 全局共享**
   - 所有用户共享管理员预设的 Openlist 服务器连接，无需单独配置。"""

        help_text += f"""

💡 **通用提示**:
1.  路径区分大小写，以 `/` 开头表示根目录。
2.  `ls` 获取链接，`download` 直接发送文件。
3.  管理员可在机器人后台的插件配置页面调整全局设置。"""
        
        yield event.plain_result(help_text)

    async def terminate(self):
        """插件卸载时执行的清理操作"""
        logger.info("Openlist文件管理插件已卸载")
