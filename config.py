"""Configuration loading + validation. The contract everything builds against.

Owns:
- `.env` loading (hand-rolled, no python-dotenv dependency — see D14)
- loading + validating `config/models.yaml`, `config/thresholds.yaml`,
  `rules/documents.yaml`, `rules/workflows.yaml`

Rules for this module (do not break):
- NEVER cache: every load_*() reads the file fresh on each call so the owner can
  edit YAML at any time without code changes (D10 hot-load).
- Validation fails loudly with `path: key ...` messages for wrong types and missing
  required keys; unknown keys produce warnings, not failures (owner may add fields).
- All file paths default to repo-relative locations anchored on this file, so the
  module works regardless of the process CWD.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

import yaml

import messages

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_PATH = PROJECT_ROOT / ".env"
MODELS_PATH = PROJECT_ROOT / "config" / "models.yaml"
THRESHOLDS_PATH = PROJECT_ROOT / "config" / "thresholds.yaml"
DOCUMENTS_PATH = PROJECT_ROOT / "rules" / "documents.yaml"
WORKFLOWS_PATH = PROJECT_ROOT / "rules" / "workflows.yaml"
EXPORTS_PATH = PROJECT_ROOT / "rules" / "exports.yaml"

# Fields the pipeline REQUIRES a thresholds.yaml entry for. D49: empty —
# per-field bars are profile-declared (D28 inline `threshold`), the YAML map
# is an optional fallback, and the generic schema + profile-declared names
# are allowed dynamically (ALLOWED_THRESHOLDS_KEYS below).
REQUIRED_THRESHOLDS: list[str] = []

# Document-quality gate floors (architecture.md D16). A floor value of 0 means
# "disabled" — every check is off-by-0 in the gate, so owners can relax freely.
# max_pixel_std_dev is in gray levels (0-255); the rest are 0-1 or counts >= 0.
QUALITY_KEYS = [
    "min_lines_per_page",      # int >= 0
    "min_words_per_page",      # int >= 0
    "min_latin_ratio",         # float 0-1
    "min_text_area_ratio",     # float 0-1
    "max_pixel_std_dev",       # float 0-255 (gray levels)
    "max_dark_pixel_ratio",    # float 0-1
    "review_band",             # float 0-1 (multiplier on floors)
    "quality_probe",           # bool
]

# Verification stack (2026-09-02): grounding/arithmetic/format/disagreement
# checks aggregated per field. Weights are the confidence of each independent
# check; floors map the weighted score to accept/review levels.
VERIFICATION_KEYS = [
    "grounding_margin_rel",    # float 0-0.5 (normalized page coords)
    "grounding_search_rel",    # float 0-0.5 (loose band for region imprecision)
    "grounding_loose_score",   # float 0-1 (loose-band confirmation score)
    "containment_ngram",       # int >= 2
    "check_weights",           # {check: weight} summing ~1.0
    "accept_score",            # float 0-1 (>= this -> accept)
    "review_score",            # float 0-1 (below this -> review_human)
    "hard_fail_checks",        # [check names] failing -> review_human
    "recheck_max_calls",       # int >= 0 (cap per document, cost discipline)
]
VERIFICATION_CHECK_NAMES = ("grounding", "arithmetic", "format", "disagreement")
ARITHMETIC_RULE_KEYS = ["rule", "target", "fields", "tolerance"]
ARITHMETIC_RULE_KINDS = ("line_items_sum_to", "sum_equals")

# Cross-document validation rules (2026-09-07, D32): a workflow may declare
# `cross_checks` — equality rules between a field extracted on one document
# entry and a field on another entry of the SAME file ("BOE number on the
# broker invoice must equal the BOE number on the attached BOE"). Both sides
# are {template, field} pairs; templates must be in the workflow's `documents`
# (they must be extracted to be compared). Equality kinds are the profile
# field types: date only equals date, amount only equals amount, str/name mix
# freely (kind-aware normalization, never raw string equality).
CROSS_RULE_KEYS = ["id", "label", "left", "right"]
CROSS_SIDE_KEYS = ["template", "field"]
CROSS_MAX_RULES = 20  # sanity cap per workflow
_CROSS_KIND = {"amount": "amount", "date": "date", "currency": "currency",
               "str": "string", "name": "name", "object": "object"}

# Excel export profiles (D54): an ordered column list mapping document
# templates/fields (and line items / meta / cross-checks) to a workbook.
# Sources are ONE of: {meta: ...}, {template, field}, {line_item: ...},
# {cross_check: rule_id}. `grain: line_item` expands one row per line item of
# `line_item_template` (first found entry); `grain: file` = one row per file.
EXPORT_GRAINS = ("file", "line_item")
EXPORT_META_SOURCES = ("original", "stem", "status", "flags_pending",
                       "flags_total", "reviewed", "validation_status")
EXPORT_COLUMN_TYPES = ("auto", "text", "number", "date")
ALLOWED_EXPORT_KEYS = ["id", "label", "workflow", "grain", "line_item_template",
                       "columns", "options"]
ALLOWED_EXPORT_COLUMN_KEYS = ["header", "source", "type"]
ALLOWED_EXPORT_OPTION_KEYS = ["date_format", "require_reviewed",
                              "include_issues", "include_line_items_sheet"]


def cross_kinds_compatible(t1: str, t2: str) -> bool:
    """Can two profile field types be compared for equality? object can never
    participate (no scalar); if either side is amount or date both sides must
    match exactly (amount only equals amount, date only equals date); str/name
    mix freely (both normalize to string comparison semantics)."""
    k1, k2 = _CROSS_KIND.get(t1), _CROSS_KIND.get(t2)
    if k1 is None or k2 is None or "object" in (k1, k2):
        return False
    if k1 in ("amount", "date", "currency") or k2 in ("amount", "date", "currency"):
        return k1 == k2
    return True


# Top-level keys that are allowed but not part of the validated contract.
ALLOWED_MODELS_KEYS = ["primary_model", "primary_mode", "image_url_form", "api_style",
                       "endpoint", "verification_model", "model_overrides",
                       "vision_verified", "model_notes"]
# Generic minimal schema (D26) — the fields non-invoice documents extract with.
GENERIC_SCHEMA_FIELDS = ("document_title", "document_date", "document_reference", "document_amount")
# Threshold resolution fallback per field type (D28): profile field ->
# thresholds.yaml -> type default. Owner overrides with `threshold` in the
# profile field definition or thresholds.yaml — never code.
FIELD_TYPE_DEFAULTS = {"amount": 0.95, "date": 0.9, "name": 0.9, "currency": 0.9,
                       "str": 0.8, "object": 0.8}
DEFAULT_MIN_SCORE = 0.5  # profile signals.min_score fallback (selection bar)
# Document-identification profile contract (D25/D28): signals = user-defined
# keywords (presence rule) + min_score; extraction = profile-defined fields.
ALLOWED_DOCUMENT_KEYS = ["id", "label", "description", "signals", "extraction",
                         "allow_no_match"]
ALLOWED_SIGNAL_KEYS = ["keywords", "min_score"]
# Fieldless profiles use the generic minimal schema (D26). The legacy fixed_v1
# broker schema was retired 2026-09-21 — profiles are field-based or generic.
ALLOWED_EXTRACTION_SCHEMAS = ["generic"]
ALLOWED_EXTRACTION_KEYS = ["schema", "fields", "line_items", "line_items_note",
                           "line_items_columns", "arithmetic", "row_arithmetic"]
FIELD_TYPES = ("str", "date", "amount", "name", "currency", "object")
# Line-item columns (D48/D49): a template may declare its own row columns.
# Roles are the ONLY semantics the checker and the prompt rely on, and they
# are deliberately minimal and domain-neutral:
#   - `description` (the row's text) and `amount` (the row's primary amount)
#     are required when columns are declared.
#   - Row arithmetic (taxable + tax == total, or any other row invariant) is
#     TEMPLATE DATA: `extraction.row_arithmetic` rules evaluated per row.
#   - Columns without a role are free data columns (referenced by row rules).
# Legacy role alias (D49): `total` reads as `amount`; legacy `taxable`/`tax`
# roles are dropped to plain columns (their math must be declared in
# `row_arithmetic`) — warned, never silent.
LINE_ITEM_ROLES = ("description", "amount")
LEGACY_ROLE_ALIASES = {"total": "amount"}
LEGACY_ROLE_DROPS = ("taxable", "tax")
ALLOWED_COLUMN_KEYS = ["name", "description", "type", "role"]
ROW_ARITHMETIC_MISSING_MODES = ("zero",)
# Default columns for profiles that enable line_items without declaring
# columns (D49 — generic two-column default: the row's text + its amount).
DEFAULT_LINE_ITEM_COLUMNS = (
    {"name": "description", "type": "str", "role": "description",
     "description": "the row's full printed text (what the row is)"},
    {"name": "amount", "type": "amount", "role": "amount",
     "description": "the row's own amount as printed (mandatory whenever printed)"},
)
ALLOWED_FIELD_KEYS = ["name", "type", "description", "threshold", "labels", "anchor"]
# Structural anchor (2026-09-07, D33): where a field lives in a boxy,
# multi-part document (Bill of Entry...) — anchored to Part/Section, NEVER a
# page number (page counts vary between documents of a kind). value_side =
# where the value sits relative to its printed label (the learned geometry).
# All optional; rendered into the extraction prompt's field notes.
ANCHOR_KEYS = ["part", "section", "value_side"]
VALUE_SIDES = ("right", "left", "above", "below")
# Static allowed threshold keys (profile-declared names are seeded dynamically
# from rules/documents.yaml — see load_thresholds).
ALLOWED_THRESHOLDS_KEYS = (REQUIRED_THRESHOLDS + list(GENERIC_SCHEMA_FIELDS)
                           + ["quality"])


class ConfigError(ValueError):
    """A configuration file is present but invalid (missing key, wrong type, ...)."""


def _load_yaml_file(path: Path) -> dict:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"{path}: file not found")
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: malformed YAML: {exc}")
    if raw is None:
        raise ConfigError(f"{path}: empty file")
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping, got {type(raw).__name__}")
    return raw


def _check_mapping(data: Any, path: Path, what: str) -> dict:
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: '{what}' must be a mapping, got {type(data).__name__}")
    return data


def _check_list(data: Any, path: Path, what: str) -> list:
    if not isinstance(data, list):
        raise ConfigError(f"{path}: '{what}' must be a list, got {type(data).__name__}")
    return data


def _check_float(data: Any, path: Path, what: str, minimum: float = 0.0, maximum: float = 1.0) -> None:
    if not isinstance(data, (int, float)) or isinstance(data, bool):
        raise ConfigError(f"{path}: '{what}' must be a number, got {type(data).__name__}")
    if not minimum <= float(data) <= maximum:
        raise ConfigError(f"{path}: '{what}' must be between {minimum} and {maximum}, got {data}")


def _check_int(data: Any, path: Path, what: str, minimum: int = 0) -> None:
    if not isinstance(data, int) or isinstance(data, bool):
        raise ConfigError(f"{path}: '{what}' must be an integer, got {type(data).__name__}")
    if data < minimum:
        raise ConfigError(f"{path}: '{what}' must be >= {minimum}, got {data}")


def _warn_unknown_keys(data: dict, allowed: list, path: Path, where: str) -> None:
    for key in data:
        if key not in allowed:
            logger.warning("%s: unknown key '%s' under %s (typo?) — ignored", path, key, where)


def _warn(warnings: list, code: str, where: str, **values: Any) -> None:
    """Append a structured, human-readable warning (messages.WARNINGS)."""
    warnings.append(messages.warning_message(code, where=where, **values))


def _dedupe_warnings(warnings: list) -> list:
    seen: set = set()
    out: list = []
    for w in warnings:
        key = (w.get("code"), w.get("where")) if isinstance(w, dict) else str(w)
        if key in seen:
            continue
        seen.add(key)
        out.append(w)
    return out


def load_dotenv(path: Path = ENV_PATH) -> dict:
    """Load KEY=VALUE pairs from an optional .env file into os.environ.

    os.environ already-set values always win (standard practice — lets CI/agent
    environments supply variables without a file). A missing file is a warning,
    not an error. Returns the vars read from the file.
    """
    loaded: dict = {}
    if not path.exists():
        logger.warning("%s: not found — using environment variables only", path)
        return loaded
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            logger.warning("%s:%d: not a KEY=VALUE line, skipped: %r", path, lineno, line)
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if not key:
            logger.warning("%s:%d: empty key, skipped", path, lineno)
            continue
        if key not in os.environ:
            os.environ[key] = value
            loaded[key] = value
    return loaded


def load_models(path: Path | None = None) -> dict:
    """Load config/models.yaml: {'primary_model', 'primary_mode', 'endpoint'}.

    Vision-primary contract (D24, 2026-08-19): the single extraction pass reads
    page renders, so `primary_mode` must be "vision" — the text-LLM extraction
    path was removed with the pivot. Swap the model by editing `primary_model`.
    """
    path = path or MODELS_PATH
    data = _load_yaml_file(path)
    _warn_unknown_keys(data, ALLOWED_MODELS_KEYS, path, "models")
    if "primary_model" not in data:
        raise ConfigError(f"{path}: missing required key 'primary_model'")
    if not isinstance(data["primary_model"], str) or not data["primary_model"].strip():
        raise ConfigError(f"{path}: 'primary_model' must be a non-empty string")
    mode = data.get("primary_mode", "vision")
    if mode != "vision":
        raise ConfigError(
            f"{path}: 'primary_mode' must be 'vision' — the pipeline is "
            f"vision-primary (D24); a text-mode extraction path does not exist"
        )
    data["primary_mode"] = mode
    form = data.get("image_url_form", "flat")
    if form not in ("flat", "nested"):
        raise ConfigError(f"{path}: 'image_url_form' must be 'flat' or 'nested', got {form!r}")
    data["image_url_form"] = form
    style = data.get("api_style", "chat_completions")
    if style not in ("chat_completions", "responses"):
        raise ConfigError(f"{path}: 'api_style' must be 'chat_completions' or 'responses', got {style!r}")
    data["api_style"] = style
    endpoint = data.get("endpoint", "https://opencode.ai/zen/go/v1")
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise ConfigError(f"{path}: 'endpoint' must be a non-empty string")
    data["endpoint"] = endpoint.rstrip("/")
    # Verification stack Check 4 (gated recheck): a cheap second-opinion model,
    # "" = disabled. Per-model overrides for api_style/image_url_form when the
    # recheck model's transport differs from the primary's (documented in
    # config/models.yaml — the SWAP TRAP).
    vmodel = data.get("verification_model", "")
    if not isinstance(vmodel, str):
        raise ConfigError(f"{path}: 'verification_model' must be a string ('' = disabled)")
    data["verification_model"] = vmodel
    # Vision-probed models (model_catalog.probe_vision; UI "Verify") — the ONLY
    # models the UI offers as selectable (D35). Order is informational.
    verified = data.get("vision_verified") or []
    if not isinstance(verified, list) or not all(
            isinstance(v, str) and v.strip() for v in verified):
        raise ConfigError(f"{path}: 'vision_verified' must be a list of non-empty "
                          f"model ids")
    data["vision_verified"] = verified
    # Short, MEASURED hints shown next to the model in the UI picker (D43) —
    # e.g. "fastest: 12.8s/1p, 18.9s/8p (2026-09-25)". Documentation only:
    # nothing reads them for routing or validation decisions.
    notes = data.get("model_notes") or {}
    if not isinstance(notes, dict):
        raise ConfigError(f"{path}: 'model_notes' must be a mapping of model -> note")
    for model, note in notes.items():
        if not isinstance(model, str) or not model.strip():
            raise ConfigError(f"{path}: model_notes keys must be non-empty model ids")
        if not isinstance(note, str) or not note.strip():
            raise ConfigError(f"{path}: model_notes.{model} must be a non-empty string")
    data["model_notes"] = notes
    overrides = data.get("model_overrides") or {}
    if not isinstance(overrides, dict):
        raise ConfigError(f"{path}: 'model_overrides' must be a mapping of model -> options")
    for model, opts in overrides.items():
        if not isinstance(opts, dict):
            raise ConfigError(f"{path}: model_overrides.{model} must be a mapping")
        _warn_unknown_keys(opts, ["api_style", "image_url_form"], path, f"model_overrides.{model}")
        if "api_style" in opts and opts["api_style"] not in ("chat_completions", "responses"):
            raise ConfigError(f"{path}: model_overrides.{model}.api_style must be "
                              f"'chat_completions' or 'responses'")
        if "image_url_form" in opts and opts["image_url_form"] not in ("flat", "nested"):
            raise ConfigError(f"{path}: model_overrides.{model}.image_url_form must be "
                              f"'flat' or 'nested'")
    data["model_overrides"] = overrides
    return data


def load_thresholds(path: Path | None = None) -> dict:
    """Load config/thresholds.yaml: {'thresholds': {...}, 'quality': {...},
    'verification': {...}}."""
    path = path or THRESHOLDS_PATH
    data = _load_yaml_file(path)
    _warn_unknown_keys(data, ["thresholds", "quality", "verification"],
                       path, "top level")
    thresholds = _check_mapping(data.get("thresholds"), path, "thresholds")
    known_thresholds = list(ALLOWED_THRESHOLDS_KEYS) + profile_field_names()
    _warn_unknown_keys(thresholds, known_thresholds, path, "thresholds")
    for key in REQUIRED_THRESHOLDS:
        if key not in thresholds:
            raise ConfigError(f"{path}: missing required threshold '{key}'")
        _check_float(thresholds[key], path, f"thresholds.{key}")

    # Document-quality gate floors (D16).
    quality = _check_mapping(data.get("quality"), path, "quality")
    _warn_unknown_keys(quality, QUALITY_KEYS, path, "quality")
    for key in QUALITY_KEYS:
        if key not in quality:
            raise ConfigError(f"{path}: missing required quality key '{key}'")
    _check_int(quality["min_lines_per_page"], path, "quality.min_lines_per_page")
    _check_int(quality["min_words_per_page"], path, "quality.min_words_per_page")
    _check_float(quality["min_latin_ratio"], path, "quality.min_latin_ratio")
    _check_float(quality["min_text_area_ratio"], path, "quality.min_text_area_ratio")
    _check_float(quality["max_pixel_std_dev"], path, "quality.max_pixel_std_dev",
                 maximum=255.0)
    _check_float(quality["max_dark_pixel_ratio"], path, "quality.max_dark_pixel_ratio")
    _check_float(quality["review_band"], path, "quality.review_band")
    if not isinstance(quality["quality_probe"], bool):
        raise ConfigError(f"{path}: 'quality.quality_probe' must be a boolean")

    # Verification stack (2026-09-02): weights + floors for the per-field
    # check aggregation. Tunable by the owner; tuned against the golden set.
    verification = _check_mapping(data.get("verification"), path, "verification")
    _warn_unknown_keys(verification, VERIFICATION_KEYS, path, "verification")
    for key in VERIFICATION_KEYS:
        if key not in verification:
            raise ConfigError(f"{path}: missing required verification key '{key}'")
    _check_float(verification["grounding_margin_rel"], path, "verification.grounding_margin_rel",
                 maximum=0.5)
    _check_float(verification["grounding_search_rel"], path, "verification.grounding_search_rel",
                 maximum=0.5)
    _check_float(verification["grounding_loose_score"], path, "verification.grounding_loose_score")
    _check_int(verification["containment_ngram"], path, "verification.containment_ngram",
               minimum=2)
    weights = _check_mapping(verification["check_weights"], path, "verification.check_weights")
    _warn_unknown_keys(weights, list(VERIFICATION_CHECK_NAMES), path,
                       "verification.check_weights")
    total_w = 0.0
    for name in VERIFICATION_CHECK_NAMES:
        if name not in weights:
            raise ConfigError(f"{path}: verification.check_weights missing '{name}'")
        _check_float(weights[name], path, f"verification.check_weights.{name}")
        total_w += float(weights[name])
    if abs(total_w - 1.0) > 0.01:
        logger.warning("%s: verification.check_weights sum to %.2f, not ~1.0",
                       path, total_w)
    _check_float(verification["accept_score"], path, "verification.accept_score")
    _check_float(verification["review_score"], path, "verification.review_score")
    if verification["review_score"] > verification["accept_score"]:
        raise ConfigError(f"{path}: verification.review_score must be <= accept_score")
    hard = _check_list(verification["hard_fail_checks"], path, "verification.hard_fail_checks")
    for name in hard:
        if name not in VERIFICATION_CHECK_NAMES:
            raise ConfigError(f"{path}: verification.hard_fail_checks '{name}' "
                              f"must be one of {VERIFICATION_CHECK_NAMES}")
    _check_int(verification["recheck_max_calls"], path, "verification.recheck_max_calls")
    return data


def profile_field_names() -> list[str]:
    """All field names declared by document profiles (D28 — dynamic whitelist:
    every profile-declared field is a known threshold name)."""
    try:
        docs = load_documents()
    except ConfigError:
        return []
    return [f["name"] for d in docs["documents"]
            for f in d.get("extraction", {}).get("fields", [])]


def profile_field_def(docs: dict, doc_id: str, name: str) -> dict | None:
    """The profile's field definition {name, type, description, threshold,
    labels} for one extraction field, or None if undeclared."""
    if not doc_id:
        return None
    for d in docs.get("documents", []):
        if d.get("id") != doc_id:
            continue
        for f in d.get("extraction", {}).get("fields", []):
            if f.get("name") == name:
                return f
    return None


def profile_min_score(docs: dict, doc_id: str) -> float:
    """The profile's selection-confidence bar (signals.min_score or default)."""
    for d in docs.get("documents", []):
        if d.get("id") == doc_id:
            return (d.get("signals") or {}).get("min_score", DEFAULT_MIN_SCORE)
    return DEFAULT_MIN_SCORE


