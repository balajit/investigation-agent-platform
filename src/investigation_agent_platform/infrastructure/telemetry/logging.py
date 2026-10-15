# src/investigation_agent_platform/infrastructure/telemetry/logging.py
"""Logging pipeline wiring (TO_DO-006).

Loads handler topology from ``config/logging.yaml`` and attaches a console
handler plus a rotating JSON file handler to the root logger. Both handlers
use ``structlog.stdlib.ProcessorFormatter`` with ``JSONRenderer`` so stdlib
and structlog records share one JSON shape carrying timestamp, level,
logger, exception data, and any ``extra`` correlation keys (tenant_id,
investigation_id, event). ``IAP_LOG_LEVEL`` and ``IAP_LOG_FILE`` override
the YAML at runtime. Idempotent and never raises: logging must not break
boot. The file path's parent directory is created on demand.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog
import yaml

logger = logging.getLogger(__name__)

_CONFIGURED = False


def _json_formatter() -> structlog.stdlib.ProcessorFormatter:
    pre_chain = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]
    return structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=pre_chain,
    )


def _topology(yaml_path: Path) -> dict:
    try:
        raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("Ignoring unreadable logging YAML", extra={"error": str(exc)})
        return {}
    return raw if isinstance(raw, dict) else {}


def configure_logging(
    *,
    yaml_path: str | Path | None = None,
    log_file: str | None = None,
    level: str | None = None,
) -> Path | None:
    """Attach console + rotating-file JSON handlers; return the file path or None."""
    global _CONFIGURED
    root = logging.getLogger()
    config_file = Path(yaml_path) if yaml_path else (Path.cwd() / "config" / "logging.yaml")
    topology = _topology(config_file)
    handlers_cfg = topology.get("handlers", {}) if isinstance(topology, dict) else {}
    file_cfg = handlers_cfg.get("file", {}) if isinstance(handlers_cfg, dict) else {}
    console_cfg = handlers_cfg.get("console", {}) if isinstance(handlers_cfg, dict) else {}

    resolved_level = (
        level
        or os.environ.get("IAP_LOG_LEVEL", "")
        or (topology.get("root", {}) or {}).get("level", "INFO")
        or "INFO"
    )
    try:
        root.setLevel(resolved_level.upper())
    except (ValueError, TypeError):
        root.setLevel(logging.INFO)

    formatter = _json_formatter()
    if console_cfg.get("enabled", True):
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)

    active_file: Path | None = None
    file_enabled = bool(file_cfg.get("enabled", True))
    candidate = (
        log_file
        or os.environ.get("IAP_LOG_FILE", "")
        or str(file_cfg.get("path", "data/iap.log"))
    )
    if file_enabled and candidate:
        try:
            active_file = Path(candidate)
            active_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                active_file,
                maxBytes=int(file_cfg.get("max_bytes", 10_485_760)),
                backupCount=int(file_cfg.get("backup_count", 5)),
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except Exception as exc:
            logger.warning("File logging unavailable", extra={"error": str(exc)})
            active_file = None
    _CONFIGURED = True
    logger.info(
        "Logging pipeline ready",
        extra={"log_file": str(active_file) if active_file else None},
    )
    return active_file


def is_configured() -> bool:
    """Whether this process attached the file/console pipeline."""
    return _CONFIGURED
