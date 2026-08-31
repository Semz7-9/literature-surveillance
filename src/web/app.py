"""Local UI for Topic Archive and Literature Monitor product workflows."""

import inspect
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from ..adapters.crossref import CrossrefAdapter
from ..adapters.openalex import OpenAlexAdapter
from ..adapters.pubmed import PubMedAdapter
from ..core.config import load_config
from ..core.database import Database
from ..core.models import (
    AIProposal,
    AnalysisArtifact,
    ArchiveBackground,
    ArchiveBackgroundLink,
    ArchiveBuildRun,
    ArchiveBuildStep,
    ArchiveOperatorContext,
    ArchiveRevision,
    ArchiveScope,
    ArchiveScopeDraft,
    ArchiveSearchPlan,
    ArchiveWork,
    BackgroundAIContribution,
    BackgroundNode,
    BackgroundOperatorContribution,
    BackgroundProfile,
    ConceptSet,
    ConceptTerm,
    DiscoveryEvent,
    EffectiveSearchPlan,
    GenerationRun,
    HumanDecision,
    IdentityEdge,
    MonitorRun,
    MonitorSubscription,
    OperatorLens,
    OperatorProfile,
    PendingIdentifierRelation,
    ReadingQueue,
    Record,
    RetrievalHit,
    RetrievalRun,
    ReviewItem,
    SearchStrategy,
    Source,
    SourceHealth,
    TopicArchive,
    UserWorkState,
    Work,
    WorkIdentifier,
    WorkMergeAudit,
    WorkStatus,
)
from ..llm.client import create_llm_client
from ..workflows.archive import (
    execute_search_strategy,
    generate_search_strategy,
    record_archive_revision,
    split_lines,
)
from ..workflows.archive_builder import (
    add_operator_contribution,
    create_archive_build_run,
)
from ..workflows.archive_discovery import run_archive_discovery
from ..workflows.archive_planning import (
    LLMArchivePlanner,
    RuleBasedArchivePlanner,
    ensure_default_operator_profile,
    run_archive_planning,
)
from ..workflows.monitor import run_monitor_subscription
from ..workflows.scheduler import MonitorScheduler

WEB_ROOT = Path(__file__).parent
templates = Jinja2Templates(directory=str(WEB_ROOT / "templates"))


def default_database_path() -> Path:
    config_path = Path("config.yaml")
    return (
        Path(load_config(config_path).database.path)
        if config_path.exists()
        else Path("data/literature.db")
    )


def benchmark_landmarks(topic: str) -> list[dict]:
    benchmark_root = Path("benchmarks/archive_cases")
    if not benchmark_root.exists():
        return []
    for path in benchmark_root.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("topic", "").casefold() == topic.casefold():
            return payload.get("known_landmark_works", [])
    return []


