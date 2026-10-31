"""Excel export from the desktop app (D66).

Phase 1: build a workbook from a run via the core export engine
(`export.export_run`, D54) — the same callable the CLI uses.
Phase 4 Batch 3: the export-PROFILE builder — friendly source catalog,
profile CRUD over `ui.lib`'s serialization/validator (call-time config
paths, D10), workflow seeding and a draft preview through `export.build_rows`.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import messages

META_SOURCE_LABELS = {
    "original": ("File · Original filename", "File"),
    "stem": ("File · File ID (internal)", "File ID"),
    "status": ("File · Extraction status", "Status"),
    "flags_total": ("File · Flags total", "Flags Total"),
    "flags_pending": ("File · Flags pending review", "Flags Pending"),
    "reviewed": ("File · Flags reviewed", "Reviewed"),
    "validation_status": ("File · Validation summary", "Validation"),
}


def _humanize(name: str) -> str:
    return str(name).replace("_", " ").strip().title()


def _config():
    import config  # noqa: PLC0415

    return config


def _ui():
    from ui import lib as ui_lib  # noqa: PLC0415

    return ui_lib


def _source_key(source) -> tuple:
    if not isinstance(source, dict) or not source:
        return ("meta", "")
    if "meta" in source:
        return ("meta", source["meta"])
    if "template" in source:
        return ("template", source["template"], source.get("field"))
    if "line_item" in source:
        return ("line_item", source["line_item"])
    return ("cross_check", source.get("cross_check"))


def _column_warning(col: dict) -> dict:
    """Human-readable structured warning for a column without a valid source."""
    key = _source_key(col.get("source"))
    kind = key[0]
    header = col.get("header") or "this"
    where = f"{header} column"
    if kind == "template":
        return messages.warning_message("column_source_gone", where=where,
                                        header=header)
    if kind == "line_item":
        return messages.warning_message("column_line_item_not_available",
                                        where=where, header=header,
                                        column=key[1])
    if kind == "cross_check":
        return messages.warning_message("column_cross_check_missing",
                                        where=where, header=header, rule=key[1])
    if kind == "meta" and not key[1]:
        return messages.warning_message("column_no_source", where=where,
                                        header=header)
    return messages.warning_message("column_source_gone", where=where,
                                    header=header)


def list_profiles() -> list[dict]:
    """Full profiles (the run Export tab reads id/label/grain; the builder
    reads columns/options/workflow too)."""
    return list(_config().load_exports()["exports"])


def build(run_dir: Path, profile_id: str) -> Path:
    """Build the workbook under <run>/export/ and return its path."""
    import export as export_mod  # noqa: PLC0415

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(run_dir) / "export" / f"{profile_id}_{stamp}.xlsx"
    return export_mod.export_run(Path(run_dir), profile_id, out, _config().load_all())


# ---------------------------------------------------------------------------
# builder (D66 Phase 4 Batch 3)
# ---------------------------------------------------------------------------

def catalog(profile: dict | None = None) -> dict:
    """Friendly sources for the column builder + warnings for the draft."""
    cfg = _config().load_all()
    docs = cfg["documents"]["documents"]
    by_id = {d["id"]: d for d in docs}
    wf = next(
        (w for w in cfg["workflows"]["workflows"]
         if w.get("id") == (profile or {}).get("workflow")),
        None,
    )
    tpl_order = [d for d in (wf or {}).get("documents", []) if d in by_id] \
        or [d["id"] for d in docs]

    sources: list[dict] = []
    for meta, (label, header) in META_SOURCE_LABELS.items():
        sources.append({
            "source": {"meta": meta}, "label": label, "header": header,
            "help": "File-level information for every row.",
        })
    for tpl in tpl_order:
        prof = by_id[tpl]
        for f in (prof.get("extraction") or {}).get("fields") or []:
            sources.append({
                "source": {"template": tpl, "field": f["name"]},
                "label": f"{prof.get('label') or tpl} · {_humanize(f['name'])}",
                "header": _humanize(f["name"]),
                "help": f.get("description") or "",
            })
    if (profile or {}).get("grain") == "line_item" and \
            (profile or {}).get("line_item_template"):
        tpl = profile["line_item_template"]
        for c in _config().line_item_columns(cfg["documents"], tpl):
            sources.append({
                "source": {"line_item": c["name"]},
                "label": f"Line items · {_humanize(c['name'])}",
                "header": _humanize(c["name"]),
                "help": c.get("description") or "",
            })
        sources.append({
            "source": {"line_item": "index"},
            "label": "Line items · Item #", "header": "Item #",
            "help": "The line item's number within the document.",
        })
    for r in (wf or {}).get("cross_checks") or []:
        label = r.get("label") or r.get("id") or "check"
        sources.append({
            "source": {"cross_check": r.get("id")},
            "label": f"Check · {label}", "header": str(label),
            "help": "Cross-document outcome: PASS / FAIL / NOT CHECKED.",
        })

    keys = {json.dumps(s["source"], sort_keys=True) for s in sources}
    warnings = [
        _column_warning(col)
        for col in (profile or {}).get("columns") or []
        if json.dumps(col.get("source") or {}, sort_keys=True) not in keys
    ]
    return {
        "sources": sources,
        "warnings": warnings,
        "workflows": [
            {"id": w["id"], "label": w.get("label") or w["id"]}
            for w in cfg["workflows"]["workflows"]
        ],
        "line_item_templates": [
            d["id"] for d in docs
            if d.get("extraction", {}).get("line_items")
        ],
    }


def get_yaml() -> str:
    return _ui().exports_text()


def validate_yaml(text: str) -> dict:
    ok, message, exports = _ui().validate_exports(text)
    return {"ok": ok, "message": message,
            "ids": [e.get("id") for e in (exports or [])]}


def save_yaml(text: str) -> dict:
    path = _ui().save_exports(text)
    ok, message, _ = _ui().validate_exports(text)
    return {"path": str(path), "message": message}


def _load_profiles() -> list[dict]:
    return list(_config().load_exports()["exports"])


def _save_profiles(profiles: list[dict]) -> str:
    text = _ui().exports_to_yaml(profiles)
    ok, message, _ = _ui().validate_exports(text)
    if not ok:
        raise ValueError(message)
    _ui().save_exports(text)
    return message


def save_profile(profile: dict, original_id: str | None = None) -> dict:
    if not isinstance(profile, dict):
        raise ValueError("profile must be an object")
    pid = str(profile.get("id") or "").strip()
    if not pid:
        raise ValueError("export profile id is required")
    profiles = _load_profiles()
    target = (original_id or pid).strip()
    idx = next((i for i, e in enumerate(profiles) if e.get("id") == target), None)
    if idx is None:
        if any(e.get("id") == pid for e in profiles):
            raise ValueError(f"export profile {pid!r} already exists")
        profiles.append(profile)
    else:
        profiles[idx] = profile
    message = _save_profiles(profiles)
    return {"profile": profile, "message": message}


def delete_profile(profile_id: str) -> dict:
    profiles = _load_profiles()
    rest = [e for e in profiles if e.get("id") != profile_id]
    if len(rest) == len(profiles):
        raise ValueError(f"export profile {profile_id!r} not found")
    message = _save_profiles(rest)
    return {"deleted": profile_id, "message": message}


def duplicate_profile(profile_id: str) -> dict:
    import copy

    profiles = _load_profiles()
    src = next((e for e in profiles if e.get("id") == profile_id), None)
    if src is None:
        raise ValueError(f"export profile {profile_id!r} not found")
    clone = copy.deepcopy(src)
    seen = {e.get("id") for e in profiles}
    base = f"{profile_id}_copy"
    new_id, n = base, 2
    while new_id in seen:
        new_id = f"{base}_{n}"
        n += 1
    clone["id"] = new_id
    clone["label"] = f"{src.get('label', profile_id)} (copy)"
    profiles.append(clone)
    message = _save_profiles(profiles)
    return {"profile": clone, "message": message}


def seed_from_workflow(workflow_id: str) -> dict:
    """A starter export profile whose columns mirror a workflow's documents."""
    cfg = _config().load_all()
    docs = cfg["documents"]["documents"]
    by_id = {d["id"]: d for d in docs}
    wf = next((w for w in cfg["workflows"]["workflows"]
               if w.get("id") == workflow_id), None)
    if wf is None:
        raise ValueError(f"workflow {workflow_id!r} not found")
    columns: list[dict] = [
        {"header": "File", "source": {"meta": "original"}},
    ]
    line_item_tpl: str | None = None
    for tpl in wf.get("documents") or []:
        prof = by_id.get(tpl)
        if not prof:
            continue
        for f in (prof.get("extraction") or {}).get("fields") or []:
            columns.append({
                "header": _humanize(f["name"]),
                "source": {"template": tpl, "field": f["name"]},
            })
        if not line_item_tpl and (prof.get("extraction") or {}).get("line_items"):
            line_item_tpl = tpl
    if line_item_tpl:
        for c in _config().line_item_columns(cfg["documents"], line_item_tpl):
            columns.append({
                "header": _humanize(c["name"]),
                "source": {"line_item": c["name"]},
            })
    for r in wf.get("cross_checks") or []:
        label = r.get("label") or r.get("id") or "check"
        columns.append({
            "header": str(label),
            "source": {"cross_check": r.get("id")},
        })
    columns.append({"header": "Validation",
                    "source": {"meta": "validation_status"}})
    profile = {
        "id": f"{workflow_id}_export",
        "label": f"{wf.get('label') or workflow_id} export",
        "workflow": workflow_id,
        "grain": "line_item" if line_item_tpl else "file",
        "options": {"date_format": "dd/mm/yyyy"},
        "columns": columns,
    }
    if line_item_tpl:
        profile["line_item_template"] = line_item_tpl
    return {"profile": profile}


