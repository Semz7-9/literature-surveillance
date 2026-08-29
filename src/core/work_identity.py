"""Long-lived, evidence-backed Record → Work identity resolution."""

from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    IdentityEdge, IdentityEvidenceType, IdentityStatus, PendingIdentifierRelation,
    PendingRelationStatus, Record, Work, WorkIdentifier, WorkMergeAudit,
    WorkStatus, normalize_doi,
)

INTRA_WORK_RELATIONS = {"is-preprint-of", "has-preprint", "is-version-of", "has-version"}
IDENTIFIER_KEYS = {"pmid", "pmcid", "arxiv_id", "biorxiv_id"}


class IdentifierConflictError(ValueError):
    """An identifier is already registered to a different Work."""


class WorkIdentityResolver:
    """Propose, confirm, materialize, reconcile, and conservatively merge Works."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def resolve_or_create_work(self, record: Record, crossref_relations: dict | None = None) -> IdentityEdge:
        if crossref_relations:
            edge = await self._resolve_from_explicit_relation(record, crossref_relations)
            if edge:
                return edge
        if record.doi:
            work = await self._find_work_by_identifier("doi", record.doi)
            if work:
                return await self._create_identity_edge(record, work, IdentityEvidenceType.DOI_EXACT, 1.0, IdentityStatus.CONFIRMED, {"reason": "DOI already registered to this Work"})
        edge = await self._fuzzy_match_work(record)
        return edge if edge else await self._create_new_work(record)

    async def materialize_if_confirmed(self, record: Record, edge: IdentityEdge) -> bool:
        if edge.status != IdentityStatus.CONFIRMED.value:
            return False
        work = await self.session.get(Work, edge.target_work_id)
        if work is None:
            raise ValueError(f"Identity edge {edge.id} references a missing Work")
        await self.attach_record_to_work(record, work)
        return True

    async def attach_record_to_work(self, record: Record, work: Work) -> None:
        """The only confirmation materialization path; it is idempotent."""
        if work.status != WorkStatus.ACTIVE.value:
            raise ValueError(f"Cannot attach a Record to merged Work {work.work_id}")
        if record.work_id is not None and record.work_id != work.id:
            raise ValueError(f"Record {record.record_id} is already attached to Work {record.work_id}; use merge_work or reject its confirmed edge first")
        identifiers = self._record_identifiers(record)
        for identifier_type, identifier_value in identifiers:
            existing = await self._find_identifier(identifier_type, identifier_value)
            if existing and existing.work_id != work.id:
                raise IdentifierConflictError(f"{identifier_type}:{identifier_value} belongs to Work {existing.work_id}, not {work.id}")
        record.work_id = work.id
        await self.session.flush()
        for identifier_type, identifier_value in identifiers:
            if await self._find_identifier(identifier_type, identifier_value) is None:
                self.session.add(WorkIdentifier(work_id=work.id, identifier_type=identifier_type, identifier_value=identifier_value))
        await self.session.flush()
        for identifier_type, identifier_value in identifiers:
            await self.reconcile_pending_for_identifier(identifier_type, identifier_value, record)
        await self.recompute_work_projection(work)

    async def _resolve_from_explicit_relation(self, record: Record, relations: dict) -> Optional[IdentityEdge]:
        for relation_type in INTRA_WORK_RELATIONS:
            for related in relations.get(relation_type, []):
                related_doi = related.get("id")
                if not related_doi:
                    continue
                related_doi = normalize_doi(related_doi)
                work = await self._find_work_by_identifier("doi", related_doi)
                if work:
                    return await self._create_identity_edge(record, work, IdentityEvidenceType.EXPLICIT_CROSSREF_RELATION, 1.0, IdentityStatus.CONFIRMED, {"relation_type": relation_type, "related_doi": related_doi})
                await self._create_pending_relation(record, "doi", related_doi, relation_type)
        return None

    async def _create_pending_relation(self, record: Record, identifier_type: str, identifier_value: str, relation_type: str) -> PendingIdentifierRelation:
        stmt = select(PendingIdentifierRelation).where(PendingIdentifierRelation.source_record_id == record.id, PendingIdentifierRelation.target_identifier_type == identifier_type, PendingIdentifierRelation.target_identifier_value == identifier_value, PendingIdentifierRelation.relation_type == relation_type)
        existing = (await self.session.execute(stmt)).scalar_one_or_none()
        if existing:
            return existing
        relation = PendingIdentifierRelation(source_record_id=record.id, target_identifier_type=identifier_type, target_identifier_value=identifier_value, relation_type=relation_type, evidence_source="crossref")
        self.session.add(relation)
        await self.session.flush()
        return relation

    async def reconcile_pending_for_identifier(self, identifier_type: str, identifier_value: str, target_record: Record) -> None:
        stmt = select(PendingIdentifierRelation).where(PendingIdentifierRelation.target_identifier_type == identifier_type, PendingIdentifierRelation.target_identifier_value == identifier_value, PendingIdentifierRelation.status == PendingRelationStatus.PENDING.value)
        relations = (await self.session.execute(stmt)).scalars().all()
        for relation in relations:
            source = await self.session.get(Record, relation.source_record_id)
            if source is None:
                relation.status, relation.resolved_at = PendingRelationStatus.DISMISSED.value, datetime.utcnow()
            elif source.id == target_record.id:
                continue
            elif source.work_id and not target_record.work_id:
                await self.attach_record_to_work(target_record, await self.session.get(Work, source.work_id))
                relation.status, relation.resolved_at = PendingRelationStatus.RESOLVED.value, datetime.utcnow()
            elif target_record.work_id and not source.work_id:
                await self.attach_record_to_work(source, await self.session.get(Work, target_record.work_id))
                relation.status, relation.resolved_at = PendingRelationStatus.RESOLVED.value, datetime.utcnow()
            elif source.work_id and target_record.work_id:
                if source.work_id == target_record.work_id:
                    relation.status, relation.resolved_at = PendingRelationStatus.RESOLVED.value, datetime.utcnow()
                else:
                    await self._create_identity_edge(source, await self.session.get(Work, target_record.work_id), IdentityEvidenceType.EXPLICIT_CROSSREF_RELATION, 1.0, IdentityStatus.CANDIDATE, {"relation_type": relation.relation_type, "pending_relation_id": relation.id})
                    relation.status, relation.resolved_at = PendingRelationStatus.CONFLICT.value, datetime.utcnow()
        await self.session.flush()

    async def merge_work(self, keep: Work, merge: Work, *, reason: str, evidence: dict, confirmed: bool = False) -> Work:
        """Merge only after explicit high-confidence evidence or human confirmation."""
        if not confirmed:
            raise ValueError("merge_work requires explicit confirmation")
        if keep.id == merge.id:
            return keep
        if keep.status != WorkStatus.ACTIVE.value or merge.status != WorkStatus.ACTIVE.value:
            raise ValueError("Only active Works can be merged")
        records = (await self.session.execute(select(Record).where(Record.work_id == merge.id))).scalars().all()
        for record in records:
            record.work_id = keep.id
        identifiers = (await self.session.execute(select(WorkIdentifier).where(WorkIdentifier.work_id == merge.id))).scalars().all()
        for identifier in identifiers:
            existing = await self._find_identifier(identifier.identifier_type, identifier.identifier_value)
            if existing and existing.id != identifier.id and existing.work_id != keep.id:
                raise IdentifierConflictError("Merge would steal an identifier from an unrelated Work")
            if existing and existing.id != identifier.id:
                await self.session.delete(identifier)
            else:
                identifier.work_id = keep.id
        edges = (await self.session.execute(select(IdentityEdge).where(IdentityEdge.target_work_id == merge.id))).scalars().all()
        for edge in edges:
            edge.target_work_id = keep.id
        merge.status, merge.merged_into_work_id = WorkStatus.MERGED.value, keep.id
        self.session.add(WorkMergeAudit(merged_from_work_id=merge.id, merged_into_work_id=keep.id, reason=reason, evidence=evidence))
        await self.session.flush()
        await self.recompute_work_projection(keep)
        return keep

    async def recompute_work_projection(self, work: Work) -> None:
        records = (await self.session.execute(select(Record).where(Record.work_id == work.id))).scalars().all()
        if not records:
            work.preferred_record_id = work.first_public_record_id = None
            work.canonical_doi = None
            return
        def date_key(record: Record):
            return (record.publication_date is not None, record.publication_date or datetime.min, record.id)
        def version_rank(record: Record) -> int:
            kind = str(record.extra_metadata.get("record_version", "")).upper()
            if kind in {"VOR", "VERSION_OF_RECORD", "PUBLISHED"}:
                return 3
            if kind in {"AAM", "ACCEPTED_MANUSCRIPT"}:
                return 2
            if record.publication_status == "PREPRINT":
                return 1
            return 3 if record.journal else 1
        preferred = max(records, key=lambda record: (version_rank(record), *date_key(record)))
        dated = [record for record in records if record.publication_date is not None]
        first_public = min(dated, key=lambda record: (record.publication_date, record.id)) if dated else min(records, key=lambda record: record.id)
        work.preferred_record_id, work.first_public_record_id = preferred.id, first_public.id
        work.title, work.canonical_doi = preferred.title, preferred.doi
        await self.session.flush()

    async def _find_work_by_identifier(self, identifier_type: str, identifier_value: str) -> Optional[Work]:
        identifier = await self._find_identifier(identifier_type, identifier_value)
        return await self.session.get(Work, identifier.work_id) if identifier else None

    async def _find_identifier(self, identifier_type: str, identifier_value: str) -> Optional[WorkIdentifier]:
        stmt = select(WorkIdentifier).where(WorkIdentifier.identifier_type == identifier_type, WorkIdentifier.identifier_value == identifier_value)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    def _record_identifiers(self, record: Record) -> list[tuple[str, str]]:
        identifiers = [("doi", normalize_doi(record.doi))] if record.doi else []
        identifiers.extend((key, str(value).strip()) for key, value in record.extra_metadata.items() if key in IDENTIFIER_KEYS and value)
        return identifiers

    async def _fuzzy_match_work(self, record: Record) -> Optional[IdentityEdge]:
        first_author = record.authors[0].get("name") if record.authors else None
        if not first_author:
            return None
        works = (await self.session.execute(select(Work).where(Work.title == record.title))).scalars().all()
        for work in works:
            work_record = (await self.session.execute(select(Record).where(Record.work_id == work.id).limit(1))).scalar_one_or_none()
            if work_record and work_record.authors and work_record.authors[0].get("name") == first_author:
                return await self._create_identity_edge(record, work, IdentityEvidenceType.EXACT_TITLE_FIRST_AUTHOR, 0.7, IdentityStatus.CANDIDATE, {"title_match": "exact", "first_author": first_author})
        return None

    async def _create_new_work(self, record: Record) -> IdentityEdge:
        work = Work(work_id=f"W{datetime.utcnow().timestamp()}", title=record.title, canonical_doi=None)
        self.session.add(work)
        await self.session.flush()
        return await self._create_identity_edge(record, work, IdentityEvidenceType.NEW_WORK, 1.0, IdentityStatus.CONFIRMED, {"reason": "no matching Work found, created new one"})

    async def _create_identity_edge(self, record: Record, work: Work, evidence_type: IdentityEvidenceType, confidence: float, status: IdentityStatus, evidence_detail: dict) -> IdentityEdge:
        edge = IdentityEdge(source_record_id=record.id, target_work_id=work.id, evidence_type=evidence_type.value, confidence=confidence, evidence_detail=evidence_detail, status=status.value)
        self.session.add(edge)
        await self.session.flush()
        return edge

    async def get_candidate_edges(self) -> list[IdentityEdge]:
        result = await self.session.execute(select(IdentityEdge).where(IdentityEdge.status.in_([IdentityStatus.CANDIDATE.value, IdentityStatus.PROVISIONAL.value])))
        return list(result.scalars().all())

    async def confirm_edge(self, edge_id: int) -> None:
        edge = await self.session.get(IdentityEdge, edge_id)
        if edge is None:
            raise ValueError(f"Identity edge {edge_id} does not exist")
        existing = (await self.session.execute(select(IdentityEdge).where(IdentityEdge.source_record_id == edge.source_record_id, IdentityEdge.status == IdentityStatus.CONFIRMED.value, IdentityEdge.id != edge.id))).scalar_one_or_none()
        if existing:
            raise ValueError(f"Record {edge.source_record_id} already has a CONFIRMED edge {existing.id}")
        record = await self.session.get(Record, edge.source_record_id)
        work = await self.session.get(Work, edge.target_work_id)
        if work is None:
            raise ValueError(f"Identity edge {edge.id} references a missing Work")
        await self.attach_record_to_work(record, work)
        edge.status, edge.updated_at = IdentityStatus.CONFIRMED.value, datetime.utcnow()
        await self.session.flush()

    async def reject_edge(self, edge_id: int) -> None:
        edge = await self.session.get(IdentityEdge, edge_id)
        if edge is None:
            raise ValueError(f"Identity edge {edge_id} does not exist")
        edge.status, edge.updated_at = IdentityStatus.REJECTED.value, datetime.utcnow()
        record = await self.session.get(Record, edge.source_record_id)
        if record.work_id == edge.target_work_id:
            record.work_id = None
            work = await self.session.get(Work, edge.target_work_id)
            await self.session.flush()
            await self.recompute_work_projection(work)
