"""api package hygiene: importing api.errors must not drag the app stack.

Regression: api/__init__ eagerly imported the FastAPI app, so any
workflow-transitive ``api.errors`` import (log redaction in repos,
verification, gateway, forcing) pulled structlog -> rich into Temporal's
workflow sandbox (random.getrandbits is restricted there) and the worker
died on boot with "Failed validating workflow". The package init is now
lazy (PEP 562); this test pins that in a fresh interpreter.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_api_errors_import_stays_light() -> None:
    repo = Path(__file__).resolve().parents[2]
    code = (
        "import sys; "
        "import investigation_agent_platform.api.errors; "
        "heavy = sorted(m for m in sys.modules if m.split('.')[0] in "
        "('fastapi', 'structlog', 'rich', 'uvicorn', 'starlette')); "
        "print('HEAVY:' + ','.join(heavy)); "
        "from investigation_agent_platform.api import create_app; "
        "print('LAZY:' + ('ok' if callable(create_app) else 'broken'))"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo / "src") + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=repo,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    lines = dict(
        line.split(":", 1) for line in proc.stdout.strip().splitlines() if ":" in line
    )
    assert lines.get("HEAVY", "MISSING") == "", f"heavy imports leaked: {lines}"
    assert lines.get("LAZY") == "ok"
