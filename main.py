# main.py — root scaffold placeholder (DEPRECATED; Part 4 defines the real gateway)
"""DEPRECATED root scaffold stub — retained as a placeholder for the first build.

The canonical FastAPI gateway + health router + Temporal/Temporal worker entrypoint
live at ``src/investigation_agent_platform/main.py`` (``uvicorn
investigation_agent_platform.main:app``) and ``application/worker/``.

Pending deletion (see REVIEW_GAPS.md *LOW / Cleanup manifest*): this root-level
entrypoint is superseded and will be removed in the consolidation pass.
"""


def main() -> None:
    print("Hello from investigation-agent-platform!")


if __name__ == "__main__":
    main()