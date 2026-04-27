from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

_CONFIGURED: bool = False


def setup_logging(log_dir: Path, level: str = "INFO", *, when: str = "midnight", backup_count: int = 30) -> Path:
    """初始化全局日志：同时输出到控制台与文件，按天分割。

    - 控制台：stderr
    - 文件：<log_dir>/app.log，滚动后附加日期后缀（如 app.log.2026-04-27）
    - 保留 backup_count 天
    - 幂等：多次调用只配置一次
    """

    global _CONFIGURED
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "app.log"

    if _CONFIGURED:
        return log_file

    root = logging.getLogger()
    root.setLevel(level.upper())

    # 清理已有 handlers，避免 uvicorn --reload 下重复打印
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename=log_file,
        when=when,
        interval=1,
        backupCount=backup_count,
        encoding="utf-8",
        utc=False,
    )
    file_handler.suffix = "%Y-%m-%d"
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # 对齐 uvicorn / fastapi 的日志到同一套 handlers
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        child = logging.getLogger(name)
        child.handlers = []
        child.propagate = True
        child.setLevel(level.upper())

    # 降噪：openai / httpx 默认太细
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.INFO)

    _CONFIGURED = True
    logging.getLogger(__name__).info(
        "logging initialized level=%s file=%s rotate=%s backup=%d",
        level.upper(),
        log_file,
        when,
        backup_count,
    )
    return log_file
