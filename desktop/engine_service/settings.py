"""Desktop settings (D66 Phase 1): defaults + the gateway key in the OS keychain.

- `<workspace>/settings.json` holds the chosen profile/model (small, local).
- The API key lives in the operating system's credential store via the
  optional `keyring` package; it is loaded into the environment at service
  start so the existing config resolution (os.environ → .env, D10) just works.
  If `keyring` is unavailable (or no backend exists), everything still runs —
  only the key status reports false.

No core module reads this file.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .workspace import workspace_root

logger = logging.getLogger(__name__)

SERVICE = "verdandi-desktop"
ACCOUNT = "OPENCODE_API_KEY"


def _path() -> Path:
    return workspace_root() / "settings.json"


def load_settings() -> dict:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(patch: dict) -> dict:
    data = load_settings()
    for key in ("profile", "model", "workers"):
        if patch.get(key) not in (None, ""):
            data[key] = patch[key]
    # Backup/recheck model: "" IS meaningful (same as primary) — unlike the
    # keys above, a blank value must be persisted.
    if patch.get("verification_model") is not None:
        data["verification_model"] = str(patch["verification_model"])
    if patch.get("onboarded") is not None:
        data["onboarded"] = bool(patch["onboarded"])
    if patch.get("auto_prune_renders") is not None:
        data["auto_prune_renders"] = bool(patch["auto_prune_renders"])
    _path().parent.mkdir(parents=True, exist_ok=True)
    _path().write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return data


def key_present() -> bool:
    if os.environ.get("OPENCODE_API_KEY"):
        return True
    try:
        import keyring  # noqa: PLC0415 — optional dependency

        return bool(keyring.get_password(SERVICE, ACCOUNT))
    except Exception:  # noqa: BLE001 — no backend / import error
        return False


def keychain_backend() -> str | None:
    """The OS keychain backend keyring resolved to, or None when unavailable.

    Discovery is entry-point based (`keyring.backends`); a packaged app only
    finds a backend when the bundle carries keyring's dist-info metadata
    (build_sidecar.py collects it). `fail.Keyring` means no usable backend.
    """
    try:
        import keyring  # noqa: PLC0415 — optional dependency

        from keyring.backends import fail  # noqa: PLC0415

        backend = keyring.get_keyring()
        if isinstance(backend, fail.Keyring):
            return None
        cls = type(backend)
        return f"{cls.__module__}.{cls.__name__}"
    except Exception:  # noqa: BLE001 — no keyring / no backend
        return None


def set_key(value: str) -> bool:
    """Store the key in the OS keychain and activate it for this process.

    Returns whether it PERSISTED to the keychain — when no backend exists the
    key still works for this session (env) and the caller must say so.
    """
    value = value.strip()
    if not value:
        return False
    os.environ["OPENCODE_API_KEY"] = value
    try:
        import keyring  # noqa: PLC0415

        keyring.set_password(SERVICE, ACCOUNT, value)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not persist the key in the OS keychain: %s", exc)
        return False


def remove_key() -> bool:
    """Remove the key from the OS keychain and this process.

    Returns whether the keychain entry was deleted (False when there was no
    backend/entry); the environment is always cleared.
    """
    os.environ.pop("OPENCODE_API_KEY", None)
    try:
        import keyring  # noqa: PLC0415

        keyring.delete_password(SERVICE, ACCOUNT)
        return True
    except Exception:  # noqa: BLE001 — no backend / no entry
        return False


def apply_key() -> None:
    """Load the keychain key into the environment (never overrides env/.env)."""
    if os.environ.get("OPENCODE_API_KEY"):
        return
    try:
        import keyring  # noqa: PLC0415

        value = keyring.get_password(SERVICE, ACCOUNT)
        if value:
            os.environ["OPENCODE_API_KEY"] = value
    except Exception:  # noqa: BLE001
        pass
