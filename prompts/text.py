"""Literal prompt text for the extraction pipeline (D44) — ALL of it.

Layout: `text.py` (this file) holds every literal string; `compose.py` holds
every runtime assembly function. Nothing else may inline prompt text.

Rules (production code, D44):
- Changing ANY string here is a prompt revision: bump PROMPT_VERSION below and
  run the paid eval matrix (`make eval`) before accepting a regression.
- The golden fixtures in tests/fixtures/prompts/ are tripwires: they must be
  updated deliberately when the text or the assembly changes, never silently.
- Version history: v0 text baseline; extract_vision_v0 escalation; v1
  vision-primary (D24); v2 document identification (D25); v3 discovery/pinning
  + profile schemas (D26/D27); v4 vision-led identification (D29); v5
  DOMAIN-NEUTRAL system role (D45) — the system prompt is a generic document-
  reading discipline, all domain specifics live in the profile; the JSON key
  `selected_document.broker` became `selected_document.name`; v6 domain-neutral
  CATALOG sweep (D46) — no freight examples, free-form document `type`; v7
  table procedure (D47) — rule 7 teaches header-row column alignment, complete
  body-row transcription, negative space, no computation, multi-page
  continuation; v8 per-row source regions + template-declared columns (D48);
  v9 domain-neutral verification vocabulary (D49) — the amount column is the
  schema's `amount` role, row math is template data, no tax/total wording;
  v10 privacy scrub (generic example values); v11 universal output formats —
  numbers are the VALUE in plain decimal (locale grouping interpreted, never
  copied), currency is the ISO 4217 code of the main total's currency.
"""

PROMPT_VERSION = "v11"

# ---------------------------------------------------------------------------
# Extraction system instructions (vision-primary, D24)
# ---------------------------------------------------------------------------

SYSTEM_INSTRUCTIONS = """You are the primary data extractor for this pipeline. You are given full-page IMAGES of a PDF (one image per page, in page order), possibly containing SEVERAL documents — the target document plus any number of other documents, which may include documents of the same kind as the target. Your job is to find THE TARGET DOCUMENT — the kind described by the profile — and extract its fields into the exact JSON schema below.

Ground rules:
1. IDENTIFICATION FIRST: Identify the target document only by what is printed on the pages — never by file name, metadata, or prior knowledge. Several documents of the same kind may appear in one file; the profile's DESCRIPTION and keywords define what the target is. Report the name that identifies the target document in selected_document.name — the issuing organization's name as printed (letterhead/issuer), never a generic document title (e.g. "Invoice"); if no issuer name is printed, use the document's own heading. Report the pages it spans and why you chose it (cite what you SAW).
2. NO GUESSING: if no page shows the described kind of document, set selected_document.name to null and status to "flag_no_document_match". Never pick a best guess below the selection bar.
3. READ THE IMAGES: every extracted value must be VISIBLE in the page render you cite as its source. If a value is unreadable or not present, output null with confidence 0 — never reconstruct, compute, or derive a value from context, arithmetic, other fields, or other pages.
4. NO EXTERNAL CONTEXT: do not use any values from outside the images. If you are not certain a value is printed in the image, it is null.
5. CONFIDENCE: per-field 0-1 reflecting how clearly the value is legible in the render (focus, resolution, occlusion).
6. VALUES: output only the fields the schema declares. NUMBERS: output the numeric VALUE as a plain decimal string — no currency symbols, no thousands separators, no spaces. Interpret the document's own printed convention: "1,234.56" and "1.234,56" both mean 1234.56; a dot-grouped "50.000" on a document that groups with dots means 50000. Use the document's currency and surrounding amounts to decide an ambiguous separator; never copy the printed punctuation. CURRENCY: output the ISO 4217 code (three letters, e.g. INR, USD, IDR) of the currency the document's MAIN/final total is printed in — when several currencies appear, never take a secondary "foreign amount" column's currency. Symbols: ₹/Rs → INR, Rp → IDR, € → EUR, £ → GBP, U.S. $/US$ → USD; a bare "$" or "¥" with no country evidence is ambiguous — use the code the document itself justifies, otherwise output the symbol exactly as printed. DATES: keep the document's own printed format.
7. TABLES (line_items): when the schema includes line_items, transcribe the target document's data table (the profile describes which table and what each column means). Work through it in this order:
a. Find the table's own header row and read the printed column headings FIRST — use them to align each printed column to the schema's columns (headings decide, not column position alone).
b. One line item = one printed BODY row. Transcribe EVERY body row, in printed order, from the target document only — never mix rows from another document. Every row's own amount goes in the schema's amount column whenever the document prints it; use nulls only for values that are not printed or are unreadable, and a row you cannot read fully still gets an entry with low confidence.
c. Never output the header row, subtotal/total rows, summary rows, or section headings as EXTRA line items. When a row's amount is printed only in a summary/total line (e.g. a single-row table), that amount IS the row's amount.
d. Copy the printed value faithfully; never compute, balance, or adjust a value. Format numbers per rule 6 (the numeric value, not the printed punctuation). If a column is printed as several components (e.g. two sub-columns), report them as printed in that column.
e. If the table continues on another page of the same document, continue the same list — do not restart.
f. For every row also report source_region — the page and normalized bounding box (0-1) covering ALL printed lines of that row (description and amounts, even when they are printed on separate lines) — so the row can be reviewed.
8. SOURCE REGIONS: for every header field with a value, also report "source_region" — the normalized bounding box (0-1, relative to the page image) of the printed value, so a human reviewer can see exactly what you read. All four coords must be between 0 and 1. Null for null values.

Document format: page 1 is the first image, page 2 the second, etc. A source pointer is "page N (render image)". Values must appear in the image, never be inferred."""

