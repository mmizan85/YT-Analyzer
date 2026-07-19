"""
logger_manager.py — centralized `logging` module configuration.

This module exists to close a real gap: prior to this file, every
extraction failure (private/deleted videos, rate-limits, network errors)
was captured correctly as data (VideoRecord.status/error_message) and
shown live via `rich` in the terminal — but nothing was ever written
through Python's `logging` module, so there was no persistent, filterable,
level-based record of a run after the terminal scrolled past or the
process exited. Both mechanisms now coexist deliberately:

    * `rich` (cli_interface.py)  — the live, colorful, human-facing view
      of what's happening RIGHT NOW, in this terminal, during this run.
    * `logging` (this module)     — a plain-text, appendable, rotating log
      file (`yt_analyzer.log` by default) that survives after the process
      exits, can be `tail -f`'d during a long unattended batch, and can be
      grepped/filtered by level (DEBUG/INFO/WARNING/ERROR) independent of
      whatever the terminal happened to show.

Usage from any module in this package:
    import logging
    logger = logging.getLogger("yt_analyzer")
    logger.warning("Video %s unavailable: %s", video_id, reason)

`configure_logging()` is called ONCE, early in cli_interface.py's entry
point, before any extraction begins. Every other module just calls
`logging.getLogger("yt_analyzer")` (or a child logger, e.g.
`logging.getLogger("yt_analyzer.core_extractor")`) and inherits whatever
handlers/level were set up here — this is standard `logging` module
practice (configure once at the root of your own namespace, use
`getLogger(__name__)`-style child loggers everywhere else) rather than
each module configuring its own handlers, which would risk duplicate log
lines if a function is called more than once per process.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import Optional

# All loggers in this package hang off this root name, e.g.
# "yt_analyzer", "yt_analyzer.core_extractor", "yt_analyzer.db_manager".
# Configuring handlers/level on the root name is enough — child loggers
# propagate up to it by default, so nothing else needs its own setup.
PACKAGE_LOGGER_NAME = "yt_analyzer"

_DEFAULT_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False


def configure_logging(
    log_dir: Optional[str | Path] = None,
    log_filename: str = "yt_analyzer.log",
    level: int = logging.INFO,
    max_bytes: int = 5 * 1024 * 1024,  # 5 MB per file before rotating
    backup_count: int = 3,
    also_log_to_stderr: bool = False,
) -> logging.Logger:
    """Configure the package's root logger exactly once per process.

    Safe to call more than once (e.g. if the CLI is invoked repeatedly
    inside the same interpreter, as happens under CliRunner in tests) —
    subsequent calls are no-ops so handlers are never duplicated, which
    would otherwise cause every log line to be written N times after N
    calls.

    Parameters mirror the pieces of the blueprint this closes the gap on:
    a genuine, persistent, level-aware log — not print()/rich alone.
    """
    global _configured

    logger = logging.getLogger(PACKAGE_LOGGER_NAME)

    if _configured:
        return logger

    logger.setLevel(level)
    logger.propagate = False  # don't also send our records up to the interpreter's root logger

    log_directory = Path(log_dir) if log_dir else Path.cwd()
    try:
        log_directory.mkdir(parents=True, exist_ok=True)
        log_path = log_directory / log_filename
        file_handler = logging.handlers.RotatingFileHandler(
            filename=str(log_path),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(logging.Formatter(_DEFAULT_LOG_FORMAT, datefmt=_DEFAULT_DATE_FORMAT))
        logger.addHandler(file_handler)
    except OSError as exc:
        # Logging setup itself must never crash the whole tool — if the log
        # directory can't be created/written (permissions, read-only FS,
        # disk full), fall back to a stream-only logger so the run can
        # still proceed; the rich terminal output remains the failure UI
        # in that degraded case.
        fallback_handler = logging.StreamHandler()
        fallback_handler.setFormatter(logging.Formatter(_DEFAULT_LOG_FORMAT, datefmt=_DEFAULT_DATE_FORMAT))
        logger.addHandler(fallback_handler)
        logger.warning("Could not set up file logging (%s); falling back to stream logging only.", exc)

    if also_log_to_stderr:
        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(level)
        stream_handler.setFormatter(logging.Formatter(_DEFAULT_LOG_FORMAT, datefmt=_DEFAULT_DATE_FORMAT))
        logger.addHandler(stream_handler)

    _configured = True
    return logger


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Convenience accessor for other modules: get_logger(__name__) inside
    e.g. core_extractor.py returns a logger named "yt_analyzer.core_extractor"
    (assuming this package is imported as `yt_analyzer`), which inherits
    the root package logger's handlers/level once configure_logging() has
    run — or, if it hasn't run yet (e.g. this module is used
    programmatically, not via the CLI), returns a logger with Python's
    default handler-less behavior (a one-time "no handlers found" warning
    to stderr the first time something is logged), which is standard,
    unsurprising `logging` module behavior rather than a bug.
    """
    if name and name.startswith(PACKAGE_LOGGER_NAME):
        return logging.getLogger(name)
    if name:
        return logging.getLogger(f"{PACKAGE_LOGGER_NAME}.{name}")
    return logging.getLogger(PACKAGE_LOGGER_NAME)


def reset_logging_state_for_tests() -> None:
    """Test-only helper: clears the module-level 'already configured' flag
    and removes handlers from the package logger, so test suites can call
    configure_logging() fresh with different parameters (e.g. a different
    tmp log_dir per test) without handlers accumulating across test cases
    in the same interpreter session.
    """
    global _configured
    logger = logging.getLogger(PACKAGE_LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    _configured = False
