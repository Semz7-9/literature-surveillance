"""Batch 1.2 regression tests: delayed relations, attachment, and merge provenance."""

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from src.core.database import Database
from src.core.models import (
    IdentityEdge, IdentityStatus, PendingIdentifierRelation, PendingRelationStatus,
    Record, Work, WorkIdentifier, WorkMergeAudit, WorkStatus,
)
from src.core.work_identity import IdentifierConflictError, WorkIdentityResolver


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "identity_closure.db")
    await database.init_db()
    yield database
    await database.close()


def record(doi: str, title: str, *, preprint: bool = False, extra: dict | None = None) -> Record:
    return Record(
        record_id=f"R_{doi.replace('/', '_')}", title=title, authors=[{"name": "Author A"}],
        doi=doi, abstract="Abstract.", evidence_level="E1",
        publication_status="PREPRINT" if preprint else "ACTIVE",
        publication_date=datetime(2023 if preprint else 2024, 1, 1),
        journal=None if preprint else "Journal", extra_metadata=extra or {},
    )


async def resolve_and_attach(resolver: WorkIdentityResolver, record: Record, relations: dict | None = None):
    edge = await resolver.resolve_or_create_work(record, relations)
    await resolver.materialize_if_confirmed(record, edge)
    return edge


async def test_preprint_then_vor_closes_pending_relation_and_projects_work(db: Database):
    async with db.get_session() as session:
        resolver = WorkIdentityResolver(session)
        preprint = record("10.1/p", "Research", preprint=True)
        session.add(preprint)
        await session.flush()
        await resolve_and_attach(resolver, preprint, {"is-preprint-of": [{"id": "10.1/v"}]})
        pending = (await session.execute(select(PendingIdentifierRelation))).scalar_one()
        assert pending.status == PendingRelationStatus.PENDING.value

        vor = record("10.1/v", "Research")
        session.add(vor)
        await session.flush()
        # The arriving VoR has no reciprocal relation.  It must still resolve
        # from P's persisted one-way pending relation.
        await resolve_and_attach(resolver, vor)

        work = await session.get(Work, preprint.work_id)
        assert vor.work_id == work.id
        assert pending.status == PendingRelationStatus.RESOLVED.value
        identifiers = (await session.execute(select(WorkIdentifier).where(WorkIdentifier.work_id == work.id))).scalars().all()
        assert {(item.identifier_type, item.identifier_value) for item in identifiers} == {("doi", "10.1/p"), ("doi", "10.1/v")}
        assert work.preferred_record_id == vor.id
        assert work.first_public_record_id == preprint.id
        assert work.canonical_doi == "10.1/v"


async def test_identifier_backfill_is_idempotent_and_conflicts_do_not_reassign(db: Database):
    async with db.get_session() as session:
        resolver = WorkIdentityResolver(session)
        first = record("10.1/a", "A", extra={"pmid": "42", "arxiv_id": "x.1"})
        session.add(first)
        await session.flush()
        await resolve_and_attach(resolver, first)
        work = await session.get(Work, first.work_id)
        await resolver.attach_record_to_work(first, work)
        identifiers = (await session.execute(select(WorkIdentifier).where(WorkIdentifier.work_id == work.id))).scalars().all()
        assert len(identifiers) == 3

        second = record("10.1/b", "B", extra={"pmid": "42"})
        session.add(second)
        await session.flush()
        other = Work(work_id="other", title="Other")
        session.add(other)
        await session.flush()
        with pytest.raises(IdentifierConflictError):
            await resolver.attach_record_to_work(second, other)
        assert second.work_id is None


async def test_merge_preserves_audit_and_tombstones_old_work(db: Database):
    async with db.get_session() as session:
        resolver = WorkIdentityResolver(session)
        preprint = record("10.1/p2", "Research", preprint=True)
        vor = record("10.1/v2", "Research Version")
        session.add_all([preprint, vor])
        await session.flush()
        await resolve_and_attach(resolver, preprint)
        await resolve_and_attach(resolver, vor)
        work_keep = await session.get(Work, preprint.work_id)
        work_merge = await session.get(Work, vor.work_id)
        pending = await resolver._create_pending_relation(preprint, "doi", "10.1/v2", "is-preprint-of")
        await resolver.reconcile_pending_for_identifier("doi", "10.1/v2", vor)
        assert pending.status == PendingRelationStatus.CONFLICT.value
        candidate = (await session.execute(select(IdentityEdge).where(IdentityEdge.source_record_id == preprint.id, IdentityEdge.status == IdentityStatus.CANDIDATE.value))).scalar_one()
        assert candidate.target_work_id == work_merge.id

        await resolver.merge_work(work_keep, work_merge, reason="explicit preprint relation", evidence={"pending_relation_id": pending.id}, confirmed=True)
        await session.refresh(vor)
        await session.refresh(work_merge)
        assert vor.work_id == work_keep.id
        assert work_merge.status == WorkStatus.MERGED.value
        assert work_merge.merged_into_work_id == work_keep.id
        audit = (await session.execute(select(WorkMergeAudit))).scalar_one()
        assert audit.merged_from_work_id == work_merge.id
        assert pending.status == PendingRelationStatus.RESOLVED.value
        await session.refresh(candidate)
        assert candidate.status == IdentityStatus.SUPERSEDED.value
        assert work_merge.preferred_record_id is None
        assert work_merge.first_public_record_id is None
        assert work_merge.canonical_doi is None