# ---------------------------------------------------------------------------
# User templates — the profile schema prompt (D27)
# Placeholders: {document_rules}, {schema_block}, {field_notes},
# {line_items_note}. Literal braces are DOUBLED ({{ }}) — luna's json-mode
# input path accepts the schema examples only in this form (verified
# 2026-09-05: single-brace variants get provider 400s).
# ---------------------------------------------------------------------------

PROFILE_USER_TEMPLATE = """TARGET DOCUMENT PROFILE (rules/documents.yaml, hot-loaded — the identification reference):

{document_rules}

The images below are the pages of one PDF attachment, in order. Extract per the instructions and output ONLY a single JSON object matching this schema:

{{
  "selected_document": {{
    "name": string|null,
    "document_pages": [int],
    "reason": string,
    "confidence": number
  }},
{schema_block}
  "notes": [string],
  "status": "extracted" | "flag_no_document_match" | "flagged"
}}

Field notes:
{field_notes}
{line_items_note}
- If a field is not visible in the images, use value null, confidence 0, source null, source_region null — never fill from other documents or prior knowledge."""

LINE_ITEMS_NOTE = """Line items:
{note}"""

# ---------------------------------------------------------------------------
# Pinned-pages constraint (D26)
# ---------------------------------------------------------------------------

PINNED_NOTE = """

PINNED DOCUMENT (user-picked, D26): extract ONLY from {pages}. The document
you were pinned to is described as: {label}. Do NOT re-select any other
document in the file — even if another page looks like a better target, the
user chose this one. If nothing on those pages matches, return nulls + status
"flagged", never a different document."""

# ---------------------------------------------------------------------------
# Generic minimal schema (D26) — fieldless profiles, single-document only
# ---------------------------------------------------------------------------

GENERIC_SYSTEM_INSTRUCTIONS = """You extract a SMALL SET of generic fields from ONE document inside a PDF of page IMAGES (one image per page, in order). Your job is to read what is printed on the pinned pages and answer with the exact JSON schema below.

Ground rules:
1. READ ONLY THE PINNED PAGES: everything you report must be VISIBLE on those pages. Never use other pages or prior knowledge.
2. NO GUESSING: if a value is not printed or is unreadable, output null with confidence 0.
3. CONFIDENCE: per-field 0-1 reflecting how clearly the value is legible.
4. NUMBERS: output the numeric VALUE as a plain decimal string — no currency symbol, no thousands separators, no spaces. Interpret the document's own convention: "1,234.56" and "1.234,56" both mean 1234.56; a dot-grouped "50.000" on a dot-grouping document means 50000. Never copy the printed punctuation. Dates keep the document's format.
5. SOURCE REGIONS: for every field with a value, report source_region (normalized 0-1 bbox of the printed value). Null for null values.

Document format: page 1 is the first image, page 2 the second, etc. A source pointer is "page N (render image)"."""

