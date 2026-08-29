"""Monitor MVP regression tests with a deterministic fake discovery adapter."""

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from src.core.database import Database
from src.core.models import (
    AnalysisArtifact, DiscoveryEvent, MonitorSubscription, Record, Source,
    SourceCursor, SourceHealth, Work,
)
from src.workflows.monitor import run_monitor_subscription
from skills.l1_literature_card.contract import L1Output


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "monitor.db")
    await database.init_db()
    yield database
    await database.close()


class FakeCrossrefDiscovery:
    def __init__(self):
        self.calls = []

    async def discover_works(self, **kwargs):
        self.calls.append(kwargs)
        return ([{
            "DOI": "10.1000/MONITOR",
            "title": ["A newly monitored KRAS paper"],
            "author": [{"given": "Jane", "family": "Doe"}],
            "container-title": ["Journal of Tests"],
            "published": {"date-parts": [[2026, 8, 29]]},
            "abstract": "KRAS mutations drive cancer.",
            "type": "journal-article",
            "relation": {},
        }], "next-page-token")


class FakeLLM:
    def __init__(self):
        self.calls = 0

    async def call_with_schema(self, prompt, schema, system_prompt=None):
        self.calls += 1
        return L1Output(
            one_sentence="A monitored study of KRAS mutations.",
            tags=["KRAS", "cancer", "monitor"],
            research_object="KRAS",
            research_object_missing_reason=None,
            major_method=None,
            major_method_missing_reason="NOT_STATED",
            author_reported_result=None,
            author_reported_result_missing_reason="NOT_STATED",
            evidence_spans={"research_object": "KRAS mutations drive cancer."},
        )


async def test_subscription_discovers_processes_and_deduplicates(db: Database):
    async with db.get_session() as session:
        source = Source(name="crossref", source_type="api", config={})
        session.add(source)
        await session.flush()
        subscription = MonitorSubscription(
            name="JTest", subscription_type="journal", source_id=source.id,
            config={"issn": "1234-5678", "lookback_days": 3},
        )
        session.add(subscription)
        await session.flush()
        adapter, llm = FakeCrossrefDiscovery(), FakeLLM()
        now = datetime(2026, 8, 29, 12)

        first = await run_monitor_subscription(session, subscription, adapter, llm, now=now)
        await session.commit()
        assert first.discovered == 1
        assert first.created == 1
        assert first.l1_generated == 1
        assert llm.calls == 1

        event = (await session.execute(select(DiscoveryEvent))).scalar_one()
        record = (await session.execute(select(Record))).scalar_one()
        work = await session.get(Work, event.work_id)
        assert event.external_identifier == "10.1000/monitor"
        assert event.status == "L1_READY"
        assert record.work_id == work.id
        assert (await session.execute(select(AnalysisArtifact))).scalar_one()
        cursor = (await session.execute(select(SourceCursor))).scalar_one()
        assert cursor.last_success_at == now
        assert cursor.last_seen_identifier == "10.1000/monitor"
        health = (await session.execute(select(SourceHealth))).scalar_one()
        assert health.status == "healthy"

        second = await run_monitor_subscription(session, subscription, adapter, llm, now=now)
        assert second.discovered == 0
        assert second.duplicate == 1
        assert llm.calls == 1
        assert len((await session.execute(select(DiscoveryEvent))).scalars().all()) == 1


class FailingAdapter:
    async def discover_works(self, **kwargs):
        raise RuntimeError("Crossref unavailable")


async def test_fetch_failure_updates_source_health_without_advancing_success(db: Database):
    async with db.get_session() as session:
        source = Source(name="crossref", source_type="api", config={})
        session.add(source)
        await session.flush()
        subscription = MonitorSubscription(
            name="Broken", subscription_type="topic", source_id=source.id,
            config={"query": "chemistry"},
        )
        session.add(subscription)
        await session.flush()
        result = await run_monitor_subscription(session, subscription, FailingAdapter())
        assert result.error == "Crossref unavailable"
        health = (await session.execute(select(SourceHealth))).scalar_one()
        assert health.status == "degraded"
        cursor = (await session.execute(select(SourceCursor))).scalar_one()
        assert cursor.last_success_at is None


class PagingAdapter(FakeCrossrefDiscovery):
    async def discover_works(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["cursor"] == "next-page-token":
            return [], None
        items, token = await super().discover_works(**kwargs)
        return items, token


async def test_crossref_cursor_resumes_same_window_before_advancing(db: Database):
    async with db.get_session() as session:
        source = Source(name="crossref", source_type="api", config={})
        session.add(source)
        await session.flush()
        subscription = MonitorSubscription(
            name="Paged", subscription_type="journal", source_id=source.id,
            config={"issn": "1234-5678", "max_results": 1},
        )
        session.add(subscription)
        await session.flush()
        adapter = PagingAdapter()
        now = datetime(2026, 8, 29, 12)
        await run_monitor_subscription(session, subscription, adapter, now=now)
        cursor = (await session.execute(select(SourceCursor))).scalar_one()
        assert cursor.cursor_value == "next-page-token"
        assert cursor.last_success_at is None
        await run_monitor_subscription(session, subscription, adapter, now=now)
        assert adapter.calls[-1]["cursor"] == "next-page-token"
        assert cursor.cursor_value is None
        assert cursor.last_success_at == now
