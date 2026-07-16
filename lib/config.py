import json
from pathlib import Path
from typing import Dict
from astrbot.api import logger
from astrbot.api.star import StarTools
from astrbot.core.utils.io import ensure_dir


class UserConfigManager:
    """用户配置管理器 - 每个用户独立配置"""

    def __init__(self, plugin_name: str, user_id: str):
        self.plugin_name = plugin_name
        self.user_id = user_id
        self.config_dir = Path(StarTools.get_data_dir(plugin_name)) / "users"
        ensure_dir(self.config_dir)
        self.config_file = self.config_dir / f"{user_id}.json"
        self.default_config = {
            "openlist_url": "",
            "username": "",
            "password": "",
            "public_openlist_url": "",
            "fixed_base_directory": "",
            "max_display_files": 20,
            "allowed_extensions": "",
            "max_preview_size": 0,
            "text_preview_length": 1000,
            "enable_cache": True,
            "cache_duration": 300,
            "max_download_size": 50,
            "max_upload_size": 100,
            "upload_retry_attempts": 3,
            "upload_retry_delay": 5,
            "upload_chunk_size_mb": 4,
            "upload_progress_step_mb": 64,
            "upstream_connect_timeout": 60,
            "upstream_read_timeout": 180,
            "openlist_connect_timeout": 30,
            "openlist_upload_response_timeout": 3000,
            "debug_transfer_logging": False,
            "backup_default_path": "/backup/group_{group_id}",
            "backup_allowed_extensions": "",
            "backup_max_size": 0,
            "backup_skip_existing": True,
            "backup_retry_attempts": 3,
            "setup_completed": False,
        }

    def load_config(self) -> Dict:
        """从本地文件加载用户配置，若文件不存在则返回默认配置"""
        try:
            if self.config_file.exists():
                with open(self.config_file, "r", encoding="utf-8") as f:
                    config = json.load(f)
                config.pop("token", None)
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
            config = dict(config)
            config.pop("token", None)
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
        self.config_dir = Path(StarTools.get_data_dir(plugin_name))
        ensure_dir(self.config_dir)
        self.config_file = self.config_dir / "global_config.json"
        self.default_config = {
            "require_user_auth": False,
            "default_openlist_url": "",
            "public_openlist_url": "",
            "default_username": "",
            "default_password": "",
            "fixed_base_directory": "",
            "max_display_files": 20,
            "allowed_extensions": "",
            "max_preview_size": 0,
            "text_preview_length": 1000,
            "enable_cache": True,
            "cache_duration": 300,
            "max_download_size": 50,
            "max_upload_size": 100,
            "upload_retry_attempts": 3,
            "upload_retry_delay": 5,
            "upload_chunk_size_mb": 4,
            "upload_progress_step_mb": 64,
            "upstream_connect_timeout": 60,
            "upstream_read_timeout": 180,
            "openlist_connect_timeout": 30,
            "openlist_upload_response_timeout": 3000,
            "debug_transfer_logging": False,
            "backup_default_path": "/backup/group_{group_id}",
            "autobackup_groups": [],  # 启用自动备份的群号列表
            "backup_allowed_extensions": "",
            "backup_max_size": 0,
            "backup_skip_existing": True,
            "backup_retry_attempts": 3,
            "backup_retry_delay": 5,
        }

    def load_config(self) -> Dict:
        """从本地文件加载全局配置，若文件不存在则返回默认配置"""
        try:
            if self.config_file.exists():
                with open(self.config_file, "r", encoding="utf-8") as f:
                    config = json.load(f)
                config.pop("token", None)
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
            config = dict(config)
            config.pop("token", None)
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存全局配置失败: {e}")
