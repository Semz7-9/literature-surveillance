"""Product-level checks for PubMed monitoring and unattended scheduling."""

import asyncio
from datetime import datetime, timedelta

from sqlalchemy import select

from src.core.database import Database
from src.core.models import DiscoveryEvent, MonitorSubscription, Record, Source, SourceCursor
from src.workflows.monitor import run_monitor_subscription
from src.workflows.scheduler import MonitorScheduler


PUBMED_ITEM = {
    "PMID": "41234567", "DOI": None,
    "title": "A PubMed-only chemical biology study",
    "authors": [{"name": "Ada Researcher", "affiliation": [], "orcid": None}],
    "journal": "Journal of Chemical Biology",
    "date": {"year": "2026", "month": "Aug", "day": "28"},
    "abstract": "This abstract is discovered and hydrated directly from PubMed.",
    "pmcid": "PMC123456", "mesh": ["Chemical Biology", "Proteins"],
}


class FakePubMed:
    def __init__(self):
        self.calls = 0
        self.closed = False

    async def discover_works(self, **kwargs):
        self.calls += 1
        return [PUBMED_ITEM], None

    async def close(self):
        self.closed = True


async def _seed_subscription(database: Database) -> int:
    await database.init_db()
    async with database.get_session() as session:
        source = Source(name="pubmed", source_type="api", config={})
        session.add(source)
        await session.flush()
        subscription = MonitorSubscription(
            name="PubMed topic", subscription_type="topic", source_id=source.id,
            config={"query": "chemical biology", "interval_hours": 24},
        )
        session.add(subscription)
        await session.commit()
        return subscription.id


async def test_pubmed_monitor_keeps_abstract_mesh_and_pmid_without_doi(tmp_path):
    database = Database(tmp_path / "pubmed.db")
    subscription_id = await _seed_subscription(database)
    async with database.get_session() as session:
        subscription = await session.get(MonitorSubscription, subscription_id)
        result = await run_monitor_subscription(
            session, subscription, FakePubMed(), now=datetime(2026, 8, 29, 6)
        )
        await session.commit()
        record = (await session.execute(select(Record))).scalar_one()
        event = (await session.execute(select(DiscoveryEvent))).scalar_one()
        assert result.discovered == 1
        assert record.doi is None
        assert record.abstract.startswith("This abstract")
        assert record.extra_metadata["pmid"] == "41234567"
        assert record.extra_metadata["mesh"] == ["Chemical Biology", "Proteins"]
        assert event.external_identifier == "pmid:41234567"
        assert event.work_id is not None
    await database.close()


async def test_scheduler_runs_daily_for_a_simulated_unattended_week(tmp_path):
    database = Database(tmp_path / "scheduler.db")
    subscription_id = await _seed_subscription(database)
    adapters = []

    def adapter_factory(source):
        assert source.name == "pubmed"
        adapter = FakePubMed()
        adapters.append(adapter)
        return adapter

    async def no_llm():
        return None

    scheduler = MonitorScheduler(database, adapter_factory, no_llm)
    first = datetime(2026, 8, 29, 6)
    assert await scheduler.run_due_once(now=first) == [subscription_id]
    assert await scheduler.run_due_once(now=first + timedelta(hours=23)) == []
    for day in range(1, 8):
        assert await scheduler.run_due_once(
            now=first + timedelta(days=day)
        ) == [subscription_id]
    assert len(adapters) == 8 and all(adapter.closed for adapter in adapters)
    async with database.get_session() as session:
        cursor = (await session.execute(select(SourceCursor))).scalar_one()
        assert cursor.last_checked_at == first + timedelta(days=7)
    await database.close()


async def test_scheduler_skips_disabled_and_resumes_backlog_after_short_cooldown(tmp_path):
    database = Database(tmp_path / "backlog.db")
    subscription_id = await _seed_subscription(database)
    checked_at = datetime(2026, 8, 29, 6)
    async with database.get_session() as session:
        subscription = await session.get(MonitorSubscription, subscription_id)
        subscription.config = {
            **subscription.config, "backlog_cooldown_minutes": 5,
        }
        session.add(SourceCursor(
            subscription_id=subscription_id, last_checked_at=checked_at,
            cursor_value="100", state={"has_more": True},
        ))
        await session.commit()

    adapters = []
    async def no_llm():
        return None
    scheduler = MonitorScheduler(
        database, lambda source: adapters.append(FakePubMed()) or adapters[-1], no_llm
    )
    assert await scheduler.run_due_once(now=checked_at + timedelta(minutes=4)) == []
    assert await scheduler.run_due_once(now=checked_at + timedelta(minutes=5)) == [subscription_id]
    async with database.get_session() as session:
        subscription = await session.get(MonitorSubscription, subscription_id)
        subscription.enabled = False
        await session.commit()
    assert await scheduler.run_due_once(now=checked_at + timedelta(days=2)) == []
    assert len(adapters) == 1
    await database.close()


async def test_scheduler_failure_isolated_and_start_stop_cleanly(tmp_path):
    database = Database(tmp_path / "isolation.db")
    first_id = await _seed_subscription(database)
    async with database.get_session() as session:
        first = await session.get(MonitorSubscription, first_id)
        second = MonitorSubscription(
            name="Second topic", subscription_type="topic", source_id=first.source_id,
            config={"query": "medicinal chemistry", "interval_hours": 24},
        )
        session.add(second)
        await session.commit()
        second_id = second.id

    calls = 0
    def adapter_factory(source):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("one subscription cannot construct its adapter")
        return FakePubMed()
    async def no_llm():
        return None

    scheduler = MonitorScheduler(database, adapter_factory, no_llm, poll_seconds=3600)
    assert await scheduler.run_due_once(now=datetime(2026, 8, 29, 6)) == [second_id]
    scheduler.start()
    await asyncio.sleep(0.05)
    assert scheduler.task is not None and not scheduler.task.done()
    await scheduler.stop()
    assert scheduler.task is None
    await database.close()
