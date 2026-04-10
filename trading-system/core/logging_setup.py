"""Logging configuration.

Sets up rotating file handlers for trading.log (INFO+) and debug.log (DEBUG).
Call once at startup from main.py.
"""

import logging
import logging.handlers
from pathlib import Path


LOG_FORMAT = "%(asctime)s.%(msecs)03d | %(levelname)s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(log_dir: str = "logs") -> None:
    """Configure root logger with rotating file handlers."""
    log_path = Path(log_dir)
    log_path.mkdir(exist_ok=True)

    formatter = logging.Formatter(fmt=LOG_FORMAT, datefmt=DATE_FORMAT)

    # INFO+ → trading.log
    trading_handler = logging.handlers.RotatingFileHandler(
        log_path / "trading.log",
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
    )
    trading_handler.setLevel(logging.INFO)
    trading_handler.setFormatter(formatter)

    # DEBUG+ → debug.log
    debug_handler = logging.handlers.RotatingFileHandler(
        log_path / "debug.log",
        maxBytes=20 * 1024 * 1024,
        backupCount=3,
    )
    debug_handler.setLevel(logging.DEBUG)
    debug_handler.setFormatter(formatter)

    # Console at WARNING
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(trading_handler)
    root.addHandler(debug_handler)
    root.addHandler(console_handler)
