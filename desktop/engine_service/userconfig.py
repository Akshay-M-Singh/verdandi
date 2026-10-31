"""User-writable config layer (D66 Phase 2, Batch 1).

The shipped YAMLs live inside the app bundle (read-only on macOS). On first
start we copy them into the workspace and point the engine's config paths at
the user copies — the same in-process override the benchmark proved
(`config.DOCUMENTS_PATH` is resolved at call time, D65). Everything that
writes config (model verification D35, "make default", later the Templates
editor) then works in a packaged app.

The `rules/*` files are seeded from the GENERIC starter set
(`rules/starter/*`), never from the maintainer's working config — the
maintainer's templates stay in the repo for dev/CLI/benchmark use only.

Layout:
    <workspace>/config/{models,thresholds}.yaml
    <workspace>/rules/{documents,workflows,exports}.yaml
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

from .workspace import workspace_root

logger = logging.getLogger(__name__)

FILES = (
    "config/models.yaml",
    "config/thresholds.yaml",
    "rules/documents.yaml",
    "rules/workflows.yaml",
    "rules/exports.yaml",
)

# The `rules/*` files ship as GENERIC STARTERS (a new install must never
# inherit the maintainer's own templates); `config/*` ships as-is. The
# source is per relative path; keys not listed here map to themselves.
STARTER_SOURCES = {
    "rules/documents.yaml": "rules/starter/documents.yaml",
    "rules/workflows.yaml": "rules/starter/workflows.yaml",
    "rules/exports.yaml": "rules/starter/exports.yaml",
}


def _source_for(rel: str) -> Path:
    """Where a user file's shipped default lives inside the bundle."""
    return bundled_root() / STARTER_SOURCES.get(rel, rel)


def bundled_root() -> Path:
    """Where the shipped defaults live: bundle dir when frozen, else repo."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[2]


def user_root() -> Path:
    return workspace_root()


def ensure_user_config() -> list[str]:
    """Seed missing user config files from the bundled defaults."""
    created: list[str] = []
    for rel in FILES:
        src = _source_for(rel)
        dst = user_root() / rel
        if dst.exists() or not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        created.append(rel)
    if created:
        logger.info("user config seeded: %s", ", ".join(created))
    return created


def apply_paths() -> None:
    """Point the engine at the user copies (only where a file exists)."""
    import config  # noqa: PLC0415 — engine import kept lazy

    root = user_root()
    mapping = {
        "MODELS_PATH": root / "config" / "models.yaml",
        "THRESHOLDS_PATH": root / "config" / "thresholds.yaml",
        "DOCUMENTS_PATH": root / "rules" / "documents.yaml",
        "WORKFLOWS_PATH": root / "rules" / "workflows.yaml",
        "EXPORTS_PATH": root / "rules" / "exports.yaml",
    }
    for name, path in mapping.items():
        if path.exists():
            setattr(config, name, path)


def reset_defaults() -> list[str]:
    """Overwrite the user copies with the shipped defaults."""
    written: list[str] = []
    for rel in FILES:
        src = _source_for(rel)
        if not src.exists():
            continue
        dst = user_root() / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        written.append(rel)
    apply_paths()
    return written


def user_layer_active() -> bool:
    """The user config layer is for PACKAGED apps (read-only bundle).

    In a dev checkout the repo IS the config — seeding copies into the
    workspace would shadow live edits. `VERDANDI_USER_CONFIG=1` forces it
    (tests, or running the source sidecar in packaged mode).
    """
    if os.environ.get("VERDANDI_USER_CONFIG") == "1":
        return True
    return bool(getattr(sys, "frozen", False))


def bootstrap() -> dict:
    """First-run seeding + path wiring. Safe to call on every start."""
    active = user_layer_active()
    created = ensure_user_config() if active else []
    if active:
        apply_paths()
    return {"root": str(user_root()), "bundled": str(bundled_root()),
            "active": active, "created": created}
