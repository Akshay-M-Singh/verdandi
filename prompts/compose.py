"""Runtime prompt assembly (D44) — the ONLY place prompt text is composed.

Pure string work over hot-loaded config (D10): every function reads the
current profile data and returns the exact (system, user) text pair the LLM
receives. `text.py` owns the literals; this module owns the composition.

Extract.py calls the build_* functions and records
`prompt_fingerprint(system, user)` in `_meta.prompt` (version + sha256), so
every extraction can be traced to the exact prompt revision that produced it.

Invariants (do not break):
- Literal braces in generated schema blocks stay DOUBLED ({{ }}) — the
  json-mode input convention verified 2026-09-05.
- The user message always contains the word "JSON" (json-object mode
  contract).
- Golden fixtures in tests/fixtures/prompts/ pin the assembled bytes.
"""

from __future__ import annotations

import hashlib

import config
from prompts import text


def _profile(cfg: dict, doc_id: str) -> dict:
    profile = config.profile_def(cfg["documents"], doc_id)
    if profile is None:
        raise ValueError(f"unknown document profile {doc_id!r} — rules/documents.yaml")
    return profile


def _anchor_suffix(f: dict) -> str:
    """Render a field's structural anchor (D33) into the prompt field notes —
    "Part 2 · Section B; value printed below the label". Page-agnostic by
    design (BOE page counts vary); the model finds the part wherever it is."""
    a = f.get("anchor") or {}
    loc = " · ".join(f"{k} {a[k]}" for k in ("part", "section") if a.get(k))
    bits = []
    if loc:
        bits.append(loc)
    side = a.get("value_side")
    if side:
        bits.append(f"value printed {side} of the label")
    return f" ({'; '.join(bits)})" if bits else ""


def render_column_notes(columns: list[dict]) -> str:
    """Per-column notes for the Line items section (D48): the template says
    what each row column means — the model's column-alignment reference."""
    return "\n".join(f"- {c['name']}: {c['description']}" for c in columns)


def _line_items_note(columns: list[dict], note: str) -> str:
    parts = [render_column_notes(columns)]
    if note:
        parts.append(note)
    return text.LINE_ITEMS_NOTE.format(note="\n".join(parts))


def render_line_item_row(columns: list[dict]) -> str:
    """The JSON example for ONE line-item row, generated from the template's
    declared columns (D48): description role -> string (mandatory text),
    amount -> string|null (plain decimal), others -> string|object|null.
    Rows carry their own source_region (D48 Phase B) for review/grounding."""
    parts = []
    for c in columns:
        if c.get("role") == "description":
            json_type = "string"
        elif c.get("type") == "object":
            json_type = "string|object|null"
        else:
            json_type = "string|null"
        parts.append(f'"{c["name"]}": {json_type}')
    parts += ['"confidence": number',
              '"source": "page N (render image)"',
              '"source_region": {{ "page": int, "bbox": [x0, y0, x1, y1] }} | null']
    return "{{ " + ", ".join(parts) + " }}"


def render_schema_block(fields: list[dict], line_items: bool,
                        columns: list[dict] | None = None) -> tuple[str, str]:
    """Generate the prompt's schema block + field notes + line-items note.

    All literal braces are doubled — the block is inserted through
    str.format into PROFILE_USER_TEMPLATE. `columns` = the template's
    line-item columns (D48; defaults to the legacy fixed columns). Returns
    (schema_block, field_notes).
    """
    assert fields, "render_schema_block needs >= 1 field"
    names = " | ".join(f'"{f["name"]}"' for f in fields)
    schema = (
        '  "header_fields": [\n'
        "    {{\n"
        f'      "name": {names},\n'
        '      "value": string|object|null,\n'
        '      "confidence": number,\n'
        '      "source": "page N (render image)" | null,\n'
        '      "source_region": {{ "page": int, "bbox": [x0, y0, x1, y1] }} | null\n'
        "    }}\n"
        "  ],\n"
    )
    if line_items:
        row = render_line_item_row(columns or list(config.DEFAULT_LINE_ITEM_COLUMNS))
        schema += f'  "line_items": [\n    {row}\n  ],\n'
    else:
        schema += '  "line_items": [],\n'
    notes = "\n".join(
        f"- {f['name']}: {f.get('description', 'as printed on the target document')}"
        f"{_anchor_suffix(f)}"
        for f in fields
    )
    return schema, notes


