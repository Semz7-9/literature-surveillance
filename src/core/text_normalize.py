"""
Deterministic abstract text normalization.

Crossref (and other sources) may return abstracts as raw JATS/XML markup
(e.g. <jats:p>, <jats:italic>). Evidence spans and LLM extraction must
operate on the same plain-text view, not on markup-laden raw text, or
"quote appears verbatim in abstract" checks will spuriously fail.

This is deliberately NOT a full JATS pipeline (no table/formula/list
semantics) — just tag stripping + entity unescaping, versioned so that
future upgrades can be tracked against existing SourceSnapshots.
"""

import re
import html

NORMALIZER_VERSION = "jats-strip-v1"


def normalize_abstract_text(raw: str | None) -> str | None:
    """Strip XML/JATS tags and unescape entities, collapse whitespace."""
    if not raw:
        return None
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None