def profile_def(docs: dict, doc_id: str) -> dict | None:
    """The full profile dict for one document id, or None if undeclared."""
    return next((d for d in docs.get("documents", []) if d.get("id") == doc_id), None)


def profile_fields(docs: dict, doc_id: str) -> list[dict]:
    """The profile's user-defined extraction fields (D27) — [] for the
    fieldless generic minimal schema (D26). Canonical accessor used by the
    prompt assembly (`prompts/compose.py`) and extract.py's wrapper."""
    profile = profile_def(docs, doc_id) or {}
    return (profile.get("extraction") or {}).get("fields") or []


def line_item_columns(docs: dict, doc_id: str) -> list[dict]:
    """The profile's line-item columns (D48/D49) — declared in
    `extraction.line_items_columns`, or the generic defaults. Always contains
    exactly one `description` role and one `amount` role."""
    profile = profile_def(docs, doc_id) or {}
    cols = (profile.get("extraction") or {}).get("line_items_columns")
    return list(cols) if cols else [dict(c) for c in DEFAULT_LINE_ITEM_COLUMNS]


def line_item_column(docs: dict, doc_id: str, role: str) -> dict | None:
    """The column carrying `role` (description|amount), or None."""
    return next((c for c in line_item_columns(docs, doc_id) if c.get("role") == role), None)


