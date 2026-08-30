"""Replay a historical monitor window one virtual day at a time."""

import argparse
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import func, select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.adapters.crossref import CrossrefAdapter
from src.adapters.pubmed import PubMedAdapter
from src.core.clock import FakeClock
from src.core.config import load_config
from src.core.database import Database
from src.core.models import (
    DiscoveryEvent, MonitorRun, MonitorSubscription, Record, Source, SourceCursor,
    Work, WorkStatus,
)
from src.workflows.scheduler import MonitorScheduler


class SimulatedProviderFailure:
    async def discover_works(self, **kwargs):
        request = httpx.Request("GET", "https://acceptance.invalid")
        raise httpx.ConnectError("simulated replay provider failure", request=request)

    async def close(self):
        return None


class ReplayAdapterFactory:
    def __init__(self, args: argparse.Namespace):
        self.provider = args.provider
        self.failed_days = set(args.fail_on_day)
        self.day = 0
        config_path = PROJECT_ROOT / "config.yaml"
        config = load_config(config_path) if config_path.exists() else None
        default_email = "monitor-acceptance@example.invalid"
        self.email = args.email or (
            (config.pubmed.email or config.crossref.email)
            if config and args.provider == "pubmed" else
            config.crossref.email if config else default_email
        )
        self.api_key = args.api_key or (config.pubmed.api_key if config else None)
        self.timeout = (
            config.pubmed.timeout if config and args.provider == "pubmed" else
            config.crossref.timeout if config else 30.0
        )
        self.rate_limit = config.crossref.rate_limit if config else 10.0

    def __call__(self, source: Source):
        if self.day in self.failed_days:
            return SimulatedProviderFailure()
        if source.name == "pubmed":
            return PubMedAdapter(self.email, api_key=self.api_key, timeout=self.timeout)
        return CrossrefAdapter(self.email, rate_limit=self.rate_limit, timeout=self.timeout)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="from_date", type=datetime.fromisoformat, required=True)
    parser.add_argument("--to", dest="to_date", type=datetime.fromisoformat, required=True)
    parser.add_argument("--provider", choices=["crossref", "pubmed"], required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--email", help="provider contact email; defaults to config.yaml")
    parser.add_argument("--api-key", help="optional PubMed API key")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--issn")
    target.add_argument("--query")
    parser.add_argument("--database", type=Path, default=Path("data/monitor_acceptance.db"))
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--restart-each-day", action="store_true")
    parser.add_argument("--repeat-each-day", type=int, default=1)
    parser.add_argument("--fail-on-day", type=int, action="append", default=[])
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-items", type=int, default=500)
    parser.add_argument("--max-pages", type=int, default=5)
    return parser.parse_args()


async def ensure_subscription(database: Database, args: argparse.Namespace) -> int:
    await database.init_db()
    async with database.get_session() as session:
        source = (await session.execute(select(Source).where(
            Source.name == args.provider
        ))).scalar_one_or_none()
        if source is None:
            source = Source(name=args.provider, source_type="api", config={})
            session.add(source)
            await session.flush()
        subscription = (await session.execute(select(MonitorSubscription).where(
            MonitorSubscription.name == args.name
        ))).scalar_one_or_none()
        if subscription is None:
            config = {
                "lookback_days": 0, "cursor_overlap_days": 0,
                "interval_hours": 24, "feed_mode": "created",
                "page_size": args.page_size, "max_items_per_run": args.max_items,
                "max_pages_per_run": args.max_pages,
            }
            if args.issn:
                config["issn"] = args.issn
            if args.query:
                config["query"] = args.query
            subscription = MonitorSubscription(
                name=args.name,
                subscription_type="journal" if args.issn else "topic",
                source_id=source.id, config=config, enabled=True,
            )
            session.add(subscription)
            await session.commit()
        return subscription.id


async def latest_run(database: Database, subscription_id: int) -> MonitorRun | None:
    async with database.get_session() as session:
        return (await session.execute(select(MonitorRun).where(
            MonitorRun.subscription_id == subscription_id
        ).order_by(MonitorRun.id.desc()).limit(1))).scalar_one_or_none()


async def final_invariants(database: Database) -> dict[str, int]:
    async with database.get_session() as session:
        duplicate_dois = (await session.execute(select(func.count()).select_from(
            select(Record.doi).where(Record.doi.is_not(None)).group_by(Record.doi).having(
                func.count(Record.id) > 1
            ).subquery()
        ))).scalar_one()
        merged_event_refs = (await session.execute(select(func.count(DiscoveryEvent.id)).join(
            Work, DiscoveryEvent.work_id == Work.id
        ).where(Work.status == WorkStatus.MERGED.value))).scalar_one()
        running = (await session.execute(select(func.count(MonitorRun.id)).where(
            MonitorRun.status == "RUNNING"
        ))).scalar_one()
        backlog = (await session.execute(select(func.count(SourceCursor.id)).where(
            SourceCursor.cursor_value.is_not(None)
        ))).scalar_one()
        return {
            "works": (await session.execute(select(func.count(Work.id)))).scalar_one(),
            "events": (await session.execute(select(func.count(DiscoveryEvent.id)))).scalar_one(),
            "duplicate_dois": duplicate_dois,
            "merged_event_refs": merged_event_refs,
            "running_runs": running,
            "remaining_backlog": backlog,
        }


async def simulate(args: argparse.Namespace) -> int:
    if args.to_date < args.from_date:
        raise ValueError("--to must not be earlier than --from")
    database_path = args.database.resolve()
    if args.reset:
        if not database_path.is_relative_to(PROJECT_ROOT.resolve()):
            raise ValueError("--reset only deletes databases inside the project workspace")
        for path in (
            database_path,
            database_path.with_name(database_path.name + "-wal"),
            database_path.with_name(database_path.name + "-shm"),
        ):
            if path.exists():
                path.unlink()
    clock = FakeClock(args.from_date.replace(hour=6))
    factory = ReplayAdapterFactory(args)
    total_days = (args.to_date.date() - args.from_date.date()).days + 1
    print(f"Simulation: {total_days} days · {args.provider} · {args.name}")
    database = None
    scheduler = None
    subscription_id = None
    for day in range(1, total_days + 1):
        if database is None or args.restart_each_day:
            if database is not None:
                await database.close()
            database = Database(database_path)
            subscription_id = await ensure_subscription(database, args)
            scheduler = MonitorScheduler(
                database, factory, _no_llm, default_interval_hours=24, clock=clock,
            )
        factory.day = day
        await scheduler.run_due_once()
        run = await latest_run(database, subscription_id)
        print(
            f"Day {day:02d} {clock.now().date()} · {run.status if run else 'SKIPPED'} · "
            f"discovered={run.discovered if run else 0} created={run.created if run else 0} "
            f"updated={run.updated if run else 0} failed={run.failed if run else 0}"
        )
        for _ in range(max(1, args.repeat_each_day) - 1):
            assert await scheduler.run_due_once() == [], "same virtual day ran twice"
        clock.advance(timedelta(days=1))
    metrics = await final_invariants(database)
    await database.close()
    passed = (
        metrics["works"] > 0 and metrics["duplicate_dois"] == 0
        and metrics["merged_event_refs"] == 0 and metrics["running_runs"] == 0
        and metrics["remaining_backlog"] == 0
    )
    print("Final:", " ".join(f"{key}={value}" for key, value in metrics.items()))
    print("PASS" if passed else "FAIL")
    return 0 if passed else 1


async def _no_llm():
    return None


if __name__ == "__main__":
    raise SystemExit(asyncio.run(simulate(parse_args())))
