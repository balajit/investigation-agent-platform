"""Repo-type detection for profile generation (G1).

Scored detection over build files, framework markers, and config
layout. No single marker trusted alone; ties and unknowns fall back
to generic gracefully (detection degrades, never blocks generation).
Pure: operates on file listings + sampled file contents.
"""

from __future__ import annotations

from typing import Any

# Signal weights. Framework markers outweigh build files; config layout
# is confirmatory only.
_BUILD_SIGNALS: dict[str, tuple[str, int]] = {
    "pom.xml": ("jvm", 2),
    "build.gradle": ("jvm", 2),
    "build.gradle.kts": ("jvm", 2),
    "pyproject.toml": ("python", 2),
    "setup.py": ("python", 2),
    "requirements.txt": ("python", 2),
    "Pipfile": ("python", 2),
    "go.mod": ("go", 3),
    "Cargo.toml": ("rust", 3),
}

_FRAMEWORK_SIGNALS: list[tuple[str, str, int]] = [
    ("spring-boot-starter", "java-spring", 5),
    ("@SpringBootApplication", "java-spring", 5),
    ("@RequestMapping", "java-spring", 3),
    ("@RestController", "java-spring", 3),
    ("quarkus-maven-plugin", "java-quarkus", 5),
    ("@QuarkusTest", "java-quarkus", 5),
    ("io.quarkus", "java-quarkus", 4),
    ("@Path(", "java-quarkus", 2),
    ("@RegisterRestClient", "java-quarkus", 4),
    ("io.micronaut", "java-micronaut", 4),
    ("@Controller", "java-micronaut", 2),
    ("from fastapi", "python-fastapi", 4),
    ("FastAPI(", "python-fastapi", 4),
    ("APIRouter", "python-fastapi", 3),
    ("from flask", "python-flask", 4),
    ("Flask(", "python-flask", 4),
    ("Blueprint(", "python-flask", 3),
    ("from django", "python-django", 4),
    ("manage.py", "python-django", 3),
    ("ROOT_URLCONF", "python-django", 3),
]

_CONFIG_SIGNALS: list[tuple[str, str, int]] = [
    ("application.yml", "java-spring", 1),
    ("application.yaml", "java-spring", 1),
    ("bootstrap.yml", "java-spring", 1),
    ("bootstrap.properties", "java-spring", 1),
    ("application.properties", "java-quarkus", 1),
]

_REPO_TYPES = (
    "java-spring",
    "java-quarkus",
    "java-micronaut",
    "python-fastapi",
    "python-flask",
    "python-django",
    "java-generic",
    "python-generic",
    "go-generic",
    "rust-generic",
    "unknown",
)

_LANGUAGE_FAMILY = {
    "java-spring": "java",
    "java-quarkus": "java",
    "java-micronaut": "java",
    "java-generic": "java",
    "python-fastapi": "python",
    "python-flask": "python",
    "python-django": "python",
    "python-generic": "python",
    "go-generic": "go",
    "rust-generic": "rust",
    "unknown": "",
}


def detect_repo_type(
    file_paths: list[str] | None,
    file_samples: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Score a repo to a RepoType (pure, deterministic).

    file_paths: repo-relative paths. file_samples: optional
    {path: head-text} for content markers (build files, entry points,
    config names count even without contents). Returns
    {repo_type, confidence (0..1), evidence [strings], family}.
    Confidence = winner_score / total_scored (1.0 when uncontested).
    """
    try:
        scores: dict[str, int] = {}
        evidence: list[str] = []

        def _vote(repo_type: str, weight: int, note: str) -> None:
            try:
                scores[repo_type] = scores.get(repo_type, 0) + weight
                if note not in evidence:
                    evidence.append(note)
            except Exception:
                pass

        paths = [str(p or "") for p in (file_paths or [])]
        basenames: dict[str, list[str]] = {}
        for path in paths:
            try:
                base = path.rsplit("/", 1)[-1]
                basenames.setdefault(base, []).append(path)
            except Exception:
                continue
        for base in basenames:
            try:
                hit = _BUILD_SIGNALS.get(base, None)
                if not hit:
                    continue
                family, weight = hit
                if family == "jvm":
                    _vote("java-generic", weight, f"build:{base}")
                elif family == "python":
                    _vote("python-generic", weight, f"build:{base}")
                elif family == "go":
                    _vote("go-generic", weight, f"build:{base}")
                elif family == "rust":
                    _vote("rust-generic", weight, f"build:{base}")
            except Exception:
                continue
        # Config basenames + content markers vote in the path loop below
        # (single counting).
        samples = dict(file_samples or {})
        for path in paths:
            try:
                base = path.rsplit("/", 1)[-1]
                body = str(samples.get(path, "") or "")[:20000]
                for needle, repo_type, weight in _FRAMEWORK_SIGNALS:
                    if needle in body or needle in base:
                        _vote(repo_type, weight, f"marker:{needle}")
                for needle, repo_type, weight in _CONFIG_SIGNALS:
                    if base == needle:
                        _vote(repo_type, weight, f"config:{base}")
            except Exception:
                continue
        if not scores:
            return {
                "repo_type": "unknown",
                "confidence": 0.0,
                "evidence": [],
                "family": "",
            }
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        winner, winner_score = ranked[0]
        total = sum(scores.values()) or 1
        # Generic family votes only break ties toward frameworks; a lone
        # generic winner stays generic.
        if winner not in _REPO_TYPES:
            winner = "unknown"
        return {
            "repo_type": winner,
            "confidence": round(min(1.0, winner_score / total), 3),
            "evidence": sorted(evidence)[:20],
            "family": _LANGUAGE_FAMILY.get(winner, ""),
        }
    except Exception:
        return {"repo_type": "unknown", "confidence": 0.0, "evidence": [], "family": ""}