def preview(profile: dict, run_id: str | None = None) -> dict:
    """Preview a DRAFT profile against the newest extracted run (or a given
    one) using the real row builder (export.build_rows)."""
    import export as export_mod  # noqa: PLC0415

    run_dir = None
    used_id = run_id
    if run_id:
        job = _manager().get(run_id)
        if job is None:
            raise ValueError(f"unknown run {run_id}")
        run_dir = Path(job.run_dir)
    else:
        for job in _manager().list():
            run = Path(job.run_dir)
            if (run / "json").is_dir() and any((run / "json").glob("*.json")):
                run_dir, used_id = run, job.id
                break
    if run_dir is None:
        raise ValueError("no extracted run available for a preview yet")
    try:
        rows = export_mod.build_rows(run_dir, profile, _config().load_all())
    except export_mod.ExportError as exc:
        raise ValueError(str(exc))
    return {
        "run": used_id,
        "headers": [c.get("header") or "" for c in profile.get("columns") or []],
        "total_rows": len(rows),
        "rows": [
            {
                "stem": r.stem,
                "cells": [
                    {"value": c.raw, "status": c.status, "type": c.type,
                     "comment": c.comment}
                    for c in r.cells
                ],
            }
            for r in rows[:25]
        ],
    }


_MANAGER = None


def bind_manager(manager) -> None:
    """The app passes its JobManager so preview can find runs."""
    global _MANAGER
    _MANAGER = manager


def _manager():
    if _MANAGER is None:
        raise ValueError("no run manager bound")
    return _MANAGER
