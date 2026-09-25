#!/usr/bin/env python3
"""
Repository Initialization Script for Investigation Agent Platform (IAP).

Configures project structure, hatchling pyproject.toml, toolchain configurations,
and initial application profiles.
"""

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import sys
import uuid

# Configure structured JSON logging for agentic observability
class StructuredJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        if hasattr(record, "context"):
            log_obj["context"] = getattr(record, "context")
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_obj)

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(StructuredJsonFormatter())
logger = logging.getLogger("iap.bootstrap")
logger.setLevel(logging.INFO)
logger.addHandler(handler)


@dataclass(frozen=True)
class ProjectSpec:
    root_dir: Path
    execution_id: str


class RepositoryInitializer:
    """Handles atomic repository structure initialization and configuration generation."""

    def __init__(self, target_root: Path) -> None:
        self.target_root = target_root.resolve()
        self.execution_id = str(uuid.uuid4())
        self.created_paths: list[Path] = []

    def log_event(self, level: int, msg: str, **kwargs: object) -> None:
        logger.log(
            level,
            msg,
            extra={"context": {"execution_id": self.execution_id, **kwargs}},
        )

    def validate_target_boundary(self) -> None:
        """Ensure operations stay strictly within target root boundary."""
        if self.target_root.exists() and not self.target_root.is_dir():
            raise ValueError(f"Target path {self.target_root} is not a directory.")

    def rollback(self) -> None:
        """Roll back created files and directories in reverse order on failure."""
        self.log_event(logging.WARN, "Initiating rollback of created artifacts")
        for path in reversed(self.created_paths):
            try:
                if path.is_file() or path.is_symlink():
                    path.unlink(missing_ok=True)
                elif path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
            except Exception as err:
                self.log_event(
                    logging.ERROR,
                    f"Failed to remove {path} during rollback",
                    error=str(err),
                )

    def create_directories(self, directories: list[str]) -> None:
        """Recursively create target directories with tracking."""
        for rel_dir in directories:
            dir_path = (self.target_root / rel_dir).resolve()
            if not str(dir_path).startswith(str(self.target_root)):
                raise PermissionError(f"Directory traversal attack detected: {rel_dir}")
            
            if not dir_path.exists():
                dir_path.mkdir(parents=True, exist_ok=True)
                self.created_paths.append(dir_path)
                self.log_event(logging.INFO, f"Created directory", path=str(rel_dir))

    def write_file(self, rel_path: str, content: str) -> None:
        """Safely write configuration files with tracking."""
        file_path = (self.target_root / rel_path).resolve()
        if not str(file_path).startswith(str(self.target_root)):
            raise PermissionError(f"Path traversal attempt detected: {rel_path}")

        if file_path.exists():
            self.log_event(logging.INFO, "File exists, skipping", path=str(rel_path))
            return

        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content.strip() + "\n", encoding="utf-8")
        self.created_paths.append(file_path)
        self.log_event(logging.INFO, "Created file", path=str(rel_path))

    def initialize(self) -> None:
        """Execute complete repository structure setup."""
        self.validate_target_boundary()
        self.target_root.mkdir(parents=True, exist_ok=True)

        try:
            directories = [
                "src/investigation_agent_platform/domain/investigation",
                "src/investigation_agent_platform/domain/evidence",
                "src/investigation_agent_platform/domain/hypothesis",
                "src/investigation_agent_platform/domain/correlation",
                "src/investigation_agent_platform/domain/entity",
                "src/investigation_agent_platform/domain/timeline",
                "src/investigation_agent_platform/domain/finding",
                "src/investigation_agent_platform/domain/profile",
                "src/investigation_agent_platform/domain/common",
                "src/investigation_agent_platform/application/investigation",
                "src/investigation_agent_platform/application/evidence",
                "src/investigation_agent_platform/application/correlation",
                "src/investigation_agent_platform/application/profile",
                "src/investigation_agent_platform/ports/evidence",
                "src/investigation_agent_platform/ports/persistence",
                "src/investigation_agent_platform/ports/reasoning",
                "src/investigation_agent_platform/ports/correlation",
                "src/investigation_agent_platform/ports/profile",
                "src/investigation_agent_platform/infrastructure/persistence",
                "src/investigation_agent_platform/infrastructure/configuration",
                "src/investigation_agent_platform/infrastructure/messaging",
                "src/investigation_agent_platform/infrastructure/observability",
                "src/investigation_agent_platform/bootstrap",
                "tests/unit/domain",
                "tests/unit/application",
                "tests/integration",
                "tests/fixtures",
                "docs/architecture/adr",
                "docs/development",
                "config/profiles",
                "scripts",
                "migrations",
                "docker",
                "deployment/k8s",
            ]
            self.create_directories(directories)

            # Generate package __init__.py files
            for rel_dir in directories:
                if rel_dir.startswith("src/investigation_agent_platform"):
                    self.write_file(f"{rel_dir}/__init__.py", '"""Package initialization."""')

            self.write_file(
                "src/investigation_agent_platform/__init__.py",
                '"""Investigation Agent Platform core package."""\n\n__version__ = "0.1.0"',
            )

            # Write standard pyproject.toml
            pyproject_content = """
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "investigation-agent-platform"
version = "0.1.0"
description = "Agentic investigation platform to investigate system issues"
readme = "README.md"
requires-python = ">=3.13"
dependencies = [
    "pydantic>=2.0.0",
    "structlog>=24.1.0",
    "opentelemetry-api>=1.20.0",
    "opentelemetry-sdk>=1.20.0",
    "sqlalchemy[asyncio]>=2.0.0",
    "alembic>=1.13.0",
    "pyyaml>=6.0",
]

[dependency-groups]
dev = [
    "uv>=0.4.0",
    "ruff>=0.6.0",
    "mypy>=1.11.0",
    "pipdeptree>=2.23.0",
    "pytest>=8.3.0",
    "pytest-cov>=5.0.0",
    "pylint>=3.2.0",
]

[tool.hatch.build.targets.wheel]
packages = ["src/investigation_agent_platform"]

[tool.ruff]
target-version = "py313"
line-length = 100

[tool.mypy]
python_version = "3.13"
strict = true

[tool.pytest.ini_options]
minversion = "8.0"
testpaths = ["tests"]
pythonpath = ["src"]
"""
            self.write_file("pyproject.toml", pyproject_content)

            # Write basic config files
            app_config = """
platform:
  name: investigation-agent-platform
  environment: local

investigation:
  default_timeout_seconds: 1800
  max_tool_calls: 100
  max_hypotheses: 10
  max_evidence_items: 25

persistence:
  provider: postgresql
"""
            self.write_file("config/application.yaml", app_config)

            self.write_file("README.md", "# Investigation Agent Platform (IAP)")
            self.write_file(".gitignore", "*.pyc\n__pycache__/\n.venv/\n.mypy_cache/\n.pytest_cache/\n")

            self.log_event(
                logging.INFO,
                "Repository successfully initialized",
                root=str(self.target_root),
            )

        except Exception as err:
            self.log_event(
                logging.ERROR,
                "Initialization failed, halting and rolling back",
                error=str(err),
            )
            self.rollback()
            sys.exit(1)


def main() -> None:
    target_path = Path.cwd()
    if len(sys.argv) > 1:
        target_path = Path(sys.argv[1])
    
    initializer = RepositoryInitializer(target_path)
    initializer.initialize()


if __name__ == "__main__":
    main()
