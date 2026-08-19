"""
SourceSnapshot creation and helper utilities.
"""

import hashlib
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Record, SourceSnapshot
from .text_normalize import normalize_abstract_text, NORMALIZER_VERSION


def _hash(text: str | None) -> str | None:
    return hashlib.sha256(text.encode()).hexdigest() if text else None


async def create_source_snapshot(
    session: AsyncSession,
    record: Record,
    source_name: str,
) -> SourceSnapshot:
    """
    Capture record.abstract as an immutable SourceSnapshot.

    Call this immediately after creating a Record so that evidence_spans
    grounding always references a stable, plain-text anchor instead of the
    mutable, possibly-markup-laden Record.abstract field.
    """
    raw = record.abstract
    analysis = normalize_abstract_text(raw)

    snapshot = SourceSnapshot(
        record_id=record.id,
        raw_abstract=raw,
        analysis_text=analysis,
        raw_hash=_hash(raw),
        analysis_hash=_hash(analysis),
        normalizer_version=NORMALIZER_VERSION if analysis else None,
        source_name=source_name,
    )
    session.add(snapshot)
    await session.flush()
    return snapshot