def create_app(
    database_path: str | Path | None = None,
    crossref_factory: Callable[[], CrossrefAdapter] | None = None,
    pubmed_factory: Callable[[], PubMedAdapter] | None = None,
    openalex_factory: Callable[[], OpenAlexAdapter] | None = None,
    llm_factory: Callable | None = None,
    archive_planner_factory: Callable | None = None,
    archive_ai_enabled: bool | None = None,
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
            if runtime_config
            else "local-monitor@example.invalid"
        )
        timeout = runtime_config.pubmed.timeout if runtime_config else 30.0
        api_key = runtime_config.pubmed.api_key if runtime_config else None
        return PubMedAdapter(email=email, api_key=api_key, timeout=timeout)

    def make_openalex() -> OpenAlexAdapter:
        if openalex_factory:
            return openalex_factory()
        email = runtime_config.crossref.email if runtime_config else None
        timeout = runtime_config.crossref.timeout if runtime_config else 30.0
        return OpenAlexAdapter(email=email, timeout=timeout)

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

    async def make_archive_planner():
        if archive_planner_factory:
            planner = archive_planner_factory()
            return await planner if inspect.isawaitable(planner) else planner
        use_ai = archive_ai_enabled
        if use_ai is None:
            use_ai = bool(
                database_path is None
                and runtime_config
                and runtime_config.llm.api_key
                and not runtime_config.llm.api_key.startswith("your-")
            )
        if use_ai and runtime_config:
            client = await create_llm_client(runtime_config.llm.model_dump(), model_tier="strong")
            return LLMArchivePlanner(client)
        return RuleBasedArchivePlanner()

    scheduler = MonitorScheduler(
        database,
        make_adapter,
        make_llm,
        default_interval_hours=(
            runtime_config.monitor.check_interval_hours if runtime_config else 24
        ),
    )
    should_schedule = (
        scheduler_enabled
        if scheduler_enabled is not None
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

    def visible_monitor_subscription_ids():
        return select(MonitorSubscription.id).where(
            MonitorSubscription.subscription_type != "archive_search"
        )

    async def work_view(work: Work, session, discovery: DiscoveryEvent | None = None) -> dict:
        preferred = (
            await session.get(Record, work.preferred_record_id)
            if work.preferred_record_id
            else None
        )
        if preferred is None:
            preferred = (
                await session.execute(select(Record).where(Record.work_id == work.id).limit(1))
            ).scalar_one_or_none()
        state = (
            await session.execute(select(UserWorkState).where(UserWorkState.work_id == work.id))
        ).scalar_one_or_none()
        queued = (
            await session.execute(
                select(ReadingQueue).where(
                    ReadingQueue.work_id == work.id,
                    ReadingQueue.requested_level == "L2",
                    ReadingQueue.status == "pending",
                )
            )
        ).scalar_one_or_none()
        artifact = None
        if preferred:
            artifact = (
                await session.execute(
                    select(AnalysisArtifact)
                    .where(
                        AnalysisArtifact.record_id == preferred.id,
                        AnalysisArtifact.analysis_type == "L1",
                    )
                    .order_by(AnalysisArtifact.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        subscription = (
            await session.get(MonitorSubscription, discovery.subscription_id) if discovery else None
        )
        source = await session.get(Source, discovery.source_id) if discovery else None
        return {
            "work": work,
            "record": preferred,
            "state": state,
            "queued": queued,
            "artifact": artifact,
            "discovery": discovery,
            "subscription": subscription,
            "source": source,
        }

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        async with database.get_session() as session:
            events = (
                (
                    await session.execute(
                        select(DiscoveryEvent).where(
                            DiscoveryEvent.subscription_id.in_(visible_monitor_subscription_ids())
                        )
                    )
                )
                .scalars()
                .all()
            )
            discovered_work_ids = {event.work_id for event in events if event.work_id is not None}
            works = [
                work
                for work_id in discovered_work_ids
                if (work := await session.get(Work, work_id))
                and work.status == WorkStatus.ACTIVE.value
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
            event_stmt = select(DiscoveryEvent).where(
                DiscoveryEvent.work_id.is_not(None),
                DiscoveryEvent.subscription_id.in_(visible_monitor_subscription_ids()),
            )
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
            events = (
                (await session.execute(event_stmt.order_by(DiscoveryEvent.discovered_at.desc())))
                .scalars()
                .all()
            )
            latest_by_work = {}
            for event in events:
                latest_by_work.setdefault(event.work_id, event)
            cards = []
            for work_id, event in latest_by_work.items():
                work = await session.get(Work, work_id)
                if work and work.status == WorkStatus.ACTIVE.value:
                    cards.append(await work_view(work, session, event))
            weekly_events = (
                (
                    await session.execute(
                        select(DiscoveryEvent)
                        .where(
                            DiscoveryEvent.work_id.is_not(None),
                            DiscoveryEvent.subscription_id.in_(visible_monitor_subscription_ids()),
                            DiscoveryEvent.discovered_at >= week_start,
                            DiscoveryEvent.discovered_at < week_end,
                        )
                        .order_by(DiscoveryEvent.discovered_at.desc())
                    )
                )
                .scalars()
                .all()
            )
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
            subscriptions = (
                (
                    await session.execute(
                        select(MonitorSubscription)
                        .where(MonitorSubscription.subscription_type != "archive_search")
                        .order_by(MonitorSubscription.created_at)
                    )
                )
                .scalars()
                .all()
            )
            sources = {
                source.id: source
                for source in (await session.execute(select(Source))).scalars().all()
            }
            health = {
                item.source_id: item
                for item in (await session.execute(select(SourceHealth))).scalars().all()
            }
            latest_runs = {}
            for run in (
                (await session.execute(select(MonitorRun).order_by(MonitorRun.started_at.desc())))
                .scalars()
                .all()
            ):
                latest_runs.setdefault(run.subscription_id, run)
            return templates.TemplateResponse(
                request,
                "inbox.html",
                {
                    "cards": cards,
                    "selected_state": state,
                    "notice": notice,
                    "period": period,
                    "selected_subscription": subscription_id,
                    "selected_source": source_id,
                    "subscriptions": subscriptions,
                    "sources": sources,
                    "health": health,
                    "run_discovered": discovered,
                    "latest_runs": latest_runs,
                    "run_updated": updated,
                    "run_has_more": bool(has_more),
                    "week_start": week_start,
                    "week_end": week_end - timedelta(days=1),
                    "weekly_stats": weekly_stats,
                    "scheduler_enabled": app.state.scheduler_enabled,
                    "scheduler_last_tick": app.state.monitor_scheduler.last_tick,
                },
            )

    @app.get("/works/{work_id}", response_class=HTMLResponse)
    async def paper_detail(request: Request, work_id: int, notice: str | None = None):
        async with database.get_session() as session:
            work = await session.get(Work, work_id)
            if work is None:
                raise HTTPException(404, "Work not found")
            data = await work_view(work, session)
            content = data["artifact"].content if data["artifact"] else {}
            return templates.TemplateResponse(
                request,
                "paper_detail.html",
                {
                    **data,
                    "content": content,
                    "notice": notice,
                },
            )

    @app.get("/works/{work_id}/debug", response_class=HTMLResponse)
    async def debug_work(request: Request, work_id: int):
        async with database.get_session() as session:
            work = await session.get(Work, work_id)
            if work is None:
                raise HTTPException(404, "Work not found")
            records = (
                (await session.execute(select(Record).where(Record.work_id == work.id)))
                .scalars()
                .all()
            )
            identifiers = (
                (
                    await session.execute(
                        select(WorkIdentifier).where(WorkIdentifier.work_id == work.id)
                    )
                )
                .scalars()
                .all()
            )
            edges = (
                (
                    await session.execute(
                        select(IdentityEdge).where(IdentityEdge.target_work_id == work.id)
                    )
                )
                .scalars()
                .all()
            )
            pending = (
                (
                    await session.execute(
                        select(PendingIdentifierRelation).where(
                            PendingIdentifierRelation.source_record_id.in_(
                                [item.id for item in records] or [-1]
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
            audits = (
                (
                    await session.execute(
                        select(WorkMergeAudit).where(
                            (WorkMergeAudit.merged_from_work_id == work.id)
                            | (WorkMergeAudit.merged_into_work_id == work.id)
                        )
                    )
                )
                .scalars()
                .all()
            )
            return templates.TemplateResponse(
                request,
                "debug.html",
                {
                    "work": work,
                    "records": records,
                    "identifiers": identifiers,
                    "edges": edges,
                    "pending": pending,
                    "audits": audits,
                },
            )

    @app.get("/archives", response_class=HTMLResponse)
    async def archives(request: Request, notice: str | None = None):
        async with database.get_session() as session:
            items = (
                (
                    await session.execute(
                        select(TopicArchive).order_by(TopicArchive.updated_at.desc())
                    )
                )
                .scalars()
                .all()
            )
            counts = {
                archive.id: (
                    await session.execute(
                        select(func.count(ArchiveWork.id)).where(
                            ArchiveWork.archive_id == archive.id
                        )
                    )
                ).scalar_one()
                for archive in items
            }
            runs = {
                archive.id: (
                    await session.execute(
                        select(ArchiveBuildRun)
                        .where(ArchiveBuildRun.archive_id == archive.id)
                        .order_by(ArchiveBuildRun.id.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                for archive in items
            }
            return templates.TemplateResponse(
                request,
                "archives.html",
                {
                    "archives": items,
                    "counts": counts,
                    "runs": runs,
                    "notice": notice,
                },
            )

    @app.get("/operator", response_class=HTMLResponse)
    async def operator_profile(request: Request, notice: str | None = None):
        async with database.get_session() as session:
            profile = await ensure_default_operator_profile(session)
            await session.commit()
            lenses = (
                (
                    await session.execute(
                        select(OperatorLens)
                        .where(
                            OperatorLens.profile_id == profile.id,
                            OperatorLens.status == "ACTIVE",
                        )
                        .order_by(OperatorLens.created_at)
                    )
                )
                .scalars()
                .all()
            )
            return templates.TemplateResponse(
                request,
                "operator.html",
                {
                    "profile": profile,
                    "lenses": lenses,
                    "notice": notice,
                },
            )

    @app.post("/operator")
    async def save_operator_profile(
        name: str = Form("默认研究者"),
        research_interests: str = Form(""),
        active_projects: str = Form(""),
        conceptual_preferences: str = Form(""),
        methodological_principles: str = Form(""),
        terminology_preferences: str = Form(""),
        note_conventions: str = Form(""),
    ):
        async with database.get_session() as session:
            current = await ensure_default_operator_profile(session)
            current_lenses = (
                (
                    await session.execute(
                        select(OperatorLens).where(
                            OperatorLens.profile_id == current.id,
                            OperatorLens.status == "ACTIVE",
                        )
                    )
                )
                .scalars()
                .all()
            )
            profile = OperatorProfile(
                profile_key=current.profile_key,
                name=name.strip() or current.name,
                research_interests=split_lines(research_interests),
                active_projects=split_lines(active_projects),
                conceptual_preferences=split_lines(conceptual_preferences),
                methodological_principles=split_lines(methodological_principles),
                terminology_preferences=split_lines(terminology_preferences),
                note_conventions=note_conventions.strip(),
                version=current.version + 1,
            )
            session.add(profile)
            await session.flush()
            session.add_all(
                [
                    OperatorLens(
                        profile_id=profile.id,
                        title=lens.title,
                        lens_type=lens.lens_type,
                        content=lens.content,
                        version=lens.version,
                        status=lens.status,
                    )
                    for lens in current_lenses
                ]
            )
            await session.commit()
        return RedirectResponse(url="/operator?notice=profile-saved", status_code=303)

    @app.post("/operator/lenses")
    async def add_operator_lens(
        title: str = Form(...),
        lens_type: str = Form("CONCEPTUAL"),
        content: str = Form(...),
    ):
        if lens_type not in {"CONCEPTUAL", "METHODOLOGICAL", "TERMINOLOGY", "HEURISTIC"}:
            raise HTTPException(422, "不支持的 Operator Lens 类型")
        if not title.strip() or not content.strip():
            raise HTTPException(422, "Lens 标题和内容不能为空")
        async with database.get_session() as session:
            profile = await ensure_default_operator_profile(session)
            session.add(
                OperatorLens(
                    profile_id=profile.id,
                    title=title.strip(),
                    lens_type=lens_type,
                    content=content.strip(),
                )
            )
            await session.commit()
        return RedirectResponse(url="/operator?notice=lens-added", status_code=303)

    @app.get("/archives/structure", response_class=HTMLResponse)
    async def archive_structure(request: Request):
        return RedirectResponse(url="/archives", status_code=307)

    @app.post("/archives")
    async def create_archive(
        title: str = Form(...),
        focus: str = Form(""),
        description: str = Form(""),
    ):
        title = title.strip()
        focus = (focus or description).strip()
        if not title:
            raise HTTPException(422, "专题名称不能为空")
        async with database.get_session() as session:
            if (
                await session.execute(select(TopicArchive).where(TopicArchive.title == title))
            ).scalar_one_or_none():
                raise HTTPException(409, "同名专题档案已存在")
            archive = TopicArchive(
                title=title,
                description=focus,
                focus=focus,
                background_mode="AUTO",
            )
            session.add(archive)
            await session.flush()
            await record_archive_revision(
                session,
                archive,
                "CREATE",
                "创建专题档案",
                {"topic": title, "focus": focus, "background_mode": "AUTO"},
            )
            run = await create_archive_build_run(session, archive)
            planner = await make_archive_planner()
            await run_archive_planning(session, archive, run, planner)
            await session.commit()
            archive_id = archive.id
        return RedirectResponse(url=f"/archives/{archive_id}?notice=created", status_code=303)

    async def archive_context(session, archive: TopicArchive) -> dict:
        scopes = (
            (
                await session.execute(
                    select(ArchiveScope)
                    .where(ArchiveScope.archive_id == archive.id)
                    .order_by(ArchiveScope.version.desc())
                )
            )
            .scalars()
            .all()
        )
        backgrounds = (
            (
                await session.execute(
                    select(ArchiveBackground)
                    .where(ArchiveBackground.archive_id == archive.id)
                    .order_by(ArchiveBackground.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        concept_sets = (
            (
                await session.execute(
                    select(ConceptSet)
                    .where(ConceptSet.archive_id == archive.id)
                    .order_by(ConceptSet.id)
                )
            )
            .scalars()
            .all()
        )
        terms = {
            item.id: (
                await session.execute(
                    select(ConceptTerm)
                    .where(ConceptTerm.concept_set_id == item.id)
                    .order_by(ConceptTerm.id)
                )
            )
            .scalars()
            .all()
            for item in concept_sets
        }
        strategies = (
            (
                await session.execute(
                    select(SearchStrategy)
                    .where(SearchStrategy.archive_id == archive.id)
                    .order_by(SearchStrategy.version.desc())
                )
            )
            .scalars()
            .all()
        )
        memberships = (
            (
                await session.execute(
                    select(ArchiveWork)
                    .where(ArchiveWork.archive_id == archive.id)
                    .order_by(ArchiveWork.added_at.desc())
                )
            )
            .scalars()
            .all()
        )
        cards = []
        for membership in memberships:
            work = await session.get(Work, membership.work_id)
            if work and work.status == WorkStatus.ACTIVE.value:
                card = await work_view(work, session)
                card["membership"] = membership
                cards.append(card)
        cards.sort(
            key=lambda card: (
                card["record"].publication_date
                if card["record"] and card["record"].publication_date
                else datetime.min
            ),
            reverse=True,
        )
        revisions = (
            (
                await session.execute(
                    select(ArchiveRevision)
                    .where(ArchiveRevision.archive_id == archive.id)
                    .order_by(ArchiveRevision.version.desc())
                )
            )
            .scalars()
            .all()
        )
        build_run = (
            await session.execute(
                select(ArchiveBuildRun)
                .where(ArchiveBuildRun.archive_id == archive.id)
                .order_by(ArchiveBuildRun.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        build_steps = []
        if build_run:
            all_steps = (
                (
                    await session.execute(
                        select(ArchiveBuildStep)
                        .where(ArchiveBuildStep.run_id == build_run.id)
                        .order_by(ArchiveBuildStep.id)
                    )
                )
                .scalars()
                .all()
            )
            latest_by_stage = {}
            for step in all_steps:
                latest_by_stage[step.stage] = step
            build_steps = list(latest_by_stage.values())
        links = (
            (
                await session.execute(
                    select(ArchiveBackgroundLink)
                    .where(
                        ArchiveBackgroundLink.archive_id == archive.id,
                        ArchiveBackgroundLink.status != "DISMISSED",
                    )
                    .order_by(ArchiveBackgroundLink.relevance.desc())
                )
            )
            .scalars()
            .all()
        )
        background_cards = []
        for link in links:
            node = await session.get(BackgroundNode, link.node_id)
            if node is None:
                continue
            profile = await session.get(BackgroundProfile, node.profile_id)
            operator_contributions = (
                (
                    await session.execute(
                        select(BackgroundOperatorContribution)
                        .where(
                            BackgroundOperatorContribution.archive_id == archive.id,
                            BackgroundOperatorContribution.node_id == node.id,
                        )
                        .order_by(BackgroundOperatorContribution.created_at)
                    )
                )
                .scalars()
                .all()
            )
            ai_contributions = (
                (
                    await session.execute(
                        select(BackgroundAIContribution)
                        .where(
                            BackgroundAIContribution.archive_id == archive.id,
                            BackgroundAIContribution.node_id == node.id,
                        )
                        .order_by(BackgroundAIContribution.created_at)
                    )
                )
                .scalars()
                .all()
            )
            background_cards.append(
                {
                    "link": link,
                    "node": node,
                    "profile": profile,
                    "operator_contributions": operator_contributions,
                    "ai_contributions": ai_contributions,
                }
            )
        scope_draft = (
            await session.execute(
                select(ArchiveScopeDraft)
                .where(ArchiveScopeDraft.archive_id == archive.id)
                .order_by(ArchiveScopeDraft.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        search_plan = (
            await session.execute(
                select(ArchiveSearchPlan)
                .where(ArchiveSearchPlan.archive_id == archive.id)
                .order_by(ArchiveSearchPlan.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        review_items = (
            (
                await session.execute(
                    select(ReviewItem)
                    .where(ReviewItem.archive_id == archive.id)
                    .order_by((ReviewItem.status == "PENDING").desc(), ReviewItem.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        proposals = {}
        decisions = {}
        for item in review_items:
            proposals[item.id] = await session.get(AIProposal, item.proposal_id)
            decisions[item.id] = (
                await session.execute(
                    select(HumanDecision)
                    .where(HumanDecision.review_item_id == item.id)
                    .order_by(HumanDecision.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        operator_context = (
            await session.execute(
                select(ArchiveOperatorContext).where(
                    ArchiveOperatorContext.archive_id == archive.id
                )
            )
        ).scalar_one_or_none()
        operator_profile = (
            await session.get(OperatorProfile, operator_context.profile_id)
            if operator_context
            else None
        )
        operator_lenses = []
        if operator_context and operator_context.selected_lens_ids:
            operator_lenses = list(
                (
                    await session.execute(
                        select(OperatorLens).where(
                            OperatorLens.id.in_(operator_context.selected_lens_ids)
                        )
                    )
                )
                .scalars()
                .all()
            )
        generation_runs = (
            (
                await session.execute(
                    select(GenerationRun)
                    .where(GenerationRun.archive_id == archive.id)
                    .order_by(GenerationRun.started_at.desc())
                )
            )
            .scalars()
            .all()
        )
        effective_plan = (
            await session.execute(
                select(EffectiveSearchPlan)
                .where(EffectiveSearchPlan.archive_id == archive.id)
                .order_by(EffectiveSearchPlan.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        retrieval_runs = list(
            (
                await session.execute(
                    select(RetrievalRun)
                    .where(RetrievalRun.archive_id == archive.id)
                    .order_by(RetrievalRun.started_at.desc())
                )
            )
            .scalars()
            .all()
        )
        retrieval_hit_count = (
            await session.execute(
                select(func.count(RetrievalHit.id)).where(RetrievalHit.archive_id == archive.id)
            )
        ).scalar_one()
        return {
            "archive": archive,
            "scopes": scopes,
            "scope": scopes[0] if scopes else None,
            "backgrounds": backgrounds,
            "concept_sets": concept_sets,
            "terms": terms,
            "strategies": strategies,
            "strategy": strategies[0] if strategies else None,
            "cards": cards,
            "revisions": revisions,
            "build_run": build_run,
            "build_steps": build_steps,
            "background_cards": background_cards,
            "attached_backgrounds": [
                item for item in background_cards if item["link"].status == "ATTACHED"
            ],
            "background_candidates": [
                item for item in background_cards if item["link"].status == "CANDIDATE"
            ],
            "scope_draft": scope_draft,
            "search_plan": search_plan,
            "review_items": review_items,
            "proposals": proposals,
            "decisions": decisions,
            "operator_context": operator_context,
            "operator_profile": operator_profile,
            "operator_lenses": operator_lenses,
            "generation_runs": generation_runs,
            "effective_plan": effective_plan,
            "retrieval_runs": retrieval_runs,
            "retrieval_run": retrieval_runs[0] if retrieval_runs else None,
            "retrieval_hit_count": retrieval_hit_count,
        }

    @app.get("/archives/{archive_id}", response_class=HTMLResponse)
    async def archive_detail(
        request: Request,
        archive_id: int,
        notice: str | None = None,
        discovered: int | None = None,
        added: int | None = None,
    ):
        async with database.get_session() as session:
            archive = await session.get(TopicArchive, archive_id)
            if archive is None:
                raise HTTPException(404, "专题档案不存在")
            context = await archive_context(session, archive)
            return templates.TemplateResponse(
                request,
                "archive_detail.html",
                {
                    **context,
                    "notice": notice,
                    "search_discovered": discovered,
                    "search_added": added,
                },
            )

    @app.post("/archives/{archive_id}/build")
    async def build_archive(archive_id: int):
        async with database.get_session() as session:
            archive = await session.get(TopicArchive, archive_id)
            if archive is None:
                raise HTTPException(404, "专题档案不存在")
            run = await create_archive_build_run(session, archive)
            planner = await make_archive_planner()
            await run_archive_planning(session, archive, run, planner)
            await session.commit()
        notice = "build-failed" if run.status == "FAILED" else "build-completed"
        return RedirectResponse(url=f"/archives/{archive_id}?notice={notice}", status_code=303)

    @app.post("/archives/{archive_id}/build/{run_id}/resume")
    async def resume_archive_build(archive_id: int, run_id: int):
        async with database.get_session() as session:
            archive = await session.get(TopicArchive, archive_id)
            run = await session.get(ArchiveBuildRun, run_id)
            if archive is None or run is None or run.archive_id != archive.id:
                raise HTTPException(404, "Archive Build Run 不存在")
            planner = await make_archive_planner()
            await run_archive_planning(session, archive, run, planner)
            await session.commit()
        notice = "build-failed" if run.status == "FAILED" else "build-resumed"
        return RedirectResponse(url=f"/archives/{archive_id}?notice={notice}", status_code=303)

    @app.post("/archives/{archive_id}/discover")
    async def discover_archive(
        archive_id: int,
        max_results_per_query: int = Form(50),
    ):
        max_results_per_query = max(10, min(max_results_per_query, 200))
        async with database.get_session() as session:
            archive = await session.get(TopicArchive, archive_id)
            if archive is None:
                raise HTTPException(404, "专题档案不存在")
            build_run = (
                await session.execute(
                    select(ArchiveBuildRun)
                    .where(ArchiveBuildRun.archive_id == archive.id)
                    .order_by(ArchiveBuildRun.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if build_run is None:
                raise HTTPException(409, "请先完成 Archive Planning")
            retrieval = await run_archive_discovery(
                session,
                archive,
                build_run,
                make_pubmed,
                make_openalex,
                max_results_per_query=max_results_per_query,
                known_landmarks=benchmark_landmarks(archive.title),
            )
            await session.commit()
        notice = "discovery-failed" if retrieval.status == "FAILED" else "discovery-completed"
        return RedirectResponse(url=f"/archives/{archive_id}?notice={notice}", status_code=303)

    @app.post("/archives/{archive_id}/background-links/{link_id}/decision")
    async def decide_background_link(
        archive_id: int,
        link_id: int,
        decision: str = Form(...),
    ):
        if decision not in {"ATTACHED", "DISMISSED"}:
            raise HTTPException(422, "不支持的 Background 决策")
        async with database.get_session() as session:
            archive = await session.get(TopicArchive, archive_id)
            link = await session.get(ArchiveBackgroundLink, link_id)
            if archive is None or link is None or link.archive_id != archive.id:
                raise HTTPException(404, "Background Link 不存在")
            link.status = decision
            link.selected_by = "OPERATOR"
            node = await session.get(BackgroundNode, link.node_id)
            await record_archive_revision(
                session,
                archive,
                "BACKGROUND_REVIEW",
                f"Background {decision.lower()}：{node.title if node else link.node_id}",
                {"link_id": link.id, "decision": decision},
            )
            await session.commit()
        return RedirectResponse(
            url=f"/archives/{archive_id}?notice=background-reviewed", status_code=303
        )

    @app.post("/archives/{archive_id}/background-links/{link_id}/operator-notes")
    async def add_background_operator_note(
        archive_id: int,
        link_id: int,
        contribution_type: str = Form(...),
        raw_text: str = Form(...),
    ):
        async with database.get_session() as session:
            archive = await session.get(TopicArchive, archive_id)
            link = await session.get(ArchiveBackgroundLink, link_id)
            if archive is None or link is None or link.archive_id != archive.id:
                raise HTTPException(404, "Background Link 不存在")
            try:
                contribution = await add_operator_contribution(
                    session,
                    archive_id=archive.id,
                    node_id=link.node_id,
                    contribution_type=contribution_type,
                    raw_text=raw_text,
                )
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            await record_archive_revision(
                session,
                archive,
                "OPERATOR_BACKGROUND_NOTE",
                f"保存操作者原始想法：{contribution.contribution_type}",
                {"contribution_id": contribution.id, "node_id": link.node_id},
            )
            await session.commit()
        return RedirectResponse(
            url=f"/archives/{archive_id}?notice=operator-note-added", status_code=303
        )

    @app.post("/archives/{archive_id}/review-items/{item_id}/decide")
    async def decide_archive_review_item(
        archive_id: int,
        item_id: int,
        decision: str = Form(...),
        rationale: str = Form(""),
    ):
        async with database.get_session() as session:
            archive = await session.get(TopicArchive, archive_id)
            item = await session.get(ReviewItem, item_id)
            if archive is None or item is None or item.archive_id != archive.id:
                raise HTTPException(404, "Review Item 不存在")
            if item.status != "PENDING":
                raise HTTPException(409, "该 Review Item 已处理")
            allowed = {option["value"] for option in item.options}
            if decision not in allowed:
                raise HTTPException(422, "不支持的 Review 决策")
            proposal = await session.get(AIProposal, item.proposal_id)
            human_decision = HumanDecision(
                archive_id=archive.id,
                review_item_id=item.id,
                proposal_id=item.proposal_id,
                decision=decision,
                rationale=rationale.strip(),
                reviewer_metadata={"actor": "local_operator", "interface": "archive_ui"},
            )
            session.add(human_decision)
            item.status = "RESOLVED"
            item.resolved_at = datetime.utcnow()
            if proposal:
                proposal.status = "OPERATOR_RESOLVED"
            await session.flush()
            await record_archive_revision(
                session,
                archive,
                "HUMAN_DECISION",
                f"人工处理 Scope 歧义：{decision}",
                {
                    "review_item_id": item.id,
                    "proposal_id": item.proposal_id,
                    "human_decision_id": human_decision.id,
                    "decision": decision,
                },
            )
            await session.commit()
        return RedirectResponse(
            url=f"/archives/{archive_id}?notice=review-decided", status_code=303
        )

    @app.post("/archives/{archive_id}/scope")
    async def save_archive_scope(
        archive_id: int,
        core_concepts: str = Form(...),
        background_concepts: str = Form(""),
        exclusions: str = Form(""),
        notes: str = Form(""),
    ):
        core = split_lines(core_concepts)
        if not core:
            raise HTTPException(422, "Scope 至少需要一个核心概念")
        async with database.get_session() as session:
            archive = await session.get(TopicArchive, archive_id)
            if archive is None:
                raise HTTPException(404, "专题档案不存在")
            version = (
                await session.execute(
                    select(func.max(ArchiveScope.version)).where(
                        ArchiveScope.archive_id == archive.id
                    )
                )
            ).scalar_one() or 0
            scope = ArchiveScope(
                archive_id=archive.id,
                version=version + 1,
                core_concepts=core,
                background_concepts=split_lines(background_concepts),
                exclusions=split_lines(exclusions),
                notes=notes.strip(),
            )
            session.add(scope)
            await record_archive_revision(
                session,
                archive,
                "SCOPE",
                f"保存 Scope v{scope.version}",
                {
                    "scope_version": scope.version,
                    "core": core,
                    "background": scope.background_concepts,
                    "exclude": scope.exclusions,
                },
            )
            await session.commit()
        return RedirectResponse(url=f"/archives/{archive_id}?notice=scope-saved", status_code=303)

    @app.post("/archives/{archive_id}/background")
    async def add_archive_background(
        archive_id: int,
        title: str = Form(...),
        content: str = Form(...),
        source_url: str = Form(""),
    ):
        if not title.strip() or not content.strip():
            raise HTTPException(422, "Background 标题和内容不能为空")
        async with database.get_session() as session:
            archive = await session.get(TopicArchive, archive_id)
            if archive is None:
                raise HTTPException(404, "专题档案不存在")
            background = ArchiveBackground(
                archive_id=archive.id,
                title=title.strip(),
                content=content.strip(),
                source_url=source_url.strip() or None,
            )
            session.add(background)
            await session.flush()
            await record_archive_revision(
                session,
                archive,
                "BACKGROUND",
                f"挂接 Background：{background.title}",
                {"background_id": background.id, "title": background.title},
            )
            await session.commit()
        return RedirectResponse(
            url=f"/archives/{archive_id}?notice=background-added", status_code=303
        )

    @app.post("/archives/{archive_id}/concept-sets")
    async def add_concept_set(
        archive_id: int,
        name: str = Form(...),
        terms_text: str = Form(...),
        description: str = Form(""),
        source: str = Form("manual"),
    ):
        if source not in {"manual", "mesh", "llm"}:
            raise HTTPException(422, "不支持的术语来源")
        parsed_terms = []
        for raw in terms_text.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            status = (
                "ambiguous"
                if raw.startswith("?")
                else "exclude"
                if raw.startswith("-")
                else "include"
            )
            term = raw[1:].strip() if raw[:1] in {"+", "?", "-"} else raw
            if term:
                parsed_terms.append((term, status))
        if not name.strip() or not parsed_terms:
            raise HTTPException(422, "Concept Set 需要名称和至少一个术语")
        async with database.get_session() as session:
            archive = await session.get(TopicArchive, archive_id)
            if archive is None:
                raise HTTPException(404, "专题档案不存在")
            if (
                await session.execute(
                    select(ConceptSet).where(
                        ConceptSet.archive_id == archive.id, ConceptSet.name == name.strip()
                    )
                )
            ).scalar_one_or_none():
                raise HTTPException(409, "同名 Concept Set 已存在")
            concept_set = ConceptSet(
                archive_id=archive.id,
                name=name.strip(),
                description=description.strip(),
            )
            session.add(concept_set)
            await session.flush()
            for term, status in parsed_terms:
                session.add(
                    ConceptTerm(
                        concept_set_id=concept_set.id,
                        term=term,
                        status=status,
                        source=source,
                    )
                )
            await record_archive_revision(
                session,
                archive,
                "LEXICON",
                f"新增 Concept Set：{concept_set.name}",
                {"concept_set_id": concept_set.id, "terms": parsed_terms},
            )
            await session.commit()
        return RedirectResponse(url=f"/archives/{archive_id}?notice=concept-added", status_code=303)

    @app.post("/archives/{archive_id}/search-strategies")
    async def build_search_strategy(archive_id: int):
        async with database.get_session() as session:
            archive = await session.get(TopicArchive, archive_id)
            if archive is None:
                raise HTTPException(404, "专题档案不存在")
            try:
                await generate_search_strategy(session, archive)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            await session.commit()
        return RedirectResponse(
            url=f"/archives/{archive_id}?notice=strategy-generated", status_code=303
        )

    @app.post("/archives/{archive_id}/terms/{term_id}/status")
    async def update_concept_term_status(
        archive_id: int,
        term_id: int,
        status: str = Form(...),
    ):
        if status not in {"include", "ambiguous", "exclude"}:
            raise HTTPException(422, "术语状态不受支持")
        async with database.get_session() as session:
            archive = await session.get(TopicArchive, archive_id)
            term = await session.get(ConceptTerm, term_id)
            concept_set = await session.get(ConceptSet, term.concept_set_id) if term else None
            if (
                archive is None
                or term is None
                or concept_set is None
                or concept_set.archive_id != archive.id
            ):
                raise HTTPException(404, "Concept Term 不存在")
            term.status = status
            await record_archive_revision(
                session,
                archive,
                "LEXICON",
                f"更新术语状态：{term.term} → {status}",
                {"term_id": term.id, "term": term.term, "status": status},
            )
            await session.commit()
        return RedirectResponse(url=f"/archives/{archive_id}?notice=term-updated", status_code=303)

    @app.post("/archives/{archive_id}/search-strategies/{strategy_id}/execute")
    async def execute_archive_search(archive_id: int, strategy_id: int):
        async with database.get_session() as session:
            archive = await session.get(TopicArchive, archive_id)
            strategy = await session.get(SearchStrategy, strategy_id)
            if archive is None or strategy is None or strategy.archive_id != archive.id:
                raise HTTPException(404, "Search Strategy 不存在")
            try:
                result = await execute_search_strategy(
                    session,
                    archive,
                    strategy,
                    make_pubmed,
                    max_results_per_query=50,
                )
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            except RuntimeError as exc:
                await session.rollback()
                raise HTTPException(502, str(exc)) from exc
            await session.commit()
        return RedirectResponse(
            url=(
                f"/archives/{archive_id}?notice=search-completed"
                f"&discovered={result['discovered']}&added={result['added']}"
            ),
            status_code=303,
        )

    @app.post("/works/{work_id}/user-state")
    async def set_user_state(work_id: int, state: str = Form(...)):
        if state not in {"keep", "ignore"}:
            raise HTTPException(422, "state must be keep or ignore")
        async with database.get_session() as session:
            if await session.get(Work, work_id) is None:
                raise HTTPException(404, "Work not found")
            user_state = (
                await session.execute(select(UserWorkState).where(UserWorkState.work_id == work_id))
            ).scalar_one_or_none()
            if user_state:
                user_state.state = state
                user_state.match_reason = {"source": "user_resolution"}
            else:
                session.add(
                    UserWorkState(work_id=work_id, state=state, match_reason={"source": "UI-0"})
                )
            await session.commit()
        return RedirectResponse(url=f"/monitor?notice={state}", status_code=303)

    @app.post("/works/{work_id}/reading-queue")
    async def queue_l2(work_id: int):
        async with database.get_session() as session:
            if await session.get(Work, work_id) is None:
                raise HTTPException(404, "Work not found")
            existing = (
                await session.execute(
                    select(ReadingQueue).where(
                        ReadingQueue.work_id == work_id,
                        ReadingQueue.requested_level == "L2",
                        ReadingQueue.status == "pending",
                    )
                )
            ).scalar_one_or_none()
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
            existing = (
                await session.execute(
                    select(MonitorSubscription).where(MonitorSubscription.name == name.strip())
                )
            ).scalar_one_or_none()
            if existing:
                raise HTTPException(409, "订阅名称已存在")
            source = (
                await session.execute(select(Source).where(Source.name == provider))
            ).scalar_one_or_none()
            if source is None:
                source = Source(name=provider, source_type="api", config={})
                session.add(source)
                await session.flush()
            config = {
                "lookback_days": 7,
                "feed_mode": feed_mode,
                "page_size": 100,
                "max_items_per_run": 500,
                "max_pages_per_run": 5,
                "interval_hours": interval_hours,
            }
            if issn.strip():
                config["issn"] = issn.strip()
            if query.strip():
                config["query"] = query.strip()
            session.add(
                MonitorSubscription(
                    name=name.strip(),
                    subscription_type=subscription_type,
                    source_id=source.id,
                    config=config,
                )
            )
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
        return RedirectResponse(
            url=(
                f"/monitor?notice=checked&discovered={result.discovered}"
                f"&updated={result.updated}&has_more={int(result.has_more)}"
            ),
            status_code=303,
        )

    @app.get("/api/inbox")
    async def api_inbox():
        async with database.get_session() as session:
            events = (
                (
                    await session.execute(
                        select(DiscoveryEvent)
                        .where(
                            DiscoveryEvent.work_id.is_not(None),
                            DiscoveryEvent.subscription_id.in_(visible_monitor_subscription_ids()),
                        )
                        .order_by(DiscoveryEvent.discovered_at.desc())
                    )
                )
                .scalars()
                .all()
            )
            seen, response = set(), []
            for event in events:
                if event.work_id in seen:
                    continue
                seen.add(event.work_id)
                work = await session.get(Work, event.work_id)
                if work and work.status == WorkStatus.ACTIVE.value:
                    response.append(
                        {
                            "id": work.id,
                            "work_id": work.work_id,
                            "title": work.title,
                            "doi": work.canonical_doi,
                            "discovered_at": event.discovered_at,
                            "subscription_id": event.subscription_id,
                        }
                    )
            return response

    return app


app = create_app()
