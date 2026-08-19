"""
SourceSnapshot creation and helper utilities.
"""

import hashlib
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Record, SourceSnapshot


async def create_source_snapshot(
    session: AsyncSession,
    record: Record,
    source_name: str,
) -> SourceSnapshot:
    """
    Capture record.abstract as an immutable SourceSnapshot.

    Call this immediately after creating a Record so that EvidenceSpans
    (and L1 evidence_spans grounding) always reference a stable text anchor
    rather than the mutable Record.abstract field.
    """
    text = record.abstract or ""
    content_hash = hashlib.sha256(text.encode()).hexdigest() if text else None

    snapshot = SourceSnapshot(
        record_id=record.id,
        abstract_text=text or None,
        content_hash=content_hash,
        source_name=source_name,
    )
    session.add(snapshot)
    await session.flush()
    return snapshot
