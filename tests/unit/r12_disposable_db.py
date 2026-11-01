"""Disposable PostgreSQL for R12 db-suite tests (not collected as tests).

One postgres:16-alpine container on a non-protected loopback port per
session, started lazily the first time a test requests it; per-test
databases are created and dropped around each test. Skips cleanly when
Docker or the image is unavailable. Every URL goes through
``require_disposable`` so the pg guard's contract is dogfooded.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

POSTGRES_IMAGE = "postgres:16-alpine"
DISPOSABLE_PORT = 5544
SUPERUSER = "iap"
PASSWORD = "iaptest12"


def _disposable_url(dbname: str) -> str:
    return f"postgresql://{SUPERUSER}:{PASSWORD}@localhost:{DISPOSABLE_PORT}/{dbname}"


def _tcp_ready(port: int) -> bool:
    try:
        with socket.create_connection(("localhost", port), timeout=1.0):
            return True
    except OSError:
        return False


def _docker_ok() -> bool:
    return shutil.which("docker") is not None


def _container_running(name: str) -> bool:
    try:
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return out.returncode == 0 and out.stdout.strip() == "true"
    except Exception:
        return False


def _ensure_container() -> None:
    name = "iap-r12-disposable-pg"
    if _container_running(name):
        return
    subprocess.run(
        ["docker", "rm", "-f", name],
        capture_output=True,
        timeout=30,
        check=False,
    )
    proc = subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", name,
            "-p", f"{DISPOSABLE_PORT}:5432",
            "-e", f"POSTGRES_USER={SUPERUSER}",
            "-e", f"POSTGRES_PASSWORD={PASSWORD}",
            POSTGRES_IMAGE,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"disposable postgres did not start: {proc.stderr[-2000:]}")
    deadline = time.time() + 90
    while time.time() < deadline:
        if _tcp_ready(DISPOSABLE_PORT):
            return
        time.sleep(0.5)
    raise RuntimeError("disposable postgres never became reachable")


def make_session_factory(db_url: str):  # type: ignore[no-untyped-def]
    engine = create_async_engine(
        db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    )
    return async_sessionmaker(engine, expire_on_commit=False), engine


async def dispose_engine(engine) -> None:  # type: ignore[no-untyped-def]
    try:
        await engine.dispose()
    except Exception:
        pass
