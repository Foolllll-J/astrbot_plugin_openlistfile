from pathlib import Path
from typing import Dict, List


LEGACY_ALLOWED_EXTENSIONS = {
    ".txt", ".pdf", ".doc", ".docx", ".zip", ".rar", ".jpg", ".png", ".gif", ".mp4", ".mp3"
}


def get_size_limit_mb(user_config: Dict, key: str, default: int, logger=None) -> int:
    try:
        value = int(user_config.get(key, default))
    except (TypeError, ValueError):
        if logger:
            logger.warning(f"配置 {key} 的值无效: {user_config.get(key)!r}，已使用默认值 {default}MB")
        return default
    if value < 0:
        if logger:
            logger.warning(f"配置 {key} 的值不能为负数: {value}，已使用默认值 {default}MB")
        return default
    return value


def get_cache_duration_seconds(user_config: Dict, logger=None) -> int:
    try:
        duration = int(user_config.get("cache_duration", 300))
    except (TypeError, ValueError):
        if logger:
            logger.warning(f"配置 cache_duration 的值无效: {user_config.get('cache_duration')!r}，已使用默认值 300 秒")
        return 300
    if duration < 1:
        if logger:
            logger.warning(f"配置 cache_duration 的值过小: {duration}，已使用默认值 300 秒")
        return 300
    return duration


def get_positive_int_config(user_config: Dict, key: str, default: int, minimum: int = 1, logger=None) -> int:
    try:
        value = int(user_config.get(key, default))
    except (TypeError, ValueError):
        if logger:
            logger.warning(f"配置 {key} 的值无效: {user_config.get(key)!r}，已使用默认值 {default}")
        return default
    if value < minimum:
        if logger:
            logger.warning(f"配置 {key} 的值过小: {value}，已使用默认值 {default}")
        return default
    return value


def get_bool_config(user_config: Dict, key: str, default: bool = False) -> bool:
    value = user_config.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


def build_transfer_config(user_config: Dict, logger=None) -> Dict:
    mb = 1024 * 1024
    return {
        "upload_chunk_size": get_positive_int_config(user_config, "upload_chunk_size_mb", 4, logger=logger) * mb,
        "upload_progress_step": get_positive_int_config(user_config, "upload_progress_step_mb", 64, logger=logger) * mb,
        "upstream_connect_timeout": get_positive_int_config(user_config, "upstream_connect_timeout", 60, logger=logger),
        "upstream_read_timeout": get_positive_int_config(user_config, "upstream_read_timeout", 180, logger=logger),
        "openlist_connect_timeout": get_positive_int_config(user_config, "openlist_connect_timeout", 30, logger=logger),
        "openlist_upload_response_timeout": get_positive_int_config(
            user_config, "openlist_upload_response_timeout", 3000, logger=logger
        ),
        "debug_transfer_logging": get_bool_config(user_config, "debug_transfer_logging", False),
    }


def get_extension_filter(user_config: Dict, key: str = "allowed_extensions") -> List[str]:
    value = user_config.get(key, [])
    if isinstance(value, str):
        extensions = [ext.strip().lower() for ext in value.split(",") if ext.strip()]
    elif isinstance(value, list):
        extensions = [str(ext).strip().lower() for ext in value if str(ext).strip()]
    else:
        return []
    extensions = [ext if ext.startswith(".") else f".{ext}" for ext in extensions]
    if key == "allowed_extensions" and set(extensions) == LEGACY_ALLOWED_EXTENSIONS:
        return []
    return extensions


def is_extension_allowed(filename: str, user_config: Dict, key: str = "allowed_extensions") -> bool:
    allowed_exts = get_extension_filter(user_config, key)
    if not allowed_exts:
        return True
    return Path((filename or "").lower()).suffix in allowed_exts


def format_extension_filter(user_config: Dict, key: str = "allowed_extensions") -> str:
    allowed_exts = get_extension_filter(user_config, key)
    return ", ".join(ext.lstrip(".") for ext in allowed_exts) if allowed_exts else "不限制"
