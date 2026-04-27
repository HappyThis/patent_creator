from __future__ import annotations

from datetime import datetime
from uuid import uuid4


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def generate_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:8]}"
