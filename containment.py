"""Plain-Python fuzzy containment — no regex (D5), no third-party deps (D14).

Shared by the verification checks (grounding + disagreement diff). Deterministic
string operations only:

  1. Normalized-token match (the ONLY pass signal): the claimed value,
     normalized per its kind (amount/date/code via compare.norm_*), appears as
     a token in the region text -> score 1.0. STRICT by design: verification
     confirms the exact value; a one-character drift (a claimed reference
     missing a printed leading character, a date one day off) must NOT pass,
     or the check would rubber-stamp exactly the confident-wrong class it
     exists to catch.
  2. Character n-gram ratio (evidence only, NOT a pass signal): how much of
     the claimed value's n-grams appear in the region text — recorded in the
     check detail as the OCR-noise proximity measure (a date the OCR reads
     with a noise character reads ~0.8 — the text channel cannot confirm, and
     the flag says so truthfully).

Whitespace/punct stripping is plain string ops (no regex). The claimed value
matches a token (whitespace-split, punctuation edge-stripped) or a
reconstruction of ADJACENT tokens (D62: OCR-spaced digits "1 234 567",
multi-token names, "30 Jun 2025"); amounts/dates are compared via their
normalizers (which also absorb format noise: "1,234.00" == "1234.00",
"26/06/2025" == "26/06/2025").
"""

from __future__ import annotations

import re

from compare import amount_readings, norm_amount, norm_date, norm_key

DEFAULT_NGRAM = 3


def _tokens(text: str) -> list[str]:
    # "|" is a layout separator in the text channel (fused fragments like
    # "2,000.00|2.00"), never part of a value — split on it (D48).
    return [t for t in text.replace("|", " ").split() if t]


def _match_token(claimed: str, token: str, kind: str) -> bool:
    """Kind-aware single-token equality: does `token` equal `claimed`?.

    D62: edge punctuation is stripped more broadly — printed values routinely
    end a sentence or sit in quotes/parens ("30-Jun-2025.", "'1234567'",
    "(1,234.00)"); only the EDGES are stripped, so dotted dates survive.
    """
    claimed = claimed.strip().rstrip(":;,.!?\"'()[]")
    token = token.strip(":;-,|.!?\"'()[]")
    if kind == "amount":
        # 2026-10-06: separator-tolerant — the claimed canonical value may be
        # the numeric VALUE of the printed token ("50000" vs printed "50.000").
        claimed_amount = norm_amount(claimed)
        return claimed_amount is not None and claimed_amount in amount_readings(token)
    if kind == "date":
        a, b = norm_date(claimed), norm_date(token)
        return a is not None and b is not None and a == b
    return norm_key(claimed) == norm_key(token)


def _window_match(claimed: str, tokens: list[str], kind: str) -> bool:
    """Consecutive-token reconstruction of the claimed value (D62).

    The text channel splits values that print as one unit: OCR-spaced digits
    ("1 234 567" for 1234567) and multi-token values (names, "30 Jun 2025").
    Only a FULL reconstruction counts — a window whose normalized join equals
    the claimed normalized string — never a partial match. Digit-only windows
    need >= 5 digits (a short "12" must not match "1 2"); multi-token claimed
    values may join up to len(parts) + 2 adjacent tokens.
    """
    target = norm_key(claimed)
    if not target:
        return False
    parts = claimed.split()
    if len(parts) > 1:
        max_window = len(parts) + 2
        digit_only = False
    elif target.isdigit() and len(target) >= 5:
        max_window = 4
        digit_only = True
    else:
        return False
    for i in range(len(tokens)):
        joined = ""
        for j in range(i, min(i + max_window, len(tokens))):
            tok = tokens[j]
            if digit_only and not re.sub(r"[.,]", "", tok).isdigit():
                break
            joined += norm_key(tok)
            if joined == target:
                return True
            if len(joined) > len(target):
                break
    return False


def exact_containment(claimed: str, region_text: str, kind: str = "str") -> bool:
    """True iff the normalized claimed value appears in the region text.

    Single tokens match kind-aware (`_match_token`); values the channel split
    across adjacent tokens are reconstructed window-wise (`_window_match`).
    """
    tokens = _tokens(region_text)
    for token in tokens:
        if _match_token(claimed, token, kind):
            return True
    return _window_match(claimed, tokens, kind)


def ngram_ratio(claimed: str, region_text: str, k: int = DEFAULT_NGRAM) -> float:
    """Fraction of the claimed value's k-grams present in the region text (0-1).

    Informational closeness measure (evidence, never a pass). Short inputs
    fall back to a plain character-coverage ratio.
    """
    cleaned = [ch.lower() for ch in claimed if ch.isalnum()]
    if len(cleaned) < k:
        region = "".join(ch for ch in region_text.lower() if ch.isalnum())
        if not cleaned:
            return 0.0
        return sum(1 for ch in set(cleaned) if ch.lower() in region) / len(cleaned)
    grams = {"".join(cleaned[i:i + k]) for i in range(len(cleaned) - k + 1)}
    region_clean = [ch for ch in region_text.lower() if ch.isalnum()]
    region_grams = {"".join(region_clean[i:i + k])
                    for i in range(len(region_clean) - k + 1)}
    if not grams:
        return 0.0
    return sum(1 for g in grams if g in region_grams) / len(grams)