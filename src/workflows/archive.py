"""Topic Archive v0.1: scope → lexicon → search → canonical Works."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.models import (
    ArchiveRevision, ArchiveScope, ArchiveWork, ConceptSet, ConceptTerm,
    DiscoveryEvent, MonitorSubscription, SearchStrategy, Source, TopicArchive,
)
from .monitor import run_monitor_subscription


def split_lines(value: str) -> list[str]:
    return list(dict.fromkeys(
        part.strip() for line in value.splitlines() for part in line.split(",") if part.strip()
    ))


async def record_archive_revision(
    session: AsyncSession,
    archive: TopicArchive,
    change_type: str,
    summary: str,
    snapshot: dict | None = None,
) -> ArchiveRevision:
    archive.revision += 1
    revision = ArchiveRevision(
        archive_id=archive.id, version=archive.revision,
        change_type=change_type, summary=summary, snapshot=snapshot or {},
    )
    session.add(revision)
    await session.flush()
    return revision


def _pubmed_group(terms: list[str]) -> str:
    clean = [term.replace('"', "").strip() for term in terms if term.strip()]
    return "(" + " OR ".join(f'"{term}"[Title/Abstract]' for term in clean) + ")"


async def generate_search_strategy(
    session: AsyncSession,
    archive: TopicArchive,
) -> SearchStrategy:
    concept_sets = (await session.execute(select(ConceptSet).where(
        ConceptSet.archive_id == archive.id
    ).order_by(ConceptSet.id))).scalars().all()
    groups: list[tuple[ConceptSet, list[str]]] = []
    excluded_terms: list[str] = []
    for concept_set in concept_sets:
        terms = (await session.execute(select(ConceptTerm.term).where(
            ConceptTerm.concept_set_id == concept_set.id,
            ConceptTerm.status == "include",
        ).order_by(ConceptTerm.id))).scalars().all()
        if terms:
            groups.append((concept_set, list(terms)))
        excluded_terms.extend((await session.execute(select(ConceptTerm.term).where(
            ConceptTerm.concept_set_id == concept_set.id,
            ConceptTerm.status == "exclude",
        ))).scalars().all())
    if not groups:
        raise ValueError("至少需要一个包含有效词的 Concept Set")
    scope = (await session.execute(select(ArchiveScope).where(
        ArchiveScope.archive_id == archive.id
    ).order_by(ArchiveScope.version.desc()).limit(1))).scalar_one_or_none()
    exclusions = list(dict.fromkeys([
        *(scope.exclusions if scope else []), *excluded_terms,
    ]))
    exclusion = _pubmed_group(exclusions) if exclusions else None
    anchor_set, anchor_terms = groups[0]
    anchor = _pubmed_group(anchor_terms)
    queries = [{
        "label": "Q1", "query": f"{anchor} NOT {exclusion}" if exclusion else anchor,
        "reason": f"基线召回：Concept Set「{anchor_set.name}」",
    }]
    for index, (concept_set, terms) in enumerate(groups[1:], 2):
        query = f"{anchor} AND {_pubmed_group(terms)}"
        if exclusion:
            query += f" NOT {exclusion}"
        queries.append({
            "label": f"Q{index}", "query": query,
            "reason": f"交叉验证：{anchor_set.name} AND {concept_set.name}",
        })
    latest_version = (await session.execute(select(func.max(SearchStrategy.version)).where(
        SearchStrategy.archive_id == archive.id
    ))).scalar_one() or 0
    strategy = SearchStrategy(
        archive_id=archive.id, version=latest_version + 1,
        provider="pubmed", queries=queries,
    )
    session.add(strategy)
    await session.flush()
    await record_archive_revision(
        session, archive, "SEARCH_STRATEGY",
        f"生成 Search Strategy v{strategy.version}（{len(queries)} 个可审计 Query）",
        {"strategy_id": strategy.id, "queries": queries},
    )
    return strategy


async def execute_search_strategy(
    session: AsyncSession,
    archive: TopicArchive,
    strategy: SearchStrategy,
    pubmed_factory,
    *,
    max_results_per_query: int = 50,
) -> dict[str, int]:
    if strategy.executed_at is not None:
        raise ValueError("该 Search Strategy 已执行；请生成新版本后再次检索")
    source = (await session.execute(select(Source).where(
        Source.name == "pubmed"
    ))).scalar_one_or_none()
    if source is None:
        source = Source(name="pubmed", source_type="api", config={})
        session.add(source)
        await session.flush()
    before = (await session.execute(select(func.count(ArchiveWork.id)).where(
        ArchiveWork.archive_id == archive.id
    ))).scalar_one()
    discovered = 0
    for query in strategy.queries:
        subscription = MonitorSubscription(
            name=f"archive-{archive.id}-s{strategy.version}-{query['label']}",
            subscription_type="archive_search", source_id=source.id, enabled=False,
            config={
                "query": query["query"], "feed_mode": "created",
                "lookback_days": 36500, "cursor_overlap_days": 0,
                "page_size": max_results_per_query,
                "max_items_per_run": max_results_per_query,
                "max_pages_per_run": 1,
                "archive_id": archive.id, "strategy_id": strategy.id,
            },
        )
        session.add(subscription)
        await session.flush()
        adapter = pubmed_factory()
        try:
            result = await run_monitor_subscription(session, subscription, adapter, None)
        finally:
            await adapter.close()
        if result.error:
            raise RuntimeError(f"{query['label']} PubMed 检索失败：{result.error}")
        discovered += result.discovered
        events = (await session.execute(select(DiscoveryEvent).where(
            DiscoveryEvent.subscription_id == subscription.id,
            DiscoveryEvent.work_id.is_not(None),
        ))).scalars().all()
        for event in events:
            membership = (await session.execute(select(ArchiveWork).where(
                ArchiveWork.archive_id == archive.id,
                ArchiveWork.work_id == event.work_id,
            ))).scalar_one_or_none()
            if membership is None:
                membership = ArchiveWork(
                    archive_id=archive.id, work_id=event.work_id,
                    strategy_id=strategy.id, matched_queries=[query["label"]],
                )
                session.add(membership)
            elif query["label"] not in membership.matched_queries:
                membership.matched_queries = [*membership.matched_queries, query["label"]]
    strategy.executed_at = datetime.utcnow()
    await session.flush()
    after = (await session.execute(select(func.count(ArchiveWork.id)).where(
        ArchiveWork.archive_id == archive.id
    ))).scalar_one()
    added = after - before
    await record_archive_revision(
        session, archive, "SEARCH_EXECUTION",
        f"执行 Search Strategy v{strategy.version}：发现 {discovered}，归档新增 {added}",
        {"strategy_id": strategy.id, "discovered": discovered, "archive_works_added": added},
    )
    return {"discovered": discovered, "added": added, "total": after}
