"""Deterministic subscription → discovery → existing processing pipeline."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters.crossref import CrossrefAdapter, parse_crossref_metadata
from ..core.artifact import create_source_snapshot
from ..core.ingestion import get_or_create_record
from ..core.models import (
    DiscoveryEvent, EvidenceLevel, MonitorSubscription, Record, Source,
    SourceCursor, SourceHealth, normalize_doi,
)
from ..core.work_identity import WorkIdentityResolver
from ..llm.client import LLMClient
from .l1_generator import run_l1


@dataclass
class MonitorRunResult:
    discovered: int = 0
    created: int = 0
    duplicate: int = 0
    l1_generated: int = 0
    failed: int = 0
    error: str | None = None


async def run_monitor_subscription(
    session: AsyncSession,
    subscription: MonitorSubscription,
    adapter: CrossrefAdapter,
    llm_client: LLMClient | None = None,
    *,
    now: datetime | None = None,
) -> MonitorRunResult:
    """Fetch one incremental page and feed it through the shared core."""
    result = MonitorRunResult()
    now = now or datetime.utcnow()
    source = await session.get(Source, subscription.source_id)
    if source is None:
        raise ValueError(f"Subscription {subscription.id} references a missing Source")
    cursor = (await session.execute(select(SourceCursor).where(
        SourceCursor.subscription_id == subscription.id
    ))).scalar_one_or_none()
    if cursor is None:
        cursor = SourceCursor(subscription_id=subscription.id)
        session.add(cursor)
        await session.flush()
    health = (await session.execute(select(SourceHealth).where(
        SourceHealth.source_id == source.id
    ))).scalar_one_or_none()
    if health is None:
        health = SourceHealth(source_id=source.id)
        session.add(health)
        await session.flush()

    lookback_days = int(subscription.config.get("lookback_days", 7))
    page_size = int(subscription.config.get("max_results", 100))
    # An unfinished Crossref cursor must keep its original query window.
    # Otherwise a backlog larger than one page would repeatedly return page 1.
    active_window = cursor.state.get("active_window") if cursor.cursor_value else None
    if active_window:
        start = datetime.fromisoformat(active_window["from"])
        until = datetime.fromisoformat(active_window["until"])
        adapter_cursor = cursor.cursor_value
    else:
        # Recheck one overlapping day so late Crossref updates are not lost;
        # DiscoveryEvent uniqueness makes the overlap idempotent.
        start = (cursor.last_success_at - timedelta(days=1)) if cursor.last_success_at else now - timedelta(days=lookback_days)
        until = now
        adapter_cursor = "*"
    started = datetime.utcnow()
    try:
        items, next_cursor = await adapter.discover_works(
            from_date=start.date().isoformat(),
            until_date=until.date().isoformat(),
            issn=subscription.config.get("issn"),
            query=subscription.config.get("query"),
            cursor=adapter_cursor,
            rows=page_size,
        )
    except Exception as exc:
        health.last_failure = datetime.utcnow()
        health.consecutive_failures += 1
        health.status = "down" if health.consecutive_failures >= 3 else "degraded"
        health.last_error = str(exc)
        cursor.last_checked_at = now
        result.error = str(exc)
        await session.flush()
        return result

    health.last_success = datetime.utcnow()
    health.consecutive_failures = 0
    health.status = "healthy"
    health.last_error = None
    health.latency_ms = (datetime.utcnow() - started).total_seconds() * 1000
    resolver = WorkIdentityResolver(session)

    for raw_work in items:
        raw_doi = raw_work.get("DOI")
        if not raw_doi:
            result.failed += 1
            continue
        doi = normalize_doi(raw_doi)
        existing_event = (await session.execute(select(DiscoveryEvent).where(
            DiscoveryEvent.subscription_id == subscription.id,
            DiscoveryEvent.external_identifier == doi,
        ))).scalar_one_or_none()
        if existing_event:
            result.duplicate += 1
            continue
        event = DiscoveryEvent(
            source_id=source.id,
            subscription_id=subscription.id,
            external_identifier=doi,
            source_url=f"https://doi.org/{doi}",
            raw_metadata=raw_work,
            status="DISCOVERED",
        )
        session.add(event)
        await session.flush()
        result.discovered += 1
        try:
            metadata = parse_crossref_metadata(raw_work)

            def record_factory(normalized_doi: str) -> Record:
                return Record(
                    record_id=f"R_{normalized_doi.replace('/', '_')}", work_id=None,
                    title=metadata["title"], authors=metadata["authors"],
                    journal=metadata["journal"], publication_date=metadata["publication_date"],
                    publication_date_precision=metadata["publication_date_precision"],
                    raw_date_parts=metadata["raw_date_parts"], doi=normalized_doi,
                    abstract=metadata["abstract"],
                    evidence_level=EvidenceLevel.E1.value if metadata["abstract"] else EvidenceLevel.E0.value,
                    publication_status=metadata["publication_status"],
                    extra_metadata=metadata["other_ids"],
                )

            record, created = await get_or_create_record(session, doi, record_factory)
            if created:
                snapshot = await create_source_snapshot(session, record, source.name)
                edge = await resolver.resolve_or_create_work(record, raw_work.get("relation", {}))
                await resolver.materialize_if_confirmed(record, edge)
                result.created += 1
            else:
                snapshot = await create_source_snapshot(session, record, source.name)
                result.duplicate += 1
            event.work_id = record.work_id
            event.status = "INGESTED" if record.work_id else "IDENTITY_REVIEW"
            if llm_client and snapshot.analysis_text and record.work_id:
                await run_l1(session, record, snapshot, llm_client)
                event.status = "L1_READY"
                result.l1_generated += 1
        except Exception as exc:
            event.status = "FAILED"
            event.raw_metadata = {**raw_work, "processing_error": str(exc)}
            result.failed += 1

    cursor.last_checked_at = now
    has_more = len(items) >= page_size and bool(next_cursor)
    cursor.cursor_value = next_cursor if has_more else None
    if not has_more:
        cursor.last_success_at = now
    cursor.last_seen_identifier = next(
        (normalize_doi(item["DOI"]) for item in reversed(items) if item.get("DOI")), None
    )
    cursor.state = {
        "from_date": start.date().isoformat(), "until_date": until.date().isoformat(),
        "active_window": {
            "from": start.isoformat(), "until": until.isoformat()
        } if has_more else None,
        "has_more": has_more,
        "last_result": result.__dict__,
    }
    await session.flush()
    return result
