"""Batch B1: resolve planning decisions, retrieve two sources, and materialize a corpus."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters.openalex import openalex_external_id, parse_openalex_metadata
from ..adapters.pubmed import parse_pubmed_metadata
from ..core.artifact import create_source_snapshot
from ..core.ingestion import get_or_create_record
from ..core.models import (
    AIProposal,
    ArchiveBuildRun,
    ArchiveSearchPlan,
    ArchiveWork,
    EffectiveSearchPlan,
    EvidenceLevel,
    HumanDecision,
    Record,
    RetrievalHit,
    RetrievalRun,
    TopicArchive,
    Work,
    WorkIdentifier,
)
from ..core.work_identity import WorkIdentityResolver
from .archive import record_archive_revision
from .archive_builder import get_build_step
from .query_compiler import compile_queries


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value and value.strip()))


async def materialize_effective_search_plan(
    session: AsyncSession,
    archive: TopicArchive,
) -> EffectiveSearchPlan:
    base = (
        await session.execute(
            select(ArchiveSearchPlan)
            .where(ArchiveSearchPlan.archive_id == archive.id)
            .order_by(ArchiveSearchPlan.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if base is None:
        raise ValueError("Archive has no SearchPlan")

    decisions = list(
        (
            await session.execute(
                select(HumanDecision)
                .where(HumanDecision.archive_id == archive.id)
                .order_by(HumanDecision.created_at, HumanDecision.id)
            )
        )
        .scalars()
        .all()
    )
    concepts = [dict(item) for item in base.concepts]
    hard_exclusions = list(base.hard_exclusions)
    background_terms: list[str] = []
    included_terms: list[str] = []
    applied_decisions = []
    for decision in decisions:
        proposal = await session.get(AIProposal, decision.proposal_id)
        terms = _dedupe((proposal.payload if proposal else {}).get("terms", []))
        applied_decisions.append(
            {
                "human_decision_id": decision.id,
                "proposal_id": decision.proposal_id,
                "decision": decision.decision,
                "terms": terms,
                "rationale": decision.rationale,
            }
        )
        if decision.decision == "INCLUDE":
            included_terms.extend(terms)
        elif decision.decision == "EXCLUDE":
            hard_exclusions.extend(terms)
        elif decision.decision == "BACKGROUND_ONLY":
            background_terms.extend(terms)
    if included_terms:
        concepts.append(
            {
                "label": "Operator included",
                "terms": _dedupe(included_terms),
                "purpose": "Terms explicitly included by HumanDecision",
            }
        )

    version = (
        await session.execute(
            select(func.max(EffectiveSearchPlan.version)).where(
                EffectiveSearchPlan.archive_id == archive.id
            )
        )
    ).scalar_one() or 0
    plan = EffectiveSearchPlan(
        archive_id=archive.id,
        base_plan_id=base.id,
        version=version + 1,
        concepts=concepts,
        historical_vocabulary=base.historical_vocabulary,
        hard_exclusions=_dedupe(hard_exclusions),
        soft_exclusions=base.soft_exclusions,
        background_terms=_dedupe(background_terms),
        source_targets=[
            source for source in base.source_targets if source in {"pubmed", "openalex"}
        ]
        or ["pubmed", "openalex"],
        applied_decisions=applied_decisions,
    )
    session.add(plan)
    await session.flush()
    plan.compiled_queries = compile_queries(plan)
    await session.flush()
    return plan


def _excluded(raw: dict, metadata: dict, exclusions: list[str]) -> bool:
    haystack = f"{metadata['title']} {metadata.get('abstract') or ''}".lower()
    return any(term.lower() in haystack for term in exclusions if term.strip())


async def _ingest_hit(
    session: AsyncSession,
    hit: RetrievalHit,
    raw: dict,
) -> int | None:
    source = hit.source
    metadata = parse_pubmed_metadata(raw) if source == "pubmed" else parse_openalex_metadata(raw)
    if not metadata["title"]:
        return None
    external_id = hit.external_id

    def record_factory(normalized_doi: str) -> Record:
        return Record(
            record_id=(
                f"R_{normalized_doi.replace('/', '_')}"
                if normalized_doi
                else f"R_{source}_{external_id}"
            ),
            work_id=None,
            title=metadata["title"],
            authors=metadata["authors"],
            journal=metadata["journal"],
            publication_date=metadata["publication_date"],
            publication_date_precision=metadata["publication_date_precision"],
            raw_date_parts=metadata["raw_date_parts"],
            doi=normalized_doi or None,
            abstract=metadata["abstract"],
            evidence_level=(
                EvidenceLevel.E1.value if metadata["abstract"] else EvidenceLevel.E0.value
            ),
            publication_status=metadata["publication_status"],
            extra_metadata=metadata["other_ids"],
        )

    if metadata["doi"]:
        record, created = await get_or_create_record(session, metadata["doi"], record_factory)
    else:
        record_id = f"R_{source}_{external_id}"
        record = (
            await session.execute(select(Record).where(Record.record_id == record_id))
        ).scalar_one_or_none()
        created = record is None
        if record is None:
            record = record_factory("")
            session.add(record)
            await session.flush()

    if not created:
        if not record.abstract and metadata["abstract"]:
            record.abstract = metadata["abstract"]
            record.evidence_level = EvidenceLevel.E1.value
        if not record.journal and metadata["journal"]:
            record.journal = metadata["journal"]
        record.extra_metadata = {
            **(record.extra_metadata or {}),
            **metadata["other_ids"],
        }
    await session.flush()
    await create_source_snapshot(session, record, source)
    resolver = WorkIdentityResolver(session)
    if record.work_id is None:
        edge = await resolver.resolve_or_create_work(record, raw.get("relation", {}))
        await resolver.materialize_if_confirmed(record, edge)
    else:
        work = await session.get(Work, record.work_id)
        if work:
            await resolver.attach_record_to_work(record, work)
    hit.work_id = record.work_id
    return record.work_id


async def _store_and_ingest(
    session: AsyncSession,
    run: RetrievalRun,
    *,
    source: str,
    query_id: str,
    raw_items: list[dict],
    hard_exclusions: list[str],
    memberships: dict[int, ArchiveWork],
) -> set[int]:
    work_ids: set[int] = set()
    for rank, raw in enumerate(raw_items, 1):
        external_id = (
            str(raw.get("PMID", "")).strip() if source == "pubmed" else openalex_external_id(raw)
        )
        if not external_id:
            continue
        hit = RetrievalHit(
            retrieval_run_id=run.id,
            archive_id=run.archive_id,
            source=source,
            external_id=external_id,
            query_id=query_id,
            rank=rank,
            raw_metadata=raw,
        )
        session.add(hit)
        await session.flush()
        metadata = (
            parse_pubmed_metadata(raw) if source == "pubmed" else parse_openalex_metadata(raw)
        )
        if _excluded(raw, metadata, hard_exclusions):
            hit.raw_metadata = {**raw, "_archive_filter": "HARD_EXCLUDED"}
            continue
        work_id = await _ingest_hit(session, hit, raw)
        if work_id is None:
            continue
        work_ids.add(work_id)
        membership = memberships.get(work_id)
        match = f"{source}:{query_id}"
        if membership is None:
            membership = ArchiveWork(
                archive_id=run.archive_id,
                work_id=work_id,
                matched_queries=[match],
            )
            session.add(membership)
            memberships[work_id] = membership
        elif match not in membership.matched_queries:
            membership.matched_queries = [*membership.matched_queries, match]
    return work_ids


async def _landmark_metrics(
    session: AsyncSession,
    archive_id: int,
    known_landmarks: list[dict],
) -> tuple[list[str], float | None]:
    if not known_landmarks:
        return [], None
    work_ids = set(
        (
            await session.execute(
                select(ArchiveWork.work_id).where(ArchiveWork.archive_id == archive_id)
            )
        )
        .scalars()
        .all()
    )
    identifiers = set()
    if work_ids:
        identifiers = {
            (kind, value.lower())
            for kind, value in (
                await session.execute(
                    select(
                        WorkIdentifier.identifier_type,
                        WorkIdentifier.identifier_value,
                    ).where(WorkIdentifier.work_id.in_(work_ids))
                )
            ).all()
        }
    found = [
        item["id"]
        for item in known_landmarks
        if (item.get("identifier_type", "doi"), item["id"].lower()) in identifiers
    ]
    return found, len(found) / len(known_landmarks)


async def run_archive_discovery(
    session: AsyncSession,
    archive: TopicArchive,
    build_run: ArchiveBuildRun,
    pubmed_factory,
    openalex_factory,
    *,
    max_results_per_query: int = 50,
    known_landmarks: list[dict] | None = None,
) -> RetrievalRun:
    """Execute one bounded two-source retrieval while retaining partial provider results."""
    plan = await materialize_effective_search_plan(session, archive)
    run = RetrievalRun(archive_id=archive.id, effective_plan_id=plan.id)
    session.add(run)
    await session.flush()
    memberships = {
        item.work_id: item
        for item in (
            (await session.execute(select(ArchiveWork).where(ArchiveWork.archive_id == archive.id)))
            .scalars()
            .all()
        )
    }
    source_metrics: dict[str, dict] = {}
    query_coverage: dict[str, dict[str, int]] = {}
    errors: list[str] = []

    for source, factory in (("pubmed", pubmed_factory), ("openalex", openalex_factory)):
        if source not in plan.compiled_queries:
            continue
        adapter = factory()
        source_work_ids: set[int] = set()
        raw_count = 0
        query_coverage[source] = {}
        source_errors = []
        try:
            for query in plan.compiled_queries[source]:
                try:
                    if source == "pubmed":
                        items, _ = await adapter.discover_works(
                            from_date="1900-01-01",
                            until_date=datetime.utcnow().date().isoformat(),
                            query=query["query"],
                            cursor="*",
                            rows=max_results_per_query,
                            sort="relevance",
                        )
                    else:
                        items, _ = await adapter.search_works(
                            query=query["query"],
                            cursor="*",
                            rows=max_results_per_query,
                        )
                    raw_count += len(items)
                    query_coverage[source][query["query_id"]] = len(items)
                    source_work_ids.update(
                        await _store_and_ingest(
                            session,
                            run,
                            source=source,
                            query_id=query["query_id"],
                            raw_items=items,
                            hard_exclusions=plan.hard_exclusions,
                            memberships=memberships,
                        )
                    )
                except Exception as exc:
                    message = f"{query['query_id']}: {exc}"
                    source_errors.append(message)
                    errors.append(f"{source} {message}")
        finally:
            await adapter.close()
        source_metrics[source] = {
            "retrieved_hits": raw_count,
            "unique_works": len(source_work_ids),
            "errors": source_errors,
        }

    await session.flush()
    total = (
        await session.execute(
            select(func.count(ArchiveWork.id)).where(ArchiveWork.archive_id == archive.id)
        )
    ).scalar_one()
    found, recall = await _landmark_metrics(session, archive.id, known_landmarks or [])
    run.source_metrics = source_metrics
    run.query_coverage = query_coverage
    run.unique_work_count = total
    run.landmark_found = found
    run.landmark_total = len(known_landmarks or [])
    run.landmark_recall = recall
    run.error = "\n".join(errors) or None
    run.status = "COMPLETED" if not errors else "COMPLETED_WITH_ERRORS" if total else "FAILED"
    run.finished_at = datetime.utcnow()

    search_step = await get_build_step(session, build_run, "SEARCHING")
    if total:
        search_step.status = "COMPLETED"
        search_step.finished_at = datetime.utcnow()
        search_step.output_artifact = {
            "retrieval_run_id": run.id,
            "effective_plan_id": plan.id,
            "unique_work_count": total,
        }
        build_run.status = "PAUSED"
        build_run.state = "DISCOVERY_COMPLETE"
    else:
        search_step.status = "FAILED"
        search_step.error = run.error or "No canonical Works were retrieved"
        build_run.status = "FAILED"
        build_run.error = search_step.error
    await record_archive_revision(
        session,
        archive,
        "ARCHIVE_DISCOVERY",
        f"双源检索完成：Archive Corpus 共 {total} 个唯一 Work",
        {
            "retrieval_run_id": run.id,
            "effective_plan_id": plan.id,
            "source_metrics": source_metrics,
            "unique_work_count": total,
            "landmark_recall": recall,
        },
    )
    return run