def profile_row_arithmetic(docs: dict, doc_id: str) -> list[dict]:
    """The profile's per-row arithmetic rules (D49) — template data applied
    by verify.check_line_items to every line item."""
    profile = profile_def(docs, doc_id) or {}
    return list((profile.get("extraction") or {}).get("row_arithmetic") or [])


def load_documents(path: Path | None = None) -> dict:
    """Load rules/documents.yaml: {'documents': [profile, ...]} (D25/D28).

    Profile = identification (user-defined keywords + min_score) + extraction
    contract for ONE document kind. Signals (D28, vision-led): `keywords` are
    USER-DEFINED words that help recognise the document ("must contain these
    words", presence rule) — no broker registry, no PANs (D29); identification
    itself is the vision model reading the document's own name/heading, plus
    the user's pin. Empty signals = identification by description only.
    Extraction: profile-defined fields (name/type/description/threshold/
    labels — D27/D28) or the fieldless generic minimal schema (D26).

    The default path is resolved at CALL time (not bound at import) so the
    runtime DOCUMENTS_PATH constant is the single source of truth — same
    hot-load guarantee as everything else (D10).
    """
    path = path or DOCUMENTS_PATH
    data = _load_yaml_file(path)
    _warn_unknown_keys(data, ["documents"], path, "top level")
    documents = _check_list(data.get("documents"), path, "documents")
    if not documents:
        raise ConfigError(f"{path}: documents list is empty — the pipeline needs a target document")
    seen = set()
    for i, doc in enumerate(documents):
        where = f"documents[{i}]"
        doc = _check_mapping(doc, path, where)
        _warn_unknown_keys(doc, ALLOWED_DOCUMENT_KEYS, path, where)
        for key in ("id", "label", "description"):
            if key not in doc:
                raise ConfigError(f"{path}: {where} missing required key '{key}'")
            if not isinstance(doc[key], str) or not doc[key].strip():
                raise ConfigError(f"{path}: {where}.{key} must be a non-empty string")
        if doc["id"] in seen:
            raise ConfigError(f"{path}: {where} duplicate id '{doc['id']}'")
        seen.add(doc["id"])

        signals = _check_mapping(doc.get("signals"), path, f"{where}.signals")
        _warn_unknown_keys(signals, ALLOWED_SIGNAL_KEYS, path, f"{where}.signals")
        if "keywords" in signals:
            _check_list(signals["keywords"], path, f"{where}.signals.keywords")
            for kw in signals["keywords"]:
                if not isinstance(kw, str) or not kw.strip():
                    raise ConfigError(f"{path}: {where}.signals.keywords must contain only non-empty strings")
        min_score = signals.get("min_score")
        if min_score is not None:
            _check_float(min_score, path, f"{where}.signals.min_score")

        extraction = _check_mapping(doc.get("extraction"), path, f"{where}.extraction")
        _warn_unknown_keys(extraction, ALLOWED_EXTRACTION_KEYS, path, f"{where}.extraction")
        fields = extraction.get("fields")
        if fields is not None:
            # D27/D28: per-document field schema — each field = {name, type,
            # description?, threshold?, labels?}. Extraction uses exactly these
            # fields; threshold/labels are optional self-contained wiring.
            _check_list(fields, path, f"{where}.extraction.fields")
            if not fields:
                raise ConfigError(f"{path}: {where}.extraction.fields must not be empty")
            seen_fields = set()
            for j, field in enumerate(fields):
                fwhere = f"{where}.extraction.fields[{j}]"
                field = _check_mapping(field, path, fwhere)
                _warn_unknown_keys(field, ALLOWED_FIELD_KEYS, path, fwhere)
                if not isinstance(field.get("name"), str) or not field["name"].strip():
                    raise ConfigError(f"{path}: {fwhere}.name must be a non-empty string")
                if field["name"] in seen_fields:
                    raise ConfigError(f"{path}: {fwhere} duplicate field name '{field['name']}'")
                seen_fields.add(field["name"])
                ftype = field.get("type")
                if ftype not in FIELD_TYPES:
                    raise ConfigError(f"{path}: {fwhere}.type must be one of {FIELD_TYPES}")
                if "description" in field and (not isinstance(field["description"], str)
                                               or not field["description"].strip()):
                    raise ConfigError(f"{path}: {fwhere}.description must be a non-empty string")
                if "threshold" in field:
                    _check_float(field["threshold"], path, f"{fwhere}.threshold")
                if "labels" in field:
                    _check_list(field["labels"], path, f"{fwhere}.labels")
                    for lab in field["labels"]:
                        if not isinstance(lab, str) or not lab.strip():
                            raise ConfigError(
                                f"{path}: {fwhere}.labels must contain only non-empty strings")
                if "anchor" in field:
                    anchor = _check_mapping(field["anchor"], path, f"{fwhere}.anchor")
                    _warn_unknown_keys(anchor, ANCHOR_KEYS, path, f"{fwhere}.anchor")
                    for ak in ("part", "section"):
                        if ak in anchor and (not isinstance(anchor[ak], str)
                                             or not anchor[ak].strip()):
                            raise ConfigError(f"{path}: {fwhere}.anchor.{ak} "
                                              f"must be a non-empty string")
                    if "value_side" in anchor:
                        if anchor["value_side"] not in VALUE_SIDES:
                            raise ConfigError(
                                f"{path}: {fwhere}.anchor.value_side must be one of "
                                f"{VALUE_SIDES}, got {anchor['value_side']!r}")
        else:
            schema = extraction.get("schema")
            if schema not in ALLOWED_EXTRACTION_SCHEMAS:
                raise ConfigError(
                    f"{path}: {where}.extraction.schema must be one of {ALLOWED_EXTRACTION_SCHEMAS}")
        if "line_items_note" in extraction and (not isinstance(extraction["line_items_note"], str)
                                                or not extraction["line_items_note"].strip()):
            raise ConfigError(f"{path}: {where}.extraction.line_items_note must be a non-empty string")
        if not isinstance(extraction.get("line_items", True), bool):
            raise ConfigError(f"{path}: {where}.extraction.line_items must be a boolean")
        # D48: template-declared line-item columns.
        cols = extraction.get("line_items_columns")
        if cols is not None:
            if extraction.get("line_items") is False:
                raise ConfigError(f"{path}: {where}.extraction.line_items_columns "
                                  f"requires line_items: true")
            _check_list(cols, path, f"{where}.extraction.line_items_columns")
            if not cols:
                raise ConfigError(f"{path}: {where}.extraction.line_items_columns "
                                  f"must not be empty")
            seen_cols: set[str] = set()
            seen_roles: set[str] = set()
            for k, col in enumerate(cols):
                cwhere = f"{where}.extraction.line_items_columns[{k}]"
                col = _check_mapping(col, path, cwhere)
                _warn_unknown_keys(col, ALLOWED_COLUMN_KEYS, path, cwhere)
                for key in ("name", "description"):
                    if not isinstance(col.get(key), str) or not col[key].strip():
                        raise ConfigError(f"{path}: {cwhere}.{key} must be a non-empty string")
                if col["name"] in seen_cols:
                    raise ConfigError(f"{path}: {cwhere} duplicate column name '{col['name']}'")
                seen_cols.add(col["name"])
                if col.get("type", "str") not in FIELD_TYPES:
                    raise ConfigError(f"{path}: {cwhere}.type must be one of {FIELD_TYPES}")
                role = col.get("role")
                if role is not None:
                    # D49 legacy alias: `total` -> `amount`; `taxable`/`tax`
                    # roles are dropped to plain columns (their math must be
                    # declared in `row_arithmetic`) — loud, never silent.
                    if role in LEGACY_ROLE_ALIASES:
                        logger.warning("%s: %s.role %r is a legacy alias — read as %r",
                                       path, cwhere, role, LEGACY_ROLE_ALIASES[role])
                        role = LEGACY_ROLE_ALIASES[role]
                        col["role"] = role
                    elif role in LEGACY_ROLE_DROPS:
                        logger.warning("%s: %s.role %r is retired (D49) — the column "
                                       "keeps its data; declare its math in "
                                       "row_arithmetic", path, cwhere, role)
                        col.pop("role")
                        role = None
                    if role is not None:
                        if role not in LINE_ITEM_ROLES:
                            raise ConfigError(f"{path}: {cwhere}.role must be one of "
                                              f"{LINE_ITEM_ROLES}")
                        if role in seen_roles:
                            raise ConfigError(f"{path}: {cwhere} duplicate role '{role}'")
                        seen_roles.add(role)
            missing_roles = [r for r in LINE_ITEM_ROLES if r not in seen_roles]
            if missing_roles:
                raise ConfigError(
                    f"{path}: {where}.extraction.line_items_columns must declare the "
                    f"role(s) {missing_roles} (description = the row's text, "
                    f"amount = the row's primary amount)")
        # Row arithmetic (D49): per-row invariants, template data. Only
        # sum_equals applies per row (line_items_sum_to is header-scoped).
        row_arith = extraction.get("row_arithmetic")
        if row_arith is not None:
            if extraction.get("line_items") is False:
                raise ConfigError(f"{path}: {where}.extraction.row_arithmetic "
                                  f"requires line_items: true")
            _check_list(row_arith, path, f"{where}.extraction.row_arithmetic")
            col_names = {c["name"] for c in (cols or [])}
            for k, rule in enumerate(row_arith):
                rwhere = f"{where}.extraction.row_arithmetic[{k}]"
                rule = _check_mapping(rule, path, rwhere)
                _warn_unknown_keys(rule, ARITHMETIC_RULE_KEYS + ["missing"], path, rwhere)
                if rule.get("rule") != "sum_equals":
                    raise ConfigError(f"{path}: {rwhere}.rule must be 'sum_equals' "
                                      f"(row rules are per-row invariants)")
                if not isinstance(rule.get("fields"), list) or not rule.get("fields"):
                    raise ConfigError(f"{path}: {rwhere}.fields must be a non-empty list")
                if not isinstance(rule.get("target"), str):
                    raise ConfigError(f"{path}: {rwhere}.target must be a string")
                if col_names:
                    unknown = [n for n in rule["fields"] + [rule["target"]]
                               if n not in col_names]
                    if unknown:
                        raise ConfigError(f"{path}: {rwhere} references undeclared "
                                          f"column(s) {unknown}")
                if "tolerance" in rule:
                    _check_float(rule["tolerance"], path, f"{rwhere}.tolerance")
                if "missing" in rule and rule["missing"] not in ROW_ARITHMETIC_MISSING_MODES:
                    raise ConfigError(f"{path}: {rwhere}.missing must be one of "
                                      f"{ROW_ARITHMETIC_MISSING_MODES}")
        # Verification-stack arithmetic invariants (Check 2, 2026-09-02): data.
        arith = extraction.get("arithmetic")
        if arith is not None:
            _check_list(arith, path, f"{where}.extraction.arithmetic")
            for k, rule in enumerate(arith):
                rwhere = f"{where}.extraction.arithmetic[{k}]"
                rule = _check_mapping(rule, path, rwhere)
                _warn_unknown_keys(rule, ARITHMETIC_RULE_KEYS, path, rwhere)
                if rule.get("rule") not in ARITHMETIC_RULE_KINDS:
                    raise ConfigError(
                        f"{path}: {rwhere}.rule must be one of {ARITHMETIC_RULE_KINDS}")
                if rule.get("rule") == "line_items_sum_to" and not isinstance(
                        rule.get("target"), str):
                    raise ConfigError(f"{path}: {rwhere}.target must be a string")
                if rule.get("rule") == "sum_equals":
                    if not isinstance(rule.get("fields"), list) or not rule.get("fields"):
                        raise ConfigError(f"{path}: {rwhere}.fields must be a non-empty list")
                    if not isinstance(rule.get("target"), str):
                        raise ConfigError(f"{path}: {rwhere}.target must be a string")
                if "tolerance" in rule:
                    _check_float(rule["tolerance"], path, f"{rwhere}.tolerance")
        if not isinstance(doc.get("allow_no_match", False), bool):
            raise ConfigError(f"{path}: {where}.allow_no_match must be a boolean")
    return {"documents": documents}


