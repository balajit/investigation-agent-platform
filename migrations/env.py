"""Alembic environment (F-069): typed URL resolution, metadata-driven autogenerate.

Never auto-creates production schema at runtime — migrations run explicitly
via ``alembic upgrade head`` in CI/deploy pipelines, with a startup version
check comparing the database revision against the code's expected head.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Ensure src/ is importable when alembic runs from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from investigation_agent_platform.infrastructure.persistence.models import (
    Base,
)
from investigation_agent_platform.infrastructure.persistence.pg_guard import (
    command_is_write,
    require_disposable,
)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

logger = logging.getLogger(__name__)


def _resolve_url() -> str:
    cli_args = context.get_x_argument(as_dictionary=True)
    if cli_args.get("url"):
        return str(cli_args["url"])
    url = os.environ.get("IAP_DATABASE_URI", "")
    # Alembic runs synchronously: translate async driver URIs.
    url = url.replace("postgresql+asyncpg://", "postgresql+psycopg://").replace(
        "postgresql+asyncpg:", "postgresql+psycopg:"
    )
    if not url:
        raise RuntimeError("No database URL: pass -x url=<URL> or set IAP_DATABASE_URI")
    return url


def run_migrations_offline() -> None:
    url = _resolve_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        compare_server_default=True,
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _current_command_name() -> str | None:
    """Alembic command driving this env run, from the CLI namespace.

    Returns None when the command cannot be determined (programmatic use);
    unknown commands are gated, never exempted.
    """
    cmd_opts = getattr(config, "cmd_opts", None)
    cmd = getattr(cmd_opts, "cmd", None)
    fn = cmd[0] if isinstance(cmd, (tuple, list)) and cmd else None
    name = getattr(fn, "__name__", None)
    return name if isinstance(name, str) else None


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _resolve_url()
    command = _current_command_name()
    if command_is_write(command):
        require_disposable(
            configuration["sqlalchemy.url"],
            purpose=f"alembic {command or 'unknown command'}",
        )
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_schemas=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
