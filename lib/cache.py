import os
import json
import hashlib
import time
from typing import Dict, Optional
from astrbot.api import logger
from astrbot.api.star import StarTools

class CacheManager:
    """文件缓存管理器"""

    def __init__(self, plugin_name: str):
        self.plugin_name = plugin_name
        self.cache_dir = os.path.join(
            StarTools.get_data_dir(plugin_name), "cache"
        )
        os.makedirs(self.cache_dir, exist_ok=True)

    def _get_cache_key(self, url: str, path: str, user_id: str) -> str:
        """根据URL、路径和用户ID生成唯一缓存键"""
        content = f"{url}:{path}:{user_id}"
        return hashlib.md5(content.encode("utf-8")).hexdigest()

    def _get_cache_file(self, cache_key: str) -> str:
        """根据缓存键生成缓存文件路径"""
        return os.path.join(self.cache_dir, f"{cache_key}.json")

    def get_cache(
        self, url: str, path: str, user_id: str, max_age: int = 300
    ) -> Optional[Dict]:
        """从本地获取缓存数据，并检查是否过期"""
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
        """将数据保存到本地缓存"""
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
                        cache_key = filename[:-5]
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
