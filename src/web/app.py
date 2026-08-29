"""FastAPI application for the deliberately small UI-0 vertical slice."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import AsyncIterator, Callable

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from ..core.config import load_config
from ..core.database import Database
from ..core.models import (
    AnalysisArtifact, DiscoveryEvent, IdentityEdge, MonitorRun, MonitorSubscription,
    PendingIdentifierRelation, ReadingQueue, Record, Source, SourceCursor,
    SourceHealth, UserWorkState, Work, WorkIdentifier, WorkMergeAudit, WorkStatus,
)
from ..adapters.crossref import CrossrefAdapter
from ..adapters.pubmed import PubMedAdapter
from ..llm.client import create_llm_client
from ..workflows.monitor import run_monitor_subscription
from ..workflows.scheduler import MonitorScheduler

WEB_ROOT = Path(__file__).parent
templates = Jinja2Templates(directory=str(WEB_ROOT / "templates"))


def default_database_path() -> Path:
    config_path = Path("config.yaml")
    return Path(load_config(config_path).database.path) if config_path.exists() else Path("data/literature.db")


def create_app(
    database_path: str | Path | None = None,
    crossref_factory: Callable[[], CrossrefAdapter] | None = None,
    pubmed_factory: Callable[[], PubMedAdapter] | None = None,
    llm_factory: Callable | None = None,
    scheduler_enabled: bool | None = None,
) -> FastAPI:
    database = Database(database_path or default_database_path())
    config_path = Path("config.yaml")
    runtime_config = load_config(config_path) if config_path.exists() else None

    def make_crossref() -> CrossrefAdapter:
        if crossref_factory:
            return crossref_factory()
        if runtime_config:
            return CrossrefAdapter(
                email=runtime_config.crossref.email,
                rate_limit=runtime_config.crossref.rate_limit,
                timeout=runtime_config.crossref.timeout,
            )
        return CrossrefAdapter(email="local-monitor@example.invalid")

    def make_pubmed() -> PubMedAdapter:
        if pubmed_factory:
            return pubmed_factory()
        email = (
            runtime_config.pubmed.email or runtime_config.crossref.email
            if runtime_config else "local-monitor@example.invalid"
        )
        timeout = runtime_config.pubmed.timeout if runtime_config else 30.0
        api_key = runtime_config.pubmed.api_key if runtime_config else None
        return PubMedAdapter(email=email, api_key=api_key, timeout=timeout)

    def make_adapter(source: Source):
        if source.name == "pubmed":
            return make_pubmed()
        if source.name == "crossref":
            return make_crossref()
        raise ValueError(f"不支持的监控来源：{source.name}")

    async def make_llm():
        if llm_factory:
            return await llm_factory()
        if runtime_config is None:
            return None
        if not runtime_config.llm.api_key or runtime_config.llm.api_key.startswith("your-"):
            return None
        return await create_llm_client(runtime_config.llm.model_dump(), model_tier="cheap")

    scheduler = MonitorScheduler(
        database, make_adapter, make_llm,
        default_interval_hours=(runtime_config.monitor.check_interval_hours if runtime_config else 24),
    )
    should_schedule = (
        scheduler_enabled if scheduler_enabled is not None
        else bool(runtime_config and runtime_config.monitor.enabled)
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await database.init_db()
        if should_schedule:
            scheduler.start()
        yield
        await scheduler.stop()
        await database.close()

    app = FastAPI(title="Literature Surveillance v0.1", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(WEB_ROOT / "static")), name="static")
    app.state.database = database
    app.state.monitor_scheduler = scheduler
    app.state.scheduler_enabled = should_schedule

    async def work_view(work: Work, session, discovery: DiscoveryEvent | None = None) -> dict:
        preferred = await session.get(Record, work.preferred_record_id) if work.preferred_record_id else None
        if preferred is None:
            preferred = (await session.execute(select(Record).where(Record.work_id == work.id).limit(1))).scalar_one_or_none()
        state = (await session.execute(select(UserWorkState).where(UserWorkState.work_id == work.id))).scalar_one_or_none()
        queued = (await session.execute(select(ReadingQueue).where(
            ReadingQueue.work_id == work.id,
            ReadingQueue.requested_level == "L2",
            ReadingQueue.status == "pending",
        ))).scalar_one_or_none()
        artifact = None
        if preferred:
            artifact = (await session.execute(select(AnalysisArtifact).where(
                AnalysisArtifact.record_id == preferred.id, AnalysisArtifact.analysis_type == "L1"
            ).order_by(AnalysisArtifact.created_at.desc()).limit(1))).scalar_one_or_none()
        subscription = await session.get(MonitorSubscription, discovery.subscription_id) if discovery else None
        source = await session.get(Source, discovery.source_id) if discovery else None
        return {
            "work": work, "record": preferred, "state": state, "queued": queued,
            "artifact": artifact,
            "discovery": discovery, "subscription": subscription, "source": source,
        }

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        async with database.get_session() as session:
            events = (await session.execute(select(DiscoveryEvent))).scalars().all()
            discovered_work_ids = {event.work_id for event in events if event.work_id is not None}
            works = [
                work for work_id in discovered_work_ids
                if (work := await session.get(Work, work_id)) and work.status == WorkStatus.ACTIVE.value
            ]
            cards = [await work_view(work, session) for work in works]
            week_start = datetime.utcnow() - timedelta(days=7)
            stats = {
                "discovered": sum(event.discovered_at >= week_start for event in events),
                "in_scope": len(works),
                "processed": sum(card["artifact"] is not None for card in cards),
                "saved": sum(
                    card["state"] is not None and card["state"].state == "keep" for card in cards
                ),
            }
            return templates.TemplateResponse(request, "dashboard.html", {"stats": stats})

    @app.get("/monitor", response_class=HTMLResponse)
    async def inbox(
        request: Request,
        state: str | None = None,
        notice: str | None = None,
        period: str = "week",
        subscription_id: int | None = None,
        source_id: int | None = None,
        discovered: int | None = None,
        updated: int | None = None,
        has_more: int | None = None,
    ):
        async with database.get_session() as session:
            event_stmt = select(DiscoveryEvent).where(DiscoveryEvent.work_id.is_not(None))
            now = datetime.utcnow()
            week_start = (now - timedelta(days=now.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            week_end = week_start + timedelta(days=7)
            if period == "today":
                day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                event_stmt = event_stmt.where(DiscoveryEvent.discovered_at >= day_start)
            elif period == "week":
                event_stmt = event_stmt.where(
                    DiscoveryEvent.discovered_at >= week_start,
                    DiscoveryEvent.discovered_at < week_end,
                )
            if subscription_id:
                event_stmt = event_stmt.where(DiscoveryEvent.subscription_id == subscription_id)
            if source_id:
                event_stmt = event_stmt.where(DiscoveryEvent.source_id == source_id)
            events = (await session.execute(
                event_stmt.order_by(DiscoveryEvent.discovered_at.desc())
            )).scalars().all()
            latest_by_work = {}
            for event in events:
                latest_by_work.setdefault(event.work_id, event)
            cards = []
            for work_id, event in latest_by_work.items():
                work = await session.get(Work, work_id)
                if work and work.status == WorkStatus.ACTIVE.value:
                    cards.append(await work_view(work, session, event))
            weekly_events = (await session.execute(select(DiscoveryEvent).where(
                DiscoveryEvent.work_id.is_not(None),
                DiscoveryEvent.discovered_at >= week_start,
                DiscoveryEvent.discovered_at < week_end,
            ).order_by(DiscoveryEvent.discovered_at.desc()))).scalars().all()
            weekly_latest = {}
            for event in weekly_events:
                weekly_latest.setdefault(event.work_id, event)
            weekly_cards = []
            for work_id, event in weekly_latest.items():
                work = await session.get(Work, work_id)
                if work and work.status == WorkStatus.ACTIVE.value:
                    weekly_cards.append(await work_view(work, session, event))
            weekly_stats = {
                "new": len(weekly_cards),
                "l1": sum(card["artifact"] is not None for card in weekly_cards),
                "no_abstract": sum(
                    card["record"] is None or not card["record"].abstract for card in weekly_cards
                ),
                "unread": sum(
                    card["state"] is None and card["queued"] is None for card in weekly_cards
                ),
            }
            if state == "unread":
                cards = [card for card in cards if card["state"] is None and card["queued"] is None]
            elif state == "l2":
                cards = [card for card in cards if card["queued"] is not None]
            elif state:
                cards = [card for card in cards if card["state"] and card["state"].state == state]
            subscriptions = (await session.execute(
                select(MonitorSubscription).order_by(MonitorSubscription.created_at)
            )).scalars().all()
            sources = {source.id: source for source in (await session.execute(select(Source))).scalars().all()}
            health = {item.source_id: item for item in (await session.execute(select(SourceHealth))).scalars().all()}
            latest_runs = {}
            for run in (await session.execute(
                select(MonitorRun).order_by(MonitorRun.started_at.desc())
            )).scalars().all():
                latest_runs.setdefault(run.subscription_id, run)
            return templates.TemplateResponse(request, "inbox.html", {
                "cards": cards, "selected_state": state, "notice": notice,
                "period": period, "selected_subscription": subscription_id,
                "selected_source": source_id,
                "subscriptions": subscriptions, "sources": sources,
                "health": health, "run_discovered": discovered,
                "latest_runs": latest_runs, "run_updated": updated,
                "run_has_more": bool(has_more),
                "week_start": week_start, "week_end": week_end - timedelta(days=1),
                "weekly_stats": weekly_stats,
                "scheduler_enabled": app.state.scheduler_enabled,
                "scheduler_last_tick": app.state.monitor_scheduler.last_tick,
            })

    @app.get("/works/{work_id}", response_class=HTMLResponse)
    async def paper_detail(request: Request, work_id: int, notice: str | None = None):
        async with database.get_session() as session:
            work = await session.get(Work, work_id)
            if work is None:
                raise HTTPException(404, "Work not found")
            data = await work_view(work, session)
            content = data["artifact"].content if data["artifact"] else {}
            return templates.TemplateResponse(request, "paper_detail.html", {
                **data, "content": content, "notice": notice,
            })

    @app.get("/works/{work_id}/debug", response_class=HTMLResponse)
    async def debug_work(request: Request, work_id: int):
        async with database.get_session() as session:
            work = await session.get(Work, work_id)
            if work is None:
                raise HTTPException(404, "Work not found")
            records = (await session.execute(select(Record).where(Record.work_id == work.id))).scalars().all()
            identifiers = (await session.execute(select(WorkIdentifier).where(WorkIdentifier.work_id == work.id))).scalars().all()
            edges = (await session.execute(select(IdentityEdge).where(IdentityEdge.target_work_id == work.id))).scalars().all()
            pending = (await session.execute(select(PendingIdentifierRelation).where(
                PendingIdentifierRelation.source_record_id.in_([item.id for item in records] or [-1])
            ))).scalars().all()
            audits = (await session.execute(select(WorkMergeAudit).where(
                (WorkMergeAudit.merged_from_work_id == work.id) | (WorkMergeAudit.merged_into_work_id == work.id)
            ))).scalars().all()
            return templates.TemplateResponse(request, "debug.html", {
                "work": work, "records": records, "identifiers": identifiers, "edges": edges,
                "pending": pending, "audits": audits,
            })

    @app.get("/archives", response_class=HTMLResponse)
    async def archives(request: Request):
        # UI-0 intentionally has no archive persistence model yet.  This page
        # makes the future product surface visible without inventing one.
        return templates.TemplateResponse(request, "archives.html", {"archives": []})

    @app.get("/archives/structure", response_class=HTMLResponse)
    async def archive_structure(request: Request):
        return templates.TemplateResponse(request, "archive_detail.html", {})

    @app.post("/works/{work_id}/user-state")
    async def set_user_state(work_id: int, state: str = Form(...)):
        if state not in {"keep", "ignore"}:
            raise HTTPException(422, "state must be keep or ignore")
        async with database.get_session() as session:
            if await session.get(Work, work_id) is None:
                raise HTTPException(404, "Work not found")
            user_state = (await session.execute(select(UserWorkState).where(UserWorkState.work_id == work_id))).scalar_one_or_none()
            if user_state:
                user_state.state = state
                user_state.match_reason = {"source": "user_resolution"}
            else:
                session.add(UserWorkState(work_id=work_id, state=state, match_reason={"source": "UI-0"}))
            await session.commit()
        return RedirectResponse(url=f"/monitor?notice={state}", status_code=303)

    @app.post("/works/{work_id}/reading-queue")
    async def queue_l2(work_id: int):
        async with database.get_session() as session:
            if await session.get(Work, work_id) is None:
                raise HTTPException(404, "Work not found")
            existing = (await session.execute(select(ReadingQueue).where(
                ReadingQueue.work_id == work_id, ReadingQueue.requested_level == "L2", ReadingQueue.status == "pending"
            ))).scalar_one_or_none()
            if not existing:
                session.add(ReadingQueue(work_id=work_id, requested_level="L2", status="pending"))
                await session.commit()
        return RedirectResponse(url=f"/works/{work_id}?notice=queued", status_code=303)

    @app.post("/monitor/subscriptions")
    async def create_subscription(
        name: str = Form(...),
        subscription_type: str = Form(...),
        feed_mode: str = Form("created"),
        provider: str = Form("crossref"),
        interval_hours: int = Form(24),
        issn: str = Form(""),
        query: str = Form(""),
    ):
        if subscription_type not in {"journal", "topic"}:
            raise HTTPException(422, "目前仅支持期刊或主题订阅")
        if feed_mode not in {"created", "update"}:
            raise HTTPException(422, "feed mode 必须是 created 或 update")
        if provider not in {"crossref", "pubmed"}:
            raise HTTPException(422, "目前仅支持 Crossref 或 PubMed")
        if provider == "pubmed" and feed_mode != "created":
            raise HTTPException(422, "PubMed v0.1 仅支持 discovery/created feed")
        if interval_hours not in {6, 12, 24, 168}:
            raise HTTPException(422, "检查频率不受支持")
        if subscription_type == "journal" and not issn.strip():
            raise HTTPException(422, "期刊订阅必须填写 ISSN")
        if subscription_type == "topic" and not query.strip():
            raise HTTPException(422, "主题订阅必须填写检索式")
        async with database.get_session() as session:
            existing = (await session.execute(select(MonitorSubscription).where(
                MonitorSubscription.name == name.strip()
            ))).scalar_one_or_none()
            if existing:
                raise HTTPException(409, "订阅名称已存在")
            source = (await session.execute(select(Source).where(Source.name == provider))).scalar_one_or_none()
            if source is None:
                source = Source(name=provider, source_type="api", config={})
                session.add(source)
                await session.flush()
            config = {
                "lookback_days": 7, "feed_mode": feed_mode, "page_size": 100,
                "max_items_per_run": 500, "max_pages_per_run": 5,
                "interval_hours": interval_hours,
            }
            if issn.strip():
                config["issn"] = issn.strip()
            if query.strip():
                config["query"] = query.strip()
            session.add(MonitorSubscription(
                name=name.strip(), subscription_type=subscription_type,
                source_id=source.id, config=config,
            ))
            await session.commit()
        return RedirectResponse(url="/monitor?notice=subscription-created", status_code=303)

    @app.post("/monitor/subscriptions/{subscription_id}/toggle")
    async def toggle_subscription(subscription_id: int):
        async with database.get_session() as session:
            subscription = await session.get(MonitorSubscription, subscription_id)
            if subscription is None:
                raise HTTPException(404, "订阅不存在")
            subscription.enabled = not subscription.enabled
            await session.commit()
        notice = "subscription-enabled" if subscription.enabled else "subscription-paused"
        return RedirectResponse(url=f"/monitor?notice={notice}", status_code=303)

    @app.post("/monitor/subscriptions/{subscription_id}/frequency")
    async def update_frequency(subscription_id: int, interval_hours: int = Form(...)):
        if interval_hours not in {6, 12, 24, 168}:
            raise HTTPException(422, "检查频率不受支持")
        async with database.get_session() as session:
            subscription = await session.get(MonitorSubscription, subscription_id)
            if subscription is None:
                raise HTTPException(404, "订阅不存在")
            subscription.config = {**subscription.config, "interval_hours": interval_hours}
            await session.commit()
        return RedirectResponse(url="/monitor?notice=frequency-updated", status_code=303)

    @app.post("/monitor/subscriptions/{subscription_id}/run")
    async def run_subscription(subscription_id: int):
        llm_client = await make_llm()
        adapter = None
        try:
            async with database.get_session() as session:
                subscription = await session.get(MonitorSubscription, subscription_id)
                if subscription is None:
                    raise HTTPException(404, "订阅不存在")
                source = await session.get(Source, subscription.source_id)
                if source is None:
                    raise HTTPException(409, "订阅来源不存在")
                adapter = make_adapter(source)
                result = await run_monitor_subscription(session, subscription, adapter, llm_client)
                await session.commit()
        finally:
            if adapter:
                await adapter.close()
            if llm_client:
                await llm_client.close()
        if result.error:
            return RedirectResponse(url="/monitor?notice=check-failed", status_code=303)
        if result.skipped:
            return RedirectResponse(url="/monitor?notice=already-running", status_code=303)
        return RedirectResponse(url=(
            f"/monitor?notice=checked&discovered={result.discovered}"
            f"&updated={result.updated}&has_more={int(result.has_more)}"
        ), status_code=303)

    @app.get("/api/inbox")
    async def api_inbox():
        async with database.get_session() as session:
            events = (await session.execute(
                select(DiscoveryEvent).where(DiscoveryEvent.work_id.is_not(None)).order_by(
                    DiscoveryEvent.discovered_at.desc()
                )
            )).scalars().all()
            seen, response = set(), []
            for event in events:
                if event.work_id in seen:
                    continue
                seen.add(event.work_id)
                work = await session.get(Work, event.work_id)
                if work and work.status == WorkStatus.ACTIVE.value:
                    response.append({
                        "id": work.id, "work_id": work.work_id, "title": work.title,
                        "doi": work.canonical_doi, "discovered_at": event.discovered_at,
                        "subscription_id": event.subscription_id,
                    })
            return response

    return app


app = create_app()
