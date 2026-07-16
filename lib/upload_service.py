from typing import Dict, Iterable, List, Optional, Tuple

from .helpers import build_upload_session_key


def get_upload_state_key(user_id: str, group_id: Optional[str] = None) -> str:
    return build_upload_session_key(user_id, group_id or "")


def get_user_upload_state(store: Dict, state_key: str) -> Dict:
    if state_key not in store:
        store[state_key] = {"waiting": False, "target_path": "/", "uploaded_files": []}
    return store[state_key]


def set_user_upload_waiting(
    store: Dict, state_key: str, waiting: bool, target_path: str = "/"
) -> Dict:
    state = get_user_upload_state(store, state_key)
    state["waiting"] = waiting
    state["target_path"] = target_path
    if waiting:
        state["uploaded_files"] = []
    return state


def extract_quoted_upload_components(
    messages: Iterable,
    reply_type,
    component_types: Tuple[type, ...],
) -> List:
    upload_components = []
    for message in messages:
        if not isinstance(message, reply_type):
            continue
        chain = getattr(message, "chain", None)
        if not chain:
            continue
        for reply_comp in chain:
            if isinstance(reply_comp, component_types):
                upload_components.append(reply_comp)
    return upload_components