def load_workflows(path: Path | None = None) -> dict:
    """Load rules/workflows.yaml: {'workflows': [bundle, ...]} (2026-09-05).

    A workflow is a named multi-document bundle for BULK runs: the `documents`
    list is extracted in ONE call per file (--documents; order = prompt
    order); `expected` is the completeness CONTRACT and may EXCEED documents
    ("extract 3, expect 4" while a BOE template does not exist yet) — a
    missing expected member produces a missing_expected_document flag row.
    `cross_checks` (D32) are cross-document validation rules — an equality
    between a field on one document entry and a field on another entry of the
    same file; a rule template should be in `documents` (it must be extracted
    to be compared) and use profile-defined fields.

    D52 POLICY — dangling references WARN, never break: deleting/renaming a
    template in the UI must not make the app unloadable. A referenced profile
    that no longer exists (or became fieldless, or lost a rule field) yields a
    warning in the returned `warnings` list; the raw reference stays in the
    workflow (so the UI shows it), the execution path skips it, and `expected`
    members still produce missing_expected_document rows at run time. Only
    STRUCTURAL errors (wrong types, missing keys, duplicates) still raise.
    An empty `workflows: []` is a valid starting state (the starter config
    ships empty). Hot-loaded (D10).
    """
    path = path or WORKFLOWS_PATH
    data = _load_yaml_file(path)
    _warn_unknown_keys(data, ["workflows"], path, "top level")
    workflows = _check_list(data.get("workflows"), path, "workflows")
    if not workflows:
        return {"workflows": [], "warnings": []}
    docs_by_id = {d["id"]: d for d in load_documents()["documents"]}
    warnings: list[str] = []
    seen = set()
    for i, wf in enumerate(workflows):
        where = f"workflows[{i}]"
        wf = _check_mapping(wf, path, where)
        _warn_unknown_keys(wf, ["id", "label", "description", "documents",
                                "expected", "cross_checks"], path, where)
        for key in ("id", "label"):
            if key not in wf:
                raise ConfigError(f"{path}: {where} missing required key '{key}'")
            if not isinstance(wf[key], str) or not wf[key].strip():
                raise ConfigError(f"{path}: {where}.{key} must be a non-empty string")
        if wf["id"] in seen:
            raise ConfigError(f"{path}: {where} duplicate id '{wf['id']}'")
        seen.add(wf["id"])
        docs = _check_list(wf.get("documents"), path, f"{where}.documents")
        if not docs:
            raise ConfigError(f"{path}: {where}.documents must be a non-empty list")
        seen_docs = set()
        for did in docs:
            if not isinstance(did, str) or not did.strip():
                raise ConfigError(f"{path}: {where}.documents must contain only non-empty strings")
            if did in seen_docs:
                raise ConfigError(f"{path}: {where}.documents contains duplicate '{did}'")
            seen_docs.add(did)
            if did not in docs_by_id:
                _warn(warnings, "workflow_doc_missing", where,
                      workflow=wf["id"], template=did)
            elif not (docs_by_id[did].get("extraction", {}).get("fields") or []):
                _warn(warnings, "workflow_doc_fieldless", where,
                      workflow=wf["id"], template=did)
        expected = wf.get("expected")
        if expected is not None:
            expected = _check_list(expected, path, f"{where}.expected")
            ordered: list[str] = []
            seen_exp = set()
            for did in expected:
                if not isinstance(did, str) or not did.strip():
                    raise ConfigError(f"{path}: {where}.expected must contain only non-empty strings")
                if did in seen_exp:
                    raise ConfigError(f"{path}: {where}.expected contains duplicate '{did}'")
                seen_exp.add(did)
                ordered.append(did)
                if did not in docs_by_id:
                    _warn(warnings, "expected_missing", where, template=did)
            wf["expected"] = ordered
        if "description" in wf and (not isinstance(wf["description"], str)):
            raise ConfigError(f"{path}: {where}.description must be a string")
        # Cross-document validation rules (2026-09-07, D32).
        if "cross_checks" in wf:
            rules = _check_list(wf["cross_checks"], path, f"{where}.cross_checks")
            if len(rules) > CROSS_MAX_RULES:
                raise ConfigError(f"{path}: {where}.cross_checks has {len(rules)} rules "
                                  f"(max {CROSS_MAX_RULES})")
            seen_rules = set()
            for j, rule in enumerate(rules):
                rwhere = f"{where}.cross_checks[{j}]"
                rule = _check_mapping(rule, path, rwhere)
                _warn_unknown_keys(rule, CROSS_RULE_KEYS, path, rwhere)
                rid = rule.get("id")
                if not isinstance(rid, str) or not rid.strip():
                    raise ConfigError(f"{path}: {rwhere}.id must be a non-empty string")
                if rid in seen_rules:
                    raise ConfigError(f"{path}: {rwhere} duplicate cross-check id '{rid}'")
                seen_rules.add(rid)
                if "label" in rule and (not isinstance(rule["label"], str)
                                        or not rule["label"].strip()):
                    raise ConfigError(f"{path}: {rwhere}.label must be a non-empty string")
                left_ok = _validate_cross_side(rule.get("left"), path, f"{rwhere}.left",
                                               docs, docs_by_id, warnings)
                right_ok = _validate_cross_side(rule.get("right"), path, f"{rwhere}.right",
                                                docs, docs_by_id, warnings)
                left, right = rule["left"], rule["right"]
                if left["template"] == right["template"]:
                    raise ConfigError(f"{path}: {rwhere} left and right must be DIFFERENT "
                                      f"templates (a cross-document rule spans two documents) — "
                                      f"both are '{left['template']}'")
                if left_ok and right_ok:
                    lt = _field_type(docs_by_id, left["template"], left["field"])
                    rt = _field_type(docs_by_id, right["template"], right["field"])
                    if not cross_kinds_compatible(lt, rt):
                        _warn(warnings, "cross_kinds_incompatible", rwhere,
                              rule=rid,
                              left_field=f"{left['template']}.{left['field']}",
                              right_field=f"{right['template']}.{right['field']}",
                              left_kind=lt, right_kind=rt)
            wf["cross_checks"] = rules
    return {"workflows": workflows, "warnings": _dedupe_warnings(warnings)}


