/** Client-side mirror of the core message catalog for values that arrive as
 * raw JSON (Results' verification levels, legacy flag rows without the
 * server-provided labels). Keep in sync with messages.py. */

export const LEVEL_LABELS: Record<string, { label: string; body: string }> = {
  accept: {
    label: "Checked",
    body: "Every check passed for this value.",
  },
  review_human: {
    label: "Needs review",
    body: "A check didn't pass — confirm the value or correct it.",
  },
  review_secondary: {
    label: "Second opinion differs",
    body: "A second read of the same spot returned something else.",
  },
};

export function levelLabel(code: string | null | undefined): string {
  if (!code) return "";
  return LEVEL_LABELS[code]?.label ?? code.replace(/_/g, " ");
}

export function levelBody(code: string | null | undefined): string {
  if (!code) return "";
  return LEVEL_LABELS[code]?.body ?? "";
}

/** Fallback titles for legacy flags.csv rows that predate reason_label. */
export const REASON_TITLES: Record<string, string> = {
  verification_grounding: "Value not found on the page",
  verification_arithmetic: "The numbers don't add up",
  verification_format: "This doesn't look like the expected format",
  verification_disagreement: "A second read disagreed",
  missing_value: "Nothing was extracted",
  line_item_issue: "A line item doesn't add up",
  no_document_match: "This document wasn't found",
  below_threshold: "Low-confidence read",
  selection_conflict: "The selected pages don't match the document",
  document_quality: "Page skipped — not readable",
  invalid_source_region: "The page location was unusable",
  page_overlap: "Two documents share pages",
  duplicate_occurrence: "Possible duplicate document",
  missing_expected_document: "Expected document is missing",
  content_mismatch: "Value may belong to another file",
  unbound_document: "Document doesn't match the others",
  cross_document_mismatch: "Two documents disagree",
};

export function reasonTitle(
  code: string | null | undefined,
  fallback?: string | null,
): string {
  if (fallback) return fallback;
  if (!code) return "";
  return REASON_TITLES[code] ?? code.replace(/_/g, " ");
}

/** 'bill_of_entry.invoice_number__commercial_invoice.invoice_number' →
 *  'bill of entry · invoice number  =  commercial invoice · invoice number' */
export function prettyRule(rule?: string | null): string {
  if (!rule) return "";
  return rule
    .split("__")
    .map((side) =>
      side
        .split(".")
        .map((part) => part.replace(/_/g, " "))
        .join(" · "),
    )
    .join("  =  ");
}

export function statusPhrase(counts: {
  pending?: number;
  issue?: number;
  mismatch?: number;
  not_checked?: number;
}): string {
  const parts: string[] = [];
  const n = counts.pending ?? 0;
  if (n) parts.push(n === 1 ? "1 item needs review" : `${n} items need review`);
  if (counts.issue) parts.push(`${counts.issue} issue${counts.issue === 1 ? "" : "s"}`);
  if (counts.mismatch)
    parts.push(`${counts.mismatch} disagreement${counts.mismatch === 1 ? "" : "s"}`);
  if (counts.not_checked)
    parts.push(`${counts.not_checked} check${counts.not_checked === 1 ? "" : "s"} not run`);
  return parts.join(" · ") || "OK";
}
