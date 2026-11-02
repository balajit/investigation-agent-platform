"""FastAPI API layer package initialization (Part 4, section 4).

``create_app`` is exposed lazily (PEP 562): importing any ``api`` submodule
(e.g. ``api.errors`` for log redaction) must not execute the full FastAPI
application import. The app module pulls structlog/FastAPI routers, which
breaks Temporal workflow-sandbox validation for any workflow-transitive
importer and kills the worker on boot.
"""

__all__ = ["create_app"]


def __getattr__(name: str) -> object:
    if name == "create_app":
        from investigation_agent_platform.api.app import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