def _validate_cross_side(side: Any, path: Path, where: str,
                         wf_docs: list, docs_by_id: dict,
                         warnings: list[str]) -> bool:
    """Validate one {template, field} side of a cross-document rule.

    Structural errors (missing keys, wrong types) raise. D52: a dangling
    REFERENCE (template no longer exists, became fieldless, or lost the rule
    field) appends a warning and returns False — the rule stays declared (the
    UI shows it) but is skipped, never a crash.
    """
    side = _check_mapping(side, path, where)
    _warn_unknown_keys(side, CROSS_SIDE_KEYS, path, where)
    for key in ("template", "field"):
        if key not in side:
            raise ConfigError(f"{path}: {where} missing required key '{key}'")
        if not isinstance(side[key], str) or not side[key].strip():
            raise ConfigError(f"{path}: {where}.{key} must be a non-empty string")
    if side["template"] not in docs_by_id:
        _warn(warnings, "cross_template_missing", where,
              template=side["template"])
        return False
    if side["template"] not in wf_docs:
        _warn(warnings, "cross_template_not_in_workflow", where,
              template=side["template"])
        return False
    profile = docs_by_id[side["template"]]
    fields = (profile.get("extraction") or {}).get("fields")
    if not fields:
        _warn(warnings, "cross_template_fieldless", where,
              template=side["template"])
        return False
    if side["field"] not in {f["name"] for f in fields}:
        _warn(warnings, "cross_field_missing", where,
              template=side["template"], field=side["field"])
        return False
    return True


