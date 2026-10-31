"""Template (document profile) editing for the desktop app (D66 Phase 4).

Wraps the Streamlit UI's pure helpers (`ui.lib`): serialization
(documents_to_yaml), the real contract validator (validate_documents) and the
atomic save — all of which resolve `config.DOCUMENTS_PATH` at CALL time
(D10), so packaged apps edit the user config layer and dev checkouts edit the
repo configs. Workflow references are reported as warnings, never refusals
(D52).
"""

from __future__ import annotations

import messages


def _ui():
    from ui import lib as ui_lib  # noqa: PLC0415 — engine path set by the entry

    return ui_lib


def _config():
    import config  # noqa: PLC0415

    return config


def list_templates() -> dict:
    """Profiles + which workflows reference them (delete warnings)."""
    cfg = _config().load_all()
    return {
        "templates": cfg["documents"]["documents"],
        "workflow_refs": _ui().workflow_references(),
    }


def get_yaml() -> str:
    return _ui().documents_text()


def validate_yaml(text: str) -> dict:
    ok, message, docs = _ui().validate_documents(text)
    return {
        "ok": ok,
        "message": message,
        "ids": [d.get("id") for d in (docs or [])],
    }


def save_yaml(text: str) -> dict:
    path = _ui().save_documents(text)  # validates + atomic write
    ok, message, _ = _ui().validate_documents(text)
    return {"path": str(path), "message": message}


def _load_docs() -> list[dict]:
    return list(_config().load_documents()["documents"])


def _save_docs(docs: list[dict]) -> str:
    text = _ui().documents_to_yaml(docs)
    ok, message, _ = _ui().validate_documents(text)
    if not ok:
        raise ValueError(message)
    _ui().save_documents(text)
    return message


def save_template(profile: dict, original_id: str | None = None) -> dict:
    """Create or update one profile (identity, fields, line items, rules)."""
    if not isinstance(profile, dict):
        raise ValueError("profile must be an object")
    doc_id = str(profile.get("id") or "").strip()
    if not doc_id:
        raise ValueError("template id is required")
    docs = _load_docs()
    target = (original_id or doc_id).strip()
    idx = next((i for i, d in enumerate(docs) if d.get("id") == target), None)
    if idx is None:
        if any(d.get("id") == doc_id for d in docs):
            raise ValueError(f"template {doc_id!r} already exists")
        docs.append(profile)
    else:
        docs[idx] = profile
    message = _save_docs(docs)
    return {"profile": profile, "message": message,
            "workflow_refs": _ui().workflow_references()}


def delete_template(doc_id: str) -> dict:
    docs = _load_docs()
    rest = [d for d in docs if d.get("id") != doc_id]
    if len(rest) == len(docs):
        raise ValueError(f"template {doc_id!r} not found")
    if not rest:
        raise ValueError("refusing to delete the last template — at least one "
                         "profile must exist")
    refs = _ui().workflow_references()
    warnings = [
        messages.warning_message("template_in_use", where=doc_id,
                                 workflows=", ".join(
                                     w for w in refs.get(doc_id, [])))
        for w in refs.get(doc_id, [])
    ]
    message = _save_docs(rest)
    return {"deleted": doc_id, "warnings": warnings, "message": message}


def duplicate_template(doc_id: str) -> dict:
    docs = _load_docs()
    clone = _ui().duplicate_profile(docs, doc_id)
    docs.append(clone)
    message = _save_docs(docs)
    return {"profile": clone, "message": message}
