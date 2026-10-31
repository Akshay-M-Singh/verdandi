"""Workflow bundle editing for the desktop app (D66 Phase 4 Batch 2).

Same pattern as templates.py: thin wrappers over `ui.lib`'s pure helpers
(workflows_to_yaml, validate_workflows, atomic save) which resolve
`config.WORKFLOWS_PATH` at CALL time (D10). Dangling template references are
WARNINGS (D52) — saves are never blocked by them.
"""

from __future__ import annotations


def _ui():
    from ui import lib as ui_lib  # noqa: PLC0415 — engine path set by the entry

    return ui_lib


def _config():
    import config  # noqa: PLC0415

    return config


def list_workflows() -> dict:
    """Workflows + D52 warnings + the profile catalog (ids, labels, fields)."""
    data = _config().load_workflows()
    profiles = []
    for d in _config().load_documents()["documents"]:
        fields = [
            f.get("name")
            for f in ((d.get("extraction") or {}).get("fields") or [])
            if f.get("name")
        ]
        profiles.append({
            "id": d.get("id"),
            "label": d.get("label") or d.get("id"),
            "fields": fields,
        })
    return {
        "workflows": data["workflows"],
        "warnings": list(data.get("warnings") or []),
        "profiles": profiles,
    }


def get_yaml() -> str:
    return _ui().workflows_text()


def validate_yaml(text: str) -> dict:
    ok, message, workflows = _ui().validate_workflows(text)
    return {
        "ok": ok,
        "message": message,
        "ids": [w.get("id") for w in (workflows or [])],
    }


def save_yaml(text: str) -> dict:
    path = _ui().save_workflows(text)  # validates + atomic write
    ok, message, _ = _ui().validate_workflows(text)
    return {"path": str(path), "message": message}


def _load() -> list[dict]:
    return list(_config().load_workflows()["workflows"])


def _save(workflows: list[dict]) -> str:
    text = _ui().workflows_to_yaml(workflows)
    ok, message, _ = _ui().validate_workflows(text)
    if not ok:
        raise ValueError(message)
    _ui().save_workflows(text)
    return message


def save_workflow(workflow: dict, original_id: str | None = None) -> dict:
    if not isinstance(workflow, dict):
        raise ValueError("workflow must be an object")
    wid = str(workflow.get("id") or "").strip()
    if not wid:
        raise ValueError("workflow id is required")
    workflows = _load()
    target = (original_id or wid).strip()
    idx = next((i for i, w in enumerate(workflows) if w.get("id") == target), None)
    if idx is None:
        if any(w.get("id") == wid for w in workflows):
            raise ValueError(f"workflow {wid!r} already exists")
        workflows.append(workflow)
    else:
        workflows[idx] = workflow
    message = _save(workflows)
    listing = list_workflows()
    return {"workflow": workflow, "message": message,
            "warnings": listing["warnings"]}


def delete_workflow(workflow_id: str) -> dict:
    workflows = _load()
    rest = [w for w in workflows if w.get("id") != workflow_id]
    if len(rest) == len(workflows):
        raise ValueError(f"workflow {workflow_id!r} not found")
    if not rest:
        raise ValueError("refusing to delete the last workflow — at least one "
                         "must exist")
    message = _save(rest)
    return {"deleted": workflow_id, "message": message}


def duplicate_workflow(workflow_id: str) -> dict:
    import copy

    workflows = _load()
    src = next((w for w in workflows if w.get("id") == workflow_id), None)
    if src is None:
        raise ValueError(f"workflow {workflow_id!r} not found")
    clone = copy.deepcopy(src)
    seen = {w.get("id") for w in workflows}
    base = f"{workflow_id}_copy"
    new_id, n = base, 2
    while new_id in seen:
        new_id = f"{base}_{n}"
        n += 1
    clone["id"] = new_id
    clone["label"] = f"{src.get('label', workflow_id)} (copy)"
    workflows.append(clone)
    message = _save(workflows)
    return {"workflow": clone, "message": message}
