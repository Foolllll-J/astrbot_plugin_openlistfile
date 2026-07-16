import posixpath
import shutil
import time
from collections import OrderedDict
from pathlib import Path
from typing import MutableMapping, Optional, Tuple

TEXT_PREVIEW_EXTENSIONS = {
    ".txt",
    ".md",
    ".log",
    ".json",
    ".xml",
    ".yaml",
    ".yml",
    ".ini",
    ".conf",
    ".cfg",
    ".toml",
    ".py",
    ".js",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".go",
    ".rs",
    ".php",
    ".rb",
    ".sh",
    ".bash",
    ".html",
    ".htm",
    ".css",
    ".jsx",
    ".tsx",
    ".ts",
    ".vue",
    ".sql",
    ".csv",
    ".properties",
    ".env",
}


def build_upload_session_key(user_id: str, group_id: str = "") -> str:
    user_id = str(user_id or "")
    group_id = str(group_id or "")
    if group_id:
        return f"group:{group_id}:user:{user_id}"
    return f"private:user:{user_id}"


def normalize_openlist_path(path: str) -> str:
    raw_path = (path or "").strip().replace("\\", "/")
    if not raw_path:
        return "/"
    if not raw_path.startswith("/"):
        raw_path = "/" + raw_path
    normalized = posixpath.normpath(raw_path)
    if normalized in ("", "."):
        return "/"
    if not normalized.startswith("/"):
        normalized = "/" + normalized.lstrip("/")
    return normalized


def resolve_openlist_path(current_path: str, path: str) -> str:
    raw_path = (path or "").strip()
    if not raw_path:
        return normalize_openlist_path(current_path or "/")
    if raw_path.startswith("/"):
        return normalize_openlist_path(raw_path)
    return normalize_openlist_path(
        posixpath.join(normalize_openlist_path(current_path or "/"), raw_path)
    )


def split_openlist_path(path: str) -> Tuple[str, str]:
    normalized = normalize_openlist_path(path)
    if normalized == "/":
        return "/", ""
    parent = posixpath.dirname(normalized) or "/"
    name = posixpath.basename(normalized)
    return parent, name


def size_limit_mb_to_bytes(limit_mb: Optional[int]) -> Optional[int]:
    if limit_mb is None:
        return None
    if int(limit_mb) <= 0:
        return None
    return int(limit_mb) * 1024 * 1024


def normalize_action_payload(result) -> dict:
    if isinstance(result, dict) and isinstance(result.get("data"), dict):
        return result["data"]
    return result if isinstance(result, dict) else {}


def should_download_for_text_preview(file_name: str) -> bool:
    return Path(file_name or "").suffix.lower() in TEXT_PREVIEW_EXTENSIONS


def copy_file_to_temp(
    source_path: str, temp_dir: str, filename: str, prefix: str = ""
) -> str:
    Path(temp_dir).mkdir(parents=True, exist_ok=True)
    source = Path(source_path)
    target_name = f"{prefix}{int(time.time() * 1000)}_{Path(filename).name}"
    target = Path(temp_dir) / target_name
    shutil.copy2(source, target)
    return str(target)


def new_lru_mapping() -> MutableMapping:
    return OrderedDict()


def remember_lru_entry(cache: MutableMapping, key, value, max_entries: int) -> None:
    cache[key] = value
    if hasattr(cache, "move_to_end"):
        cache.move_to_end(key)
    while max_entries > 0 and len(cache) > max_entries:
        if hasattr(cache, "popitem"):
            cache.popitem(last=False)
        else:
            first_key = next(iter(cache))
            del cache[first_key]
