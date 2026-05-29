from typing import Dict, List

from .helpers import normalize_openlist_path, resolve_openlist_path


def resolve_target_path(current_path: str, path: str, default_to_current: bool = True) -> str:
    raw_path = (path or "").strip()
    normalized_current = current_path if isinstance(current_path, str) and current_path.startswith("/") else "/"
    if not raw_path:
        if default_to_current:
            return normalize_openlist_path(normalized_current)
        return "/"
    return resolve_openlist_path(normalized_current, raw_path)


def resolve_path_candidates(current_path: str, path: str, default_to_current: bool = True) -> List[str]:
    raw_path = (path or "").strip()
    primary_path = resolve_target_path(current_path, raw_path, default_to_current=default_to_current)
    candidates = [primary_path]
    if raw_path and not raw_path.startswith("/"):
        root_path = normalize_openlist_path(raw_path)
        if root_path not in candidates:
            candidates.append(root_path)
    return candidates


def strip_fixed_base_directory(path: str, fixed_base_directory: str) -> str:
    normalized_path = normalize_openlist_path(path)
    fixed_base_dir = normalize_openlist_path(fixed_base_directory)
    if fixed_base_dir != "/" and (
        normalized_path == fixed_base_dir or normalized_path.startswith(fixed_base_dir + "/")
    ):
        normalized_path = normalized_path[len(fixed_base_dir):]
        if not normalized_path:
            return "/"
        if not normalized_path.startswith("/"):
            normalized_path = "/" + normalized_path
    return normalize_openlist_path(normalized_path)


def get_item_full_path(item: Dict, current_path: str, fixed_base_directory: str = "") -> str:
    item_name = item.get("name", "")
    parent_path = item.get("parent")
    if parent_path:
        parent_path = strip_fixed_base_directory(parent_path, fixed_base_directory)
        return normalize_openlist_path(f"{parent_path.rstrip('/')}/{item_name}")

    normalized_current = current_path if isinstance(current_path, str) and current_path.startswith("/") else "/"
    return normalize_openlist_path(f"{normalized_current.rstrip('/')}/{item_name}")
