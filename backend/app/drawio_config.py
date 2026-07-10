from __future__ import annotations

import ipaddress
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DEFAULT_DRAWIO_EMBED_URL = "http://127.0.0.1:8081/"


def normalize_drawio_embed_url(value: str, *, allow_nonlocal: bool) -> str:
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("PATENT_CREATOR_DRAWIO_EMBED_URL 必须是有效的 http/https URL。")
    if not allow_nonlocal and not _is_local_hostname(parsed.hostname):
        raise ValueError(
            "非本机 Draw.io 服务需要显式设置 PATENT_CREATOR_ALLOW_NONLOCAL_DRAWIO=true。"
        )

    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    defaults = {
        "offline": "1",
        "embed": "1",
        "proto": "json",
        "spin": "1",
        "ui": "min",
        "lang": "zh",
        "libraries": "1",
        "noExitBtn": "1",
        "noSaveBtn": "1",
        "saveAndExit": "0",
    }
    if parsed.scheme == "http":
        defaults["https"] = "0"
    for key, default in defaults.items():
        query.setdefault(key, default)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", urlencode(query), ""))


def _is_local_hostname(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False
