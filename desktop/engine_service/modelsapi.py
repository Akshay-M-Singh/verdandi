"""Model picker support for the desktop app (D66 Phase 2, Batch 1).

Thin wrapper over the Streamlit UI's pure helpers (`ui.lib`), which already
implement D35: live catalog + models.dev hints + the probed `vision_verified`
allowlist. Catalog calls are TTL-cached and free; `verify` is a REAL paid
probe (~100-300 tokens) and is therefore always an explicit user action.
"""

from __future__ import annotations


def _ui_lib():
    from ui import lib as ui_lib  # noqa: PLC0415 — engine path set by the entry

    return ui_lib


def snapshot(current: str | None, force: bool = False) -> dict:
    """Picker data: selectable + repairable models, drift, offline state."""
    lib = _ui_lib()
    snap = lib.catalog_snapshot(force=force)
    ids = list(snap["catalog"].get("ids") or [])
    verified = list(snap["verified"] or [])
    primary = snap["primary"]

    # Auto-prune (2026-10-05): a FRESH fetch with real drift removes models GO
    # no longer offers from vision_verified; overrides stay for re-adds.
    cat = snap["catalog"]
    pruned: list[str] = []
    if not cat.get("from_cache") and cat.get("removed"):
        try:
            pruned = lib.prune_vision_verified(list(cat["removed"]))
        except Exception:  # noqa: BLE001 — pruning must never break the picker
            pruned = []
        if pruned:
            import config

            verified = list(config.load_models().get("vision_verified") or [])

    def label(model: str) -> str:
        """Cost-only suffix for pickers (owner 2026-10-04): name + price.

        Everything selectable is vision-verified by construction, so the
        verified marker/context/measured hints are noise here.
        """
        rec = (snap["dev"]["models"] or {}).get(model) or {}
        ci, co = rec.get("cost_input"), rec.get("cost_output")
        if isinstance(ci, (int, float)) and isinstance(co, (int, float)):
            return f"${ci:g}/${co:g}"
        return ""

    choice_ids: list[str] = []
    for model in [current, primary, *verified]:
        if not model or model in choice_ids:
            continue
        if model == current or model == primary or not ids or model in ids:
            choice_ids.append(model)

    return {
        "offline": snap["offline"],
        "catalog_error": snap["catalog"].get("error"),
        "catalog_new": list(snap["catalog"].get("new") or []),
        "catalog_removed": list(snap["catalog"].get("removed") or []),
        "catalog_ids": ids,
        "catalog_fetched_at": snap["catalog"].get("fetched_at"),
        "catalog_from_cache": bool(snap["catalog"].get("from_cache")),
        "catalog_stale": bool(snap["catalog"].get("stale")),
        "pruned": pruned,
        "primary": primary,
        "current": current,
        "choices": [
            {"id": m, "label": label(m), "verified": m in verified}
            for m in choice_ids
        ],
        "unverified": [
            {"id": m, "label": label(m)}
            for m in (snap["candidates"] or []) if m not in verified
        ][:25],
        "text_only": list(snap["text_only"] or [])[:15],
        "probe_cost": round(
            lib.probe_cost_estimate(list(snap["candidates"] or [])[:5], snap), 4),
    }


def verify(model: str) -> str:
    """One paid probe (~100-300 tokens) — record success in the user config."""
    model = (model or "").strip()
    if not model:
        raise ValueError("model is required")
    return _ui_lib().verify_model(model)


def rates() -> dict:
    """models.dev hint rates (USD per 1M tokens) for run cost estimates.

    Same source the Settings picker labels show. Community-maintained — an
    API-equivalent estimate, not a bill (the GO subscription is quota-based).
    Offline/missing models return null rates; callers must show "—".
    """
    import config
    import model_catalog

    m = config.load_models()
    dev = model_catalog.fetch_models_dev(endpoint=m.get("endpoint") or "")
    models: dict[str, dict] = {}
    for mid, rec in (dev.get("models") or {}).items():
        ci, co = rec.get("cost_input"), rec.get("cost_output")
        if isinstance(ci, (int, float)) or isinstance(co, (int, float)):
            models[mid] = {"input_per_1m": ci, "output_per_1m": co}
    return {
        "models": models,
        "primary_model": m.get("primary_model") or "",
        "verification_model": m.get("verification_model") or "",
        "source": "models.dev",
        "fetched_at": dev.get("fetched_at"),
        "from_cache": bool(dev.get("from_cache")),
        "error": dev.get("error"),
    }
