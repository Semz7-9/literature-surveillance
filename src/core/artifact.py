"""
SourceSnapshot creation and helper utilities.
"""

import hashlib
from sqlalchemy import select
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
    Capture record.abstract as an immutable SourceSnapshot, reusing an
    existing snapshot if the content is identical.

    Content-addressed on (record_id, source_name, raw_hash, normalizer_version):
    a daily re-fetch of an unchanged abstract must NOT create a new snapshot,
    or downstream AnalysisArtifact idempotency (keyed on snapshot.id) would
    force spurious LLM re-runs on content that hasn't actually changed.
    """
    raw = record.abstract
    analysis = normalize_abstract_text(raw)
    raw_hash = _hash(raw)
    normalizer_version = NORMALIZER_VERSION if analysis else None

    existing_stmt = select(SourceSnapshot).where(
        SourceSnapshot.record_id == record.id,
        SourceSnapshot.source_name == source_name,
        SourceSnapshot.raw_hash == raw_hash,
        SourceSnapshot.normalizer_version == normalizer_version,
    )
    existing = (await session.execute(existing_stmt)).scalars().first()
    if existing:
        return existing

    snapshot = SourceSnapshot(
        record_id=record.id,
        raw_abstract=raw,
        analysis_text=analysis,
        raw_hash=raw_hash,
        analysis_hash=_hash(analysis),
        normalizer_version=normalizer_version,
        source_name=source_name,
    )
    session.add(snapshot)
    await session.flush()
    return snapshot


def verify_snapshot_integrity(snapshot: SourceSnapshot) -> None:
    """
    Verify a SourceSnapshot's stored text still matches its recorded hashes.

    Checks BOTH raw_abstract/raw_hash and analysis_text/analysis_hash —
    checking only one side would miss tampering or inconsistent
    normalization on the other.

    Raises:
        ValueError: if either hash doesn't match, naming which one failed.
    """
    if (snapshot.raw_abstract is None) != (snapshot.raw_hash is None):
        raise ValueError(
            f"Snapshot {snapshot.id} raw_abstract and raw_hash must either both exist or both be null"
        )
    if (snapshot.analysis_text is None) != (snapshot.analysis_hash is None):
        raise ValueError(
            f"Snapshot {snapshot.id} analysis_text and analysis_hash must either both exist or both be null"
        )

    if snapshot.raw_hash:
        actual_raw_hash = _hash(snapshot.raw_abstract)
        if actual_raw_hash != snapshot.raw_hash:
            raise ValueError(
                f"Snapshot {snapshot.id} raw_abstract does not match its recorded "
                f"raw_hash — snapshot integrity check failed"
            )
    if snapshot.analysis_hash:
        actual_analysis_hash = _hash(snapshot.analysis_text)
        if actual_analysis_hash != snapshot.analysis_hash:
            raise ValueError(
                f"Snapshot {snapshot.id} analysis_text does not match its recorded "
                f"analysis_hash — snapshot integrity check failed"
            )