def render_multi_schema() -> str:
    """The documents-array wrapper schema (multi-document extraction).

    Braces DOUBLED ({{ }}) — the same escape convention as
    render_schema_block: luna's json-mode input path accepts the schema
    examples only in this form (verified 2026-09-05: single-brace variants get
    provider 400s).
    """
    return (
        '  "documents": [\n'
        "    {{\n"
        '      "template_id": "<one of the listed profile ids>",\n'
        '      "found": true,\n'
        '      "selected_document": {{ "name": string|null, "document_pages": [int], '
        '"reason": string|null, "confidence": number }},\n'
        '      "header_fields": [ {{ "name": "a field name of THIS template", '
        '"value": string|object|null, "confidence": number, '
        '"source": "page N (render image)"|null, '
        '"source_region": {{ "page": int, "bbox": [x0, y0, x1, y1] }}|null }} ],\n'
        '      "line_items": [ {{ "<a declared line-item column of THIS template>": value, '
        '"confidence": number, "source": "page N (render image)", '
        '"source_region": {{ "page": int, "bbox": [x0, y0, x1, y1] }}|null }} ] | [],\n'
        '      "notes": [string],\n'
        '      "uncertain_duplicate": false\n'
        "    }}\n"
        "  ],\n"
    )


def format_pages(pages: list[int]) -> str:
    """[1, 2, 3, 5] -> 'pages 1-3, 5'; [2] -> 'page 2'."""
    spans: list[list[int]] = []
    for p in sorted(pages):
        if spans and p == spans[-1][-1] + 1:
            spans[-1].append(p)
        else:
            spans.append([p])
    parts = []
    for span in spans:
        if len(span) == 1:
            parts.append(str(span[0]))
        else:
            parts.append(f"{span[0]}-{span[-1]}")
    joined = ", ".join(parts)
    return f"page {joined}" if len(spans) == 1 and len(pages) == 1 else f"pages {joined}"


def render_profile_block(cfg: dict, doc_id: str, signals_omit: bool = False) -> str:
    """Render the target document profile into the prompt (D29 identification
    reference).

    The profile (rules/documents.yaml, D10) carries the label, description and
    USER-DEFINED keywords (presence rule — "must contain these words"; the
    vision model does the identifying, the keywords are helper words for
    ambiguous same-name documents). No broker registry, no PANs (D29).

    D26 `signals_omit`: in PINNED mode the user's pin IS the identification —
    the signal block is omitted; label/description/schema context stays.
    """
    profile = _profile(cfg, doc_id)
    lines = [
        f"- id: {profile['id']} — {profile['label']}",
        f"  description: {profile['description']}",
    ]
    if not signals_omit:
        min_score = config.profile_min_score(cfg["documents"], doc_id)
        lines.append(
            f"  allow_no_match: {profile.get('allow_no_match', False)} "
            f"(below selector min_score {min_score} -> flag_no_document_match, never guess)"
        )
        keywords = (profile.get("signals") or {}).get("keywords") or []
        if keywords:
            lines.append(
                "  identification keywords (user-defined HINTS — a target "
                "document of this kind typically contains at least one; a "
                "document that matches the description but NO keyword is "
                "STILL the target — keywords never disqualify): "
                f"{keywords}"
            )
    schema = "profile" if config.profile_fields(cfg["documents"], doc_id) \
        else profile["extraction"].get("schema", "generic")
    lines.append(f"  extraction schema: {schema}")
    return "\n".join(lines)


def _append_variants(user: str, profile_label: str, pinned_pages: list[int] | None,
                     pinned_label: str | None, recheck: bool) -> str:
    """PINNED_NOTE + RECHECK_NOTE, in the same order the old inline code used."""
    if pinned_pages:
        user += text.PINNED_NOTE.format(pages=format_pages(pinned_pages),
                                        label=pinned_label or profile_label)
    if recheck:
        user += "\n\n" + text.RECHECK_NOTE
    return user


