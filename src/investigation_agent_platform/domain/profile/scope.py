# src/investigation_agent_platform/domain/profile/scope.py
"""Tenant source-boundary classification (J9 D16, single home).

Three states, repo-qualified: a file is in-tenant when it sits under a
declared root of its own repository; external when it sits outside every
applicable root; unknown when roots are not declared or not loadable (say
so, do not guess, and never invent a handoff on an unknown boundary).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class BoundaryClass(StrEnum):
    IN_TENANT = "in_tenant"
    EXTERNAL = "external"
    UNKNOWN = "unknown"  # roots not declared or not loadable: say so, do not guess


class SourceScope(BaseModel):
    model_config = ConfigDict(frozen=True)

    repos: tuple[str, ...] = ()  # org-qualified locators of one service
    roots: tuple[str, ...] = ()  # normalized; "" means the whole repository


def repo_locator(org: str, repo: str) -> str:
    """Locator for one repos entry (moved from the git adapter).

    Entries may carry their own org (``otherorg/shared-libs`` for
    cross-org checkouts); otherwise the service org applies. Bare names
    stay bare. One slash max — deeper paths are not locators.
    """
    repo = str(repo or "").strip().strip("/")
    if "/" in repo:
        o, name = repo.split("/", 1)
        if o and name and "/" not in name:
            return f"{o}/{name}"
        return repo
    if org and repo:
        return f"{org}/{repo}"
    return repo


def normalize_source_root(root: object) -> str | None:
    """``"."``, ``"./"``, ``""`` and ``"/"`` mean the whole repository (``""``); ``..`` is invalid."""
    text = str(root or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    text = text.strip("/")
    if ".." in text.split("/") or "\x00" in text:
        return None
    return "" if text in {"", "."} else text


def classify(scopes: list[SourceScope], repository: str, file_path: str) -> BoundaryClass:
    if not any(scope.repos or scope.roots for scope in scopes):
        return BoundaryClass.UNKNOWN
    path = f"/{str(file_path or '').lstrip('/')}"
    repo_known = bool(repository) and repository not in {"unknown-repo", "Git"}
    for scope in scopes:
        if repo_known and scope.repos and repository not in scope.repos:
            continue  # another service's repository: its roots do not apply here
        if not scope.roots or "" in scope.roots:
            return BoundaryClass.IN_TENANT  # repos declared, no narrower root
        if any(path == f"/{r}" or path.startswith(f"/{r}/") for r in scope.roots):
            return BoundaryClass.IN_TENANT
    return BoundaryClass.EXTERNAL