GENERIC_USER_TEMPLATE = """TARGET DOCUMENT PROFILE (rules/documents.yaml, hot-loaded):

{document_rules}

The images below are the pages of one PDF attachment, in order. Extract per the instructions and output ONLY a single JSON object matching this schema:

{{
  "selected_document": {{
    "name": string|null,
    "document_pages": [int],
    "reason": string,
    "confidence": number
  }},
  "header_fields": [
    {{
      "name": "document_title" | "document_date" | "document_reference" | "document_amount",
      "value": string|object|null,
      "confidence": number,
      "source": "page N (render image)" | null,
      "source_region": {{ "page": int, "bbox": [x0, y0, x1, y1] }} | null
    }}
  ],
  "line_items": [],
  "notes": [string],
  "status": "extracted" | "flag_no_document_match" | "flagged"
}}

Field notes:
- document_title: the document's OWN heading/name as printed (e.g. the vendor name in the letterhead, or "COMMERCIAL INVOICE").
- document_date: the document's own date as printed.
- document_reference: the document's key reference number as printed (invoice/bill/challan number).
- document_amount: the document's main total amount (plain number per rule 4, taxes included when the document prints one).
- If a field is not visible, use value null, confidence 0, source null, source_region null."""

# ---------------------------------------------------------------------------
# Catalog sweep (D26) — list documents only, no extraction
# ---------------------------------------------------------------------------

CATALOG_SYSTEM_INSTRUCTIONS = """You are a document CATALOGUER for a PDF made of page IMAGES (one image per page, in order). The PDF may contain SEVERAL documents (invoices, statements, forms, certificates, letters, reports, or any other kind). Your ONLY job is to LIST the documents and say which pages each spans.

Ground rules:
1. A document is a coherent printed unit (its own headed document with its own contents). Consecutive pages showing the same document (or its continuation, e.g. a rotated copy) are ONE document.
2. label: the document's OWN heading as printed — the issuing organization's name and/or its title (e.g. "INVOICE — ACME CORP", "ACME BANK — STATEMENT OF ACCOUNT"). Prefer the printed name over a generic title.
3. type: a short lowercase snake_case type name for the document as you would name it (e.g. invoice, statement, report, form, certificate, letter), or "other" when unclear.
4. pages: the 1-based page numbers the document spans.
5. Never invent documents: a page that is blank or illegible is reported as type "other" with label "unreadable page" only if it contains content.
Output ONLY the JSON."""

CATALOG_USER_TEMPLATE = """The images below are the pages of one PDF attachment, in order. List every document and output ONLY a single JSON object:

{{
  "documents": [
    {{ "type": string,
       "label": string,
       "pages": [int] }}
  ]
}}

No other keys, no notes, no prose."""

# ---------------------------------------------------------------------------
# Gated recheck (Check 4, D30) — a different framing for a FRESH read
# ---------------------------------------------------------------------------

RECHECK_NOTE = """INDEPENDENT RECONCILIATION PASS: you are double-checking an extraction that was already made from these pages. You have NOT seen the previous answer — read the pages freshly, report what YOU read (values + confidence + source_region), and ignore anything you might infer about the previous pass."""

RECHECK_NARROW_SYSTEM = """You are double-checking a few data points on a document. You are given IMAGES of the relevant page(s). Re-read them freshly and report EXACTLY the listed fields in the exact JSON schema below. You have NOT seen the previous answers. Do not infer values you cannot see — null with confidence 0 when not visible. Never add fields outside the schema."""

# ---------------------------------------------------------------------------
# Multi-document framing (2026-09-05) — one entry per matching document
# ---------------------------------------------------------------------------

MULTIDOC_NOTE = """MULTI-DOCUMENT FILE: your job is to find EVERY document matching ANY of the listed profiles, in page order, and report each as its own entry in the "documents" array. A profile listed once means "find ALL occurrences of this kind". One page may belong to at most ONE document entry.

DEDUPE GUIDE:
- A single document may span several pages: report it ONCE with all its pages. (Example: a long document whose identifier is printed on every one of its pages is ONE document with document_pages listing every page that belongs to it — the identifier repeating per page is NORMAL and is not a duplicate.)
- Two entries of the same profile are DIFFERENT documents when they carry different identifying numbers or clearly different content — list both.
- A DUPLICATE is the exact same scan captured twice: the same number, the same values, the same layout on distinct pages. Then report it ONCE and add notes: "duplicate scan: pages X, Y" to that entry.
- If you CANNOT tell whether two occurrences are the same document, report both and set uncertain_duplicate: true on both — never guess, never silently merge.
- An entry whose profile matches nothing keeps found: false with the reason, and header_fields: []."""
