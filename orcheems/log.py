"""
Colored logging cho everyflow-automation.
Không cần thư viện ngoài — dùng ANSI escape codes thuần.

Usage:
    # main.py hoặc bất kỳ entry point nào
    from core.logging_config import setup_logging

    setup_logging()           # mặc định: INFO, màu bật nếu terminal hỗ trợ
    setup_logging(level="DEBUG")
    setup_logging(level="DEBUG", force_color=True)   # force màu dù pipe/redirect
    setup_logging(json=True)                          # JSON mode cho production/k8s
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Literal, Optional


# ──────────────────────────────────────────────
# ANSI color codes
# ──────────────────────────────────────────────
class _C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"

    # text colors
    WHITE  = "\033[97m"
    GRAY   = "\033[90m"

    BLUE   = "\033[34m"
    CYAN   = "\033[36m"
    GREEN  = "\033[32m"
    YELLOW = "\033[33m"
    RED    = "\033[31m"
    PURPLE = "\033[35m"

    # bright variants
    B_GREEN  = "\033[92m"
    B_YELLOW = "\033[93m"
    B_RED    = "\033[91m"
    B_CYAN   = "\033[96m"
    B_BLUE   = "\033[94m"


_LEVEL_STYLE: dict[int, tuple[str, str]] = {
    logging.DEBUG:    (_C.PURPLE,   "DEBUG  "),
    logging.INFO:     (_C.B_GREEN,  "INFO   "),
    logging.WARNING:  (_C.B_YELLOW, "WARNING"),
    logging.ERROR:    (_C.B_RED,    "ERROR  "),
    logging.CRITICAL: (_C.B_RED,    "CRITICAL"),
}


# ──────────────────────────────────────────────
# Colored formatter
# ──────────────────────────────────────────────
class ColoredFormatter(logging.Formatter):
    """
    Format:
        10:42 24/06/26 │ INFO    │ module_name          │ message  key=value
    """

    MOD_WIDTH  = 22
    SEP        = f"{_C.GRAY} │ {_C.RESET}"

    def __init__(self, use_color: bool = True) -> None:
        super().__init__()
        self.use_color = use_color

    def _c(self, code: str, text: str) -> str:
        if not self.use_color:
            return text
        return f"{code}{text}{_C.RESET}"

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now(timezone.utc).strftime("%H:%M %d/%m/%y")
        ts_str = self._c(_C.BLUE, ts)

        level_color, level_label = _LEVEL_STYLE.get(
            record.levelno, (_C.WHITE, record.levelname[:7].ljust(7))
        )
        level_str = self._c(level_color, level_label)

        # module name: dùng tên logger, truncate + pad
        mod_raw = record.name.split(".")[-1]           # lấy phần cuối  e.g. "manager"
        mod_padded = mod_raw[:self.MOD_WIDTH].ljust(self.MOD_WIDTH)
        mod_str = self._c(_C.CYAN, mod_padded)

        # message
        msg = record.getMessage()
        msg_str = self._c(_C.WHITE, msg)

        # extra key=value pairs được attach qua logger.info("...", extra={...})
        # hoặc qua LogRecord.xxx attrs đặt thủ công
        extras = self._format_extras(record)

        sep = self.SEP
        line = f"{ts_str}{sep}{level_str}{sep}{mod_str}{sep}{msg_str}{extras}"

        # exception traceback (nếu có)
        if record.exc_info:
            exc = self.formatException(record.exc_info)
            line = f"{line}\n{self._c(_C.GRAY, exc)}"

        return line

    def _format_extras(self, record: logging.LogRecord) -> str:
        """
        Thu thập các attr không thuộc LogRecord chuẩn để in dạng  key=value.

        Cách dùng:
            logger.info("session locked", extra={"credential_id": "abc-123", "seconds": 3})
        """
        SKIP = {
            "name", "msg", "args", "levelname", "levelno", "pathname",
            "filename", "module", "exc_info", "exc_text", "stack_info",
            "lineno", "funcName", "created", "msecs", "relativeCreated",
            "thread", "threadName", "processName", "process", "message",
            "taskName",
        }
        parts: list[str] = []
        for k, v in record.__dict__.items():
            if k.startswith("_") or k in SKIP:
                continue
            key_str   = self._c(_C.B_CYAN,   k)
            if isinstance(v, str):
                val_str = self._c(_C.B_GREEN, f"'{v}'")
            elif isinstance(v, (int, float)):
                val_str = self._c(_C.YELLOW, str(v))
            else:
                val_str = self._c(_C.PURPLE, repr(v))
            parts.append(f"{key_str}={val_str}")

        return ("  " + "  ".join(parts)) if parts else ""


# ──────────────────────────────────────────────
# JSON formatter (production / k8s / Graylog)
# ──────────────────────────────────────────────
class JsonFormatter(logging.Formatter):
    """Structured JSON — 1 dòng/record, dễ ingest vào Graylog / Loki."""

    SKIP = {
        "name", "msg", "args", "levelname", "levelno", "pathname",
        "filename", "module", "exc_info", "exc_text", "stack_info",
        "lineno", "funcName", "created", "msecs", "relativeCreated",
        "thread", "threadName", "processName", "process", "message",
        "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts":      datetime.now(timezone.utc).isoformat(),
            "level":   record.levelname,
            "logger":  record.name,
            "message": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if not k.startswith("_") and k not in self.SKIP:
                payload[k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


# ──────────────────────────────────────────────
# Setup helper
# ──────────────────────────────────────────────
def _supports_color() -> bool:
    """True nếu stdout là terminal thật và không bị force-disable."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def setup_logging(
    level: str = "INFO",
    json: bool = False,
    force_color: Optional[bool] = None,
    loggers: Optional[list[str]] = None,
) -> None:
    """
    Cấu hình root logger (và tuỳ chọn một số logger cụ thể).

    Args:
        level       : Log level — "DEBUG" | "INFO" | "WARNING" | "ERROR"
        json        : True → dùng JsonFormatter (production/k8s)
        force_color : None = auto-detect, True = bật, False = tắt
        loggers     : Danh sách tên logger muốn set riêng level DEBUG,
                      dù root logger đang ở INFO.
                      Ví dụ: ["task.app_operator", "session.manager"]
    
    Usage:
        # development
        setup_logging(level="DEBUG")

        # production / k8s
        setup_logging(json=True)

        # chỉ debug 2 module cụ thể
        setup_logging(level="INFO", loggers=["session.manager", "task.base_task"])
    """
    use_color = force_color if force_color is not None else _supports_color()

    if json:
        formatter: logging.Formatter = JsonFormatter()
    else:
        formatter = ColoredFormatter(use_color=use_color)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Tắt bớt noise từ thư viện bên ngoài
    for noisy in ("httpx", "httpcore", "uvicorn.access", "playwright"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Sub-logger override
    if loggers:
        for name in loggers:
            logging.getLogger(name).setLevel(logging.DEBUG)