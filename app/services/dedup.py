"""同一任务内去重键：规范化路径 + 标题，供落库 UNIQUE 约束使用。"""

import re
from urllib.parse import unquote


def normalize_item_key(seed_path: str, title: str) -> str:
    path = unquote(seed_path.strip().lower().rstrip("/"))
    title_norm = re.sub(r"\s+", "", title.strip().lower())
    return f"{path}::{title_norm}"