def _field_type(docs_by_id: dict, template: str, field: str) -> str:
    for f in (docs_by_id[template].get("extraction") or {}).get("fields", []):
        if f["name"] == field:
            return f.get("type", "str")
    return "str"


def load_exports(path: Path | None = None) -> dict:
    """Load rules/exports.yaml: {'exports': [...], 'warnings': [...]} (D54).

    An export profile maps a run's reviewed data to a typed Excel workbook:
    an ordered `columns` list (sources: meta | template+field | line_item |
    cross_check), a `grain` (`file` = one row per file; `line_item` = one row
    per line item of `line_item_template`, header fields repeated) and
    `options`. D52 policy applies: dangling template/field/line-item refs and
    a missing `workflow` reference WARN (blank cells at export time, never a
    crash); STRUCTURAL errors (bad types, missing keys, duplicates, unknown
    meta source) raise. An empty `exports: []` is a valid starting state.
    Hot-loaded (D10).
    """
    path = path or EXPORTS_PATH
    data = _load_yaml_file(path)
    _warn_unknown_keys(data, ["exports"], path, "top level")
    exports = _check_list(data.get("exports", []), path, "exports")
    docs_by_id = {d["id"]: d for d in load_documents()["documents"]}
    workflows = {w.get("id"): w for w in load_workflows()["workflows"]}
    warnings: list[str] = []
    seen: set[str] = set()
    for i, exp in enumerate(exports):
        where = f"exports[{i}]"
        exp = _check_mapping(exp, path, where)
        _warn_unknown_keys(exp, ALLOWED_EXPORT_KEYS, path, where)
        for key in ("id", "label"):
            if key not in exp:
                raise ConfigError(f"{path}: {where} missing required key '{key}'")
            if not isinstance(exp[key], str) or not exp[key].strip():
                raise ConfigError(f"{path}: {where}.{key} must be a non-empty string")
        if exp["id"] in seen:
            raise ConfigError(f"{path}: {where} duplicate export id '{exp['id']}'")
        seen.add(exp["id"])
        grain = exp.get("grain", "file")
        if grain not in EXPORT_GRAINS:
            raise ConfigError(f"{path}: {where}.grain must be one of {EXPORT_GRAINS}")
        exp["grain"] = grain
        wf_id = exp.get("workflow")
        if wf_id is not None:
            if not isinstance(wf_id, str) or not wf_id.strip():
                raise ConfigError(f"{path}: {where}.workflow must be a non-empty string")
            if wf_id not in workflows:
                _warn(warnings, "export_workflow_missing", where, workflow=wf_id)
        tpl = exp.get("line_item_template")
        if grain == "line_item":
            if not isinstance(tpl, str) or not tpl.strip():
                raise ConfigError(f"{path}: {where}.line_item_template is required "
                                  f"when grain is 'line_item'")
            if tpl not in docs_by_id:
                _warn(warnings, "export_line_item_template_missing", where,
                      template=tpl)
        elif tpl is not None and (not isinstance(tpl, str) or not tpl.strip()):
            raise ConfigError(f"{path}: {where}.line_item_template must be a non-empty string")
        columns = _check_list(exp.get("columns"), path, f"{where}.columns")
        if not columns:
            raise ConfigError(f"{path}: {where}.columns must not be empty")
        item_cols = ({c["name"] for c in line_item_columns(docs_by_id, tpl)} | {"amount", "index"}
                     if isinstance(tpl, str) and tpl in docs_by_id else set())
        for j, col in enumerate(columns):
            cwhere = f"{where}.columns[{j}]"
            col = _check_mapping(col, path, cwhere)
            _warn_unknown_keys(col, ALLOWED_EXPORT_COLUMN_KEYS, path, cwhere)
            if not isinstance(col.get("header"), str) or not col["header"].strip():
                raise ConfigError(f"{path}: {cwhere}.header must be a non-empty string")
            if col.get("type", "auto") not in EXPORT_COLUMN_TYPES:
                raise ConfigError(f"{path}: {cwhere}.type must be one of "
                                  f"{EXPORT_COLUMN_TYPES}")
            src = _check_mapping(col.get("source"), path, f"{cwhere}.source")
            kinds = [k for k in ("meta", "template", "line_item", "cross_check") if k in src]
            if len(kinds) != 1 or (kinds[0] == "template" and "field" not in src):
                raise ConfigError(f"{path}: {cwhere}.source must be exactly one of "
                                  f"{{meta: ...}}, {{template + field}}, {{line_item: ...}}, "
                                  f"{{cross_check: ...}}")
            _warn_unknown_keys(src, ["meta", "template", "field", "line_item",
                                     "cross_check"], path, f"{cwhere}.source")
            kind = kinds[0]
            if kind == "meta":
                if src["meta"] not in EXPORT_META_SOURCES:
                    raise ConfigError(f"{path}: {cwhere}.source.meta must be one of "
                                      f"{EXPORT_META_SOURCES}")
            elif kind == "template":
                for key in ("template", "field"):
                    if not isinstance(src.get(key), str) or not src[key].strip():
                        raise ConfigError(f"{path}: {cwhere}.source.{key} must be a "
                                          f"non-empty string")
                profile = docs_by_id.get(src["template"])
                if profile is None:
                    _warn(warnings, "column_template_missing", cwhere,
                          header=col["header"], template=src["template"])
                elif src["field"] not in {f["name"] for f in
                                          (profile.get("extraction") or {}).get("fields", [])}:
                    _warn(warnings, "column_field_missing", cwhere,
                          header=col["header"], template=src["template"],
                          field=src["field"])
            elif kind == "line_item":
                if grain != "line_item":
                    raise ConfigError(f"{path}: {cwhere}.source uses line_item but "
                                      f"{where}.grain is 'file' — set grain: line_item")
                if not isinstance(src["line_item"], str) or not src["line_item"].strip():
                    raise ConfigError(f"{path}: {cwhere}.source.line_item must be a "
                                      f"non-empty string")
                if item_cols and src["line_item"] not in item_cols:
                    _warn(warnings, "column_line_item_missing", cwhere,
                          header=col["header"], template=tpl,
                          column=src["line_item"])
            else:  # cross_check
                if not isinstance(src["cross_check"], str) or not src["cross_check"].strip():
                    raise ConfigError(f"{path}: {cwhere}.source.cross_check must be a "
                                      f"non-empty string")
                wf = workflows.get(wf_id) if isinstance(wf_id, str) else None
                if wf is not None and src["cross_check"] not in {
                        r.get("id") for r in (wf.get("cross_checks") or [])}:
                    _warn(warnings, "column_cross_check_missing", cwhere,
                          rule=src["cross_check"])
        exp["columns"] = columns
        options = exp.get("options") or {}
        options = _check_mapping(options, path, f"{where}.options")
        _warn_unknown_keys(options, ALLOWED_EXPORT_OPTION_KEYS, path, f"{where}.options")
        if "date_format" in options and not isinstance(options["date_format"], str):
            raise ConfigError(f"{path}: {where}.options.date_format must be a string")
        for key in ("require_reviewed", "include_issues", "include_line_items_sheet"):
            if key in options and not isinstance(options[key], bool):
                raise ConfigError(f"{path}: {where}.options.{key} must be a boolean")
        exp["options"] = options
    return {"exports": exports, "warnings": _dedupe_warnings(warnings)}


