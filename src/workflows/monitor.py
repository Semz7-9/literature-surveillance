"""Correct, resumable subscription → observation → processing workflow."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters.crossref import CrossrefAdapter, parse_crossref_metadata
from ..adapters.pubmed import PubMedAdapter, parse_pubmed_metadata
from ..core.artifact import create_source_snapshot
from ..core.ingestion import get_or_create_record
from ..core.models import (
    DiscoveryEvent, EvidenceLevel, MonitorRun, MonitorSubscription, Record,
    Source, SourceCursor, SourceHealth, normalize_doi,
)
from ..core.work_identity import WorkIdentityResolver
from ..llm.client import LLMClient
from .l1_generator import run_l1_with_status


@dataclass
class MonitorRunResult:
    discovered: int = 0
    created: int = 0
    updated: int = 0
    duplicate: int = 0
    l1_generated: int = 0
    failed: int = 0
    processed: int = 0
    pages: int = 0
    has_more: bool = False
    skipped: bool = False
    error: str | None = None


def metadata_hash(raw_metadata: dict) -> str:
    """Hash semantically useful metadata without Crossref indexing noise."""
    stable = {
        key: value for key, value in raw_metadata.items()
        if key not in {"indexed", "score", "processing_error"}
    }
    encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def hydrate_record_from_observation(record: Record, metadata: dict) -> bool:
    """Enrich an existing Record when later source metadata becomes better."""
    changed = False

    def replace(attribute: str, value, *, allow_empty: bool = False) -> None:
        nonlocal changed
        if value is None or (not allow_empty and value in ("", [], {})):
            return
        if getattr(record, attribute) != value:
            setattr(record, attribute, value)
            changed = True

    replace("title", metadata["title"])
    replace("authors", metadata["authors"])
    replace("journal", metadata["journal"])
    replace("publication_date", metadata["publication_date"])
    replace("publication_date_precision", metadata["publication_date_precision"])
    replace("raw_date_parts", metadata["raw_date_parts"])
    replace("abstract", metadata["abstract"])
    replace("publication_status", metadata["publication_status"])
    merged_ids = {**(record.extra_metadata or {}), **metadata["other_ids"]}
    if merged_ids != (record.extra_metadata or {}):
        record.extra_metadata = merged_ids
        changed = True
    expected_level = EvidenceLevel.E1.value if record.abstract else EvidenceLevel.E0.value
    if record.evidence_level != expected_level:
        record.evidence_level = expected_level
        changed = True
    return changed


def _provider_failure(exc: Exception) -> bool:
    if isinstance(exc, httpx.RequestError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


async def _process_event(
    session: AsyncSession,
    event: DiscoveryEvent,
    source: Source,
    resolver: WorkIdentityResolver,
    llm_client: LLMClient | None,
    result: MonitorRunResult,
    *,
    observation_changed: bool,
) -> None:
    raw_work = event.raw_metadata
    try:
        metadata = (
            parse_pubmed_metadata(raw_work)
            if source.name == "pubmed" else parse_crossref_metadata(raw_work)
        )
        doi = metadata["doi"]
        pmid = str(metadata["other_ids"].get("pmid", "")).strip()

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

        if doi:
            record, created = await get_or_create_record(session, doi, record_factory)
        else:
            record_id = f"R_pmid_{pmid}"
            record = (await session.execute(
                select(Record).where(Record.record_id == record_id)
            )).scalar_one_or_none()
            created = record is None
            if record is None:
                record = record_factory("")
                record.record_id = record_id
                record.doi = None
                session.add(record)
                await session.flush()
        hydrated = False if created else hydrate_record_from_observation(record, metadata)
        if created:
            result.created += 1
        elif hydrated or observation_changed:
            result.updated += 1
        else:
            result.duplicate += 1
        await session.flush()
        snapshot = await create_source_snapshot(session, record, source.name)
        if record.work_id is None:
            edge = await resolver.resolve_or_create_work(record, raw_work.get("relation", {}))
            await resolver.materialize_if_confirmed(record, edge)
        event.work_id = record.work_id
        event.status = (
            "IDENTITY_REVIEW" if not record.work_id else
            "INGESTED" if snapshot.analysis_text else "L0_READY"
        )
        if llm_client and snapshot.analysis_text and record.work_id:
            _, created_new = await run_l1_with_status(session, record, snapshot, llm_client)
            event.status = "L1_READY"
            if created_new:
                result.l1_generated += 1
        if "processing_error" in event.raw_metadata:
            event.raw_metadata = {
                key: value for key, value in event.raw_metadata.items()
                if key != "processing_error"
            }
        result.processed += 1
    except Exception as exc:
        event.status = "FAILED"
        event.raw_metadata = {**raw_work, "processing_error": str(exc)}
        result.failed += 1


async def run_monitor_subscription(
    session: AsyncSession,
    subscription: MonitorSubscription,
    adapter: CrossrefAdapter | PubMedAdapter,
    llm_client: LLMClient | None = None,
    *,
    now: datetime | None = None,
) -> MonitorRunResult:
    """Process recoverable local events, then fetch a budgeted provider delta."""
    result = MonitorRunResult()
    now = now or datetime.utcnow()
    source = await session.get(Source, subscription.source_id)
    if source is None:
        raise ValueError(f"Subscription {subscription.id} references a missing Source")
    running = (await session.execute(select(MonitorRun).where(
        MonitorRun.subscription_id == subscription.id,
        MonitorRun.status == "RUNNING",
    ).order_by(MonitorRun.started_at.desc()))).scalars().first()
    if running:
        lock_timeout = timedelta(
            minutes=int(subscription.config.get("run_lock_timeout_minutes", 30))
        )
        if running.started_at >= now - lock_timeout:
            result.skipped = True
            return result
        running.status = "ABANDONED"
        running.finished_at = now
        running.error = "stale subscription run lock recovered"
        await session.flush()
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
    page_size = min(int(subscription.config.get("page_size", 100)), 1000)
    max_items = int(subscription.config.get("max_items_per_run", 500))
    max_pages = int(subscription.config.get("max_pages_per_run", 5))
    feed_mode = subscription.config.get("feed_mode", "created")
    active_window = cursor.state.get("active_window") if cursor.cursor_value else None
    if active_window:
        start = datetime.fromisoformat(active_window["from"])
        until = datetime.fromisoformat(active_window["until"])
        adapter_cursor = cursor.cursor_value
    else:
        start = (cursor.last_success_at - timedelta(days=1)) if cursor.last_success_at else now - timedelta(days=lookback_days)
        until = now
        adapter_cursor = "*"

    monitor_run = MonitorRun(
        subscription_id=subscription.id, started_at=now,
        window_from=start, window_until=until, cursor_start=adapter_cursor,
    )
    session.add(monitor_run)
    await session.flush()
    resolver = WorkIdentityResolver(session)
    processed_event_ids: set[int] = set()

    # A previous run may have ingested without LLM, failed, or awaited identity.
    # Recovery does not depend on Crossref returning the DOI again.
    recoverable_statuses = ["DISCOVERED", "FAILED", "IDENTITY_REVIEW"]
    if llm_client:
        recoverable_statuses.append("INGESTED")
    incomplete = (await session.execute(select(DiscoveryEvent).where(
        DiscoveryEvent.subscription_id == subscription.id,
        DiscoveryEvent.status.in_(recoverable_statuses),
    ))).scalars().all()
    for event in incomplete:
        await _process_event(
            session, event, source, resolver, llm_client, result,
            observation_changed=False,
        )
        processed_event_ids.add(event.id)

    fetch_started = datetime.utcnow()
    has_more = result.processed >= max_items
    try:
        while result.pages < max_pages and result.processed < max_items:
            remaining = max_items - result.processed
            requested_rows = min(page_size, remaining)
            items, next_cursor = await adapter.discover_works(
                from_date=start.date().isoformat(), until_date=until.date().isoformat(),
                issn=subscription.config.get("issn"), query=subscription.config.get("query"),
                feed_mode=feed_mode, cursor=adapter_cursor, rows=requested_rows,
            )
            result.pages += 1
            health.last_success = datetime.utcnow()
            health.consecutive_failures = 0
            health.status = "healthy"
            health.last_error = None
            health.latency_ms = (datetime.utcnow() - fetch_started).total_seconds() * 1000
            for raw_work in items:
                raw_doi = raw_work.get("DOI")
                raw_pmid = str(raw_work.get("PMID", "")).strip()
                if not raw_doi and not raw_pmid:
                    result.failed += 1
                    continue
                external_identifier = normalize_doi(raw_doi) if raw_doi else f"pmid:{raw_pmid}"
                observed_hash = metadata_hash(raw_work)
                event = (await session.execute(select(DiscoveryEvent).where(
                    DiscoveryEvent.subscription_id == subscription.id,
                    DiscoveryEvent.external_identifier == external_identifier,
                ))).scalar_one_or_none()
                if event is None:
                    event = DiscoveryEvent(
                        source_id=source.id, subscription_id=subscription.id,
                        external_identifier=external_identifier,
                        source_url=(
                            f"https://doi.org/{external_identifier}" if raw_doi
                            else f"https://pubmed.ncbi.nlm.nih.gov/{raw_pmid}/"
                        ),
                        raw_metadata=raw_work, status="DISCOVERED",
                        last_seen_at=now, last_metadata_hash=observed_hash,
                    )
                    session.add(event)
                    await session.flush()
                    result.discovered += 1
                    changed = True
                else:
                    changed = event.last_metadata_hash != observed_hash
                    event.last_seen_at = now
                    if changed:
                        event.raw_metadata = raw_work
                        event.last_metadata_hash = observed_hash
                    elif event.status == "L1_READY":
                        result.duplicate += 1
                        continue
                if event.id not in processed_event_ids or changed:
                    await _process_event(
                        session, event, source, resolver, llm_client, result,
                        observation_changed=changed,
                    )
                    processed_event_ids.add(event.id)
            has_more = len(items) >= requested_rows and bool(next_cursor)
            adapter_cursor = next_cursor if has_more else None
            if not has_more:
                break
    except Exception as exc:
        cursor.last_checked_at = now
        result.error = str(exc)
        monitor_run.status = "FAILED"
        monitor_run.error = str(exc)
        if _provider_failure(exc):
            health.last_failure = datetime.utcnow()
            health.consecutive_failures += 1
            health.status = "down" if health.consecutive_failures >= 3 else "degraded"
            health.last_error = str(exc)
        has_more = bool(adapter_cursor and adapter_cursor != "*")

    result.has_more = has_more
    cursor.last_checked_at = now
    cursor.cursor_value = adapter_cursor if has_more else None
    if not has_more and result.error is None:
        cursor.last_success_at = now
    cursor.last_seen_identifier = next((
        normalize_doi(item["DOI"]) if item.get("DOI") else f"pmid:{item['PMID']}"
        for item in reversed(locals().get("items", [])) if item.get("DOI") or item.get("PMID")
    ), cursor.last_seen_identifier)
    cursor.state = {
        "from_date": start.date().isoformat(), "until_date": until.date().isoformat(),
        "feed_mode": feed_mode,
        "active_window": {"from": start.isoformat(), "until": until.isoformat()} if has_more else None,
        "has_more": has_more, "last_result": result.__dict__,
    }
    monitor_run.finished_at = datetime.utcnow()
    monitor_run.cursor_end = cursor.cursor_value
    monitor_run.discovered = result.discovered
    monitor_run.created = result.created
    monitor_run.updated = result.updated
    monitor_run.l1_generated = result.l1_generated
    monitor_run.failed = result.failed
    monitor_run.has_more = result.has_more
    if monitor_run.status != "FAILED":
        monitor_run.status = (
            "PARTIAL" if result.has_more else
            "COMPLETED_WITH_ERRORS" if result.failed else "COMPLETED"
        )
    await session.flush()
    return result
