import os
import json
from typing import Dict
from astrbot.api import logger
from astrbot.api.star import StarTools

class UserConfigManager:
    """用户配置管理器 - 每个用户独立配置"""

    def __init__(self, plugin_name: str, user_id: str):
        self.plugin_name = plugin_name
        self.user_id = user_id
        self.config_dir = os.path.join(
            StarTools.get_data_dir(plugin_name), "users"
        )
        os.makedirs(self.config_dir, exist_ok=True)
        self.config_file = os.path.join(self.config_dir, f"{user_id}.json")
        self.default_config = {
            "openlist_url": "",
            "username": "",
            "password": "",
            "token": "",
            "public_openlist_url": "",
            "fixed_base_directory": "",
            "max_display_files": 20,
            "allowed_extensions": [
                ".txt", ".pdf", ".doc", ".docx", ".zip", ".rar",
                ".jpg", ".png", ".gif", ".mp4", ".mp3",
            ],
            "enable_preview": True,
            "enable_cache": True,
            "cache_duration": 300,
            "setup_completed": False,
        }

    def load_config(self) -> Dict:
        """从本地文件加载用户配置，若文件不存在则返回默认配置"""
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
        """将用户配置保存到本地文件"""
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存用户 {self.user_id} 配置失败: {e}")

    def is_configured(self) -> bool:
        """检查用户是否完成基础配置"""
        config = self.load_config()
        return config.get("setup_completed", False) and bool(config.get("openlist_url"))


class GlobalConfigManager:
    """全局配置管理器"""

    def __init__(self, plugin_name: str):
        self.config_dir = StarTools.get_data_dir(plugin_name)
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
        """从本地文件加载全局配置，若文件不存在则返回默认配置"""
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
        """将全局配置保存到本地文件"""
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存全局配置失败: {e}")