def build_single_prompt(cfg: dict, doc_id: str, *, pinned_pages: list[int] | None = None,
                        pinned_label: str | None = None, recheck: bool = False) -> tuple[str, str]:
    """The single-document (system, user) pair: profile schema, or the generic
    minimal schema for a fieldless profile."""
    profile = _profile(cfg, doc_id)
    fields = config.profile_fields(cfg["documents"], doc_id)
    line_items = profile["extraction"].get("line_items", True)
    columns = config.line_item_columns(cfg["documents"], doc_id) if line_items else None
    document_rules = render_profile_block(cfg, doc_id, signals_omit=bool(pinned_pages))
    if fields:
        schema_block, field_notes = render_schema_block(fields, line_items, columns)
        system = text.SYSTEM_INSTRUCTIONS
        user = text.PROFILE_USER_TEMPLATE.format(
            document_rules=document_rules,
            schema_block=schema_block,
            field_notes=field_notes,
            line_items_note=_line_items_note(
                columns, profile["extraction"].get("line_items_note", "")) if line_items else "",
        )
    else:
        system = text.GENERIC_SYSTEM_INSTRUCTIONS
        user = text.GENERIC_USER_TEMPLATE.format(document_rules=document_rules)
    return system, _append_variants(user, profile["label"], pinned_pages, pinned_label, recheck)


def build_multi_prompt(cfg: dict, doc_ids: list[str], line_items_map: dict, *,
                       pinned_pages: list[int] | None = None,
                       pinned_label: str | None = None,
                       recheck: bool = False) -> tuple[str, str]:
    """Compose the multi-document (system, user) pair: one profile block +
    generated schema per template, then the documents-array wrapper."""
    # json_mode enforcement (verified 2026-09-05): luna's json-object mode
    # requires the word "JSON" in the user message, like OpenAI's contract.
    blocks = ["Report every matching document in the exact JSON schema below. "
              "Profiles to find in this file (find ALL matching documents):"]
    for idx, doc_id in enumerate(doc_ids, start=1):
        profile = _profile(cfg, doc_id)
        fields = config.profile_fields(cfg["documents"], doc_id)
        line_items = line_items_map.get(doc_id, True)
        columns = config.line_item_columns(cfg["documents"], doc_id) if line_items else None
        schema_block, field_notes = render_schema_block(fields, line_items, columns)
        blocks.append(f"\n== Profile {idx}: {doc_id} — {profile['label']} ==")
        blocks.append(render_profile_block(cfg, doc_id))
        blocks.append("Schema for this document's fields:")
        # Braces stay DOUBLED ({{ }}) — the schema-example convention luna's
        # json-mode input validation accepts; verified live 2026-09-05.
        blocks.append(schema_block)
        blocks.append("Field notes:")
        blocks.append(field_notes)
        if line_items:
            blocks.append(_line_items_note(
                columns, profile["extraction"].get("line_items_note", "")))
    blocks.append("\n== Output array schema ==")
    blocks.append(render_multi_schema())
    system = text.SYSTEM_INSTRUCTIONS + "\n\n" + text.MULTIDOC_NOTE
    user = _append_variants("\n".join(blocks), "the pinned document(s)",
                            pinned_pages, pinned_label, recheck)
    return system, user


def build_recheck_prompt(cfg: dict, doc_id: str, field_defs: list[dict]) -> tuple[str, str]:
    """The narrow Check-4 (disagreement) recheck pair — disputed fields only,
    signals omitted (the recheck page set is the constraint), fresh-read
    framing."""
    schema_block, field_notes = render_schema_block(field_defs, line_items=False)
    document_rules = render_profile_block(cfg, doc_id, signals_omit=True)
    user = text.PROFILE_USER_TEMPLATE.format(
        document_rules=document_rules,
        schema_block=schema_block,
        field_notes=field_notes,
        line_items_note="",
    ) + "\n\n" + text.RECHECK_NOTE
    return text.RECHECK_NARROW_SYSTEM, user


def build_catalog_prompt() -> tuple[str, str]:
    """The discovery-sweep pair (lists documents; never extracts)."""
    return text.CATALOG_SYSTEM_INSTRUCTIONS, text.CATALOG_USER_TEMPLATE


def prompt_fingerprint(system: str | None, user: str) -> dict:
    """Provenance record for `_meta.prompt`: the prompt revision + the exact
    assembled-bytes hash (D44). The hash catches assembly/text changes even if
    PROMPT_VERSION was not bumped."""
    payload = f"{system or ''}\n\n{user}"
    return {"version": text.PROMPT_VERSION,
            "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest()}
