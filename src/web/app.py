"""FastAPI application for the deliberately small UI-0 vertical slice."""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from ..core.config import load_config
from ..core.database import Database
from ..core.models import (
    AnalysisArtifact, IdentityEdge, PendingIdentifierRelation, ReadingQueue,
    Record, UserWorkState, Work, WorkIdentifier, WorkMergeAudit, WorkStatus,
)

WEB_ROOT = Path(__file__).parent
templates = Jinja2Templates(directory=str(WEB_ROOT / "templates"))


def default_database_path() -> Path:
    config_path = Path("config.yaml")
    return Path(load_config(config_path).database.path) if config_path.exists() else Path("data/literature.db")


def create_app(database_path: str | Path | None = None) -> FastAPI:
    database = Database(database_path or default_database_path())

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await database.init_db()
        yield
        await database.close()

    app = FastAPI(title="Literature Surveillance UI-0", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(WEB_ROOT / "static")), name="static")
    app.state.database = database

    async def work_view(work: Work, session) -> dict:
        preferred = await session.get(Record, work.preferred_record_id) if work.preferred_record_id else None
        if preferred is None:
            preferred = (await session.execute(select(Record).where(Record.work_id == work.id).limit(1))).scalar_one_or_none()
        state = (await session.execute(select(UserWorkState).where(UserWorkState.work_id == work.id))).scalar_one_or_none()
        artifact = None
        if preferred:
            artifact = (await session.execute(select(AnalysisArtifact).where(
                AnalysisArtifact.record_id == preferred.id, AnalysisArtifact.analysis_type == "L1"
            ).order_by(AnalysisArtifact.created_at.desc()).limit(1))).scalar_one_or_none()
        return {"work": work, "record": preferred, "state": state, "artifact": artifact}

    @app.get("/", response_class=HTMLResponse)
    async def inbox(request: Request, state: str | None = None):
        async with database.get_session() as session:
            stmt = select(Work).where(Work.status == WorkStatus.ACTIVE.value).order_by(Work.updated_at.desc())
            works = (await session.execute(stmt)).scalars().all()
            cards = [await work_view(work, session) for work in works]
            if state:
                cards = [card for card in cards if card["state"] and card["state"].state == state]
            return templates.TemplateResponse(request, "inbox.html", {"cards": cards, "selected_state": state})

    @app.get("/works/{work_id}", response_class=HTMLResponse)
    async def paper_detail(request: Request, work_id: int):
        async with database.get_session() as session:
            work = await session.get(Work, work_id)
            if work is None:
                raise HTTPException(404, "Work not found")
            data = await work_view(work, session)
            content = data["artifact"].content if data["artifact"] else {}
            return templates.TemplateResponse(request, "paper_detail.html", {**data, "content": content})

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
            else:
                session.add(UserWorkState(work_id=work_id, state=state, match_reason={"source": "UI-0"}))
            await session.commit()
        return RedirectResponse(url="/", status_code=303)

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
        return RedirectResponse(url=f"/works/{work_id}", status_code=303)

    @app.get("/api/inbox")
    async def api_inbox():
        async with database.get_session() as session:
            works = (await session.execute(select(Work).where(Work.status == WorkStatus.ACTIVE.value))).scalars().all()
            return [{"id": work.id, "work_id": work.work_id, "title": work.title, "doi": work.canonical_doi} for work in works]

    return app


app = create_app()