def load_all() -> dict:
    """Load every config file once and return {'models': ..., 'thresholds': ...,
    'documents': ..., 'workflows': ..., 'exports': ...}.

    Hot-load guarantee: this reads each file fresh, so the owner's latest edits
    are picked up on the next call/run. Used by process.py and tests.
    (2026-09-02: 'labels' removed with the retired rules locator — the rules
    validator is now the verification stack; per-profile inline labels in
    documents.yaml stay as documentation data. 2026-09-05: 'workflows' added —
    named multi-document bundles for bulk runs. 2026-09-28: 'exports' added —
    Excel export profiles, D54.)
    """
    load_dotenv()
    return {
        "models": load_models(),
        "thresholds": load_thresholds(),
        "documents": load_documents(),
        "workflows": load_workflows(),
        "exports": load_exports(),
    }


def opencode_api_key() -> Optional[str]:
    """Return OPENCODE_API_KEY from the environment.

    Merges .env first (idempotent — os.environ already-set values win), so
    callers like llm_client.chat work standalone without a prior load_all().
    The key from https://opencode.ai/auth — same one `/connect` uses in the
    opencode TUI; authenticates the opencode Zen gateway endpoint (D4 note).
    """
    load_dotenv()
    return os.environ.get("OPENCODE_API_KEY")
