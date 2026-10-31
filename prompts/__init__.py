"""Prompt package (D44): `text.py` (all literal strings) + `compose.py` (all
runtime assembly).

The prompt is production code. Changing any literal is a prompt revision:
bump `text.PROMPT_VERSION`, update the golden fixtures deliberately
(tests/fixtures/prompts/), and run the paid eval matrix (`make eval`) before
accepting a regression. `compose.prompt_fingerprint(system, user)` is recorded
in every extraction's `_meta.prompt` so results trace back to the exact prompt
revision + assembled bytes.

Version history: v0 text baseline; extract_vision_v0 escalation; v1
vision-primary (D24); v2 document identification (D25); v3 discovery/pinning +
profile schemas (D26/D27); v4 vision-led identification (D29); v5
domain-neutral system role (D45) — the system prompt is a generic
document-reading discipline, every domain specific lives in the profile, and
`selected_document.name` replaced `.broker`; v6 domain-neutral catalog sweep
(D46) — free-form document `type`, no freight taxonomy; v7 line-item table
procedure (D47); v8 per-row source regions + template-declared line-item
columns (D48); v9 generic verification vocabulary (D49) — the schema's amount
column + template-declared row math, no tax/total wording. Retired modules
stay in git history only.
"""
