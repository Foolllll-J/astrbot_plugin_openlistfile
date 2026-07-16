import json
import hashlib
import time
from pathlib import Path
from typing import Dict, Optional
from astrbot.api import logger
from astrbot.api.star import StarTools
from astrbot.core.utils.io import ensure_dir


class CacheManager:
    """文件缓存管理器"""

    def __init__(self, plugin_name: str):
        self.plugin_name = plugin_name
        self.cache_dir = Path(StarTools.get_data_dir(plugin_name)) / "cache"
        ensure_dir(self.cache_dir)

    def _get_cache_key(self, url: str, path: str, user_id: str) -> str:
        """根据URL、路径和用户ID生成唯一缓存键"""
        content = f"{url}:{path}:{user_id}"
        return hashlib.md5(content.encode("utf-8")).hexdigest()

    def _get_cache_file(self, cache_key: str) -> Path:
        """根据缓存键生成缓存文件路径"""
        return self.cache_dir / f"{cache_key}.json"

    def get_cache(
        self, url: str, path: str, user_id: str, max_age: int = 300
    ) -> Optional[Dict]:
        """从本地获取缓存数据，并检查是否过期"""
        try:
            cache_key = self._get_cache_key(url, path, user_id)
            cache_file = self._get_cache_file(cache_key)

            if not cache_file.exists():
                return None

            if time.time() - cache_file.stat().st_mtime > max_age:
                try:
                    cache_file.unlink()
                except Exception:
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

            cache_data = {
                "timestamp": time.time(),
                "url": url,
                "path": path,
                "user_id": user_id,
                "data": data,
            }

            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug(f"写入缓存失败: {e}")

    def clear_cache(self, user_id: str = None):
        """清理缓存"""
        try:
            if user_id:
                for entry in self.cache_dir.iterdir():
                    if entry.is_file() and entry.suffix == ".json":
                        try:
                            with open(entry, "r", encoding="utf-8") as f:
                                cache_data = json.load(f)
                            if cache_data.get("user_id") in (user_id, None):
                                entry.unlink()
                        except Exception:
                            try:
                                entry.unlink()
                            except Exception:
                                pass
            else:
                for entry in self.cache_dir.iterdir():
                    if entry.is_file() and entry.suffix == ".json":
                        try:
                            entry.unlink()
                        except Exception:
                            pass
        except Exception as e:
            logger.debug(f"清理缓存失败: {e}")
