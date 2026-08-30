"""Topic Archive v0.1 vertical product flow."""

import asyncio
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from src.core.database import Database
from src.core.models import (
    ArchiveRevision, ArchiveScope, ArchiveWork, ConceptSet, DiscoveryEvent,
    MonitorSubscription, Record, SearchStrategy, TopicArchive, Work,
)
from src.core.work_identity import WorkIdentityResolver
from src.web.app import create_app


PUBMED_ARCHIVE_ITEM = {
    "PMID": "49990001", "DOI": "10.1000/archive-space",
    "title": "Navigating molecular chemical space",
    "authors": [{"name": "Archive Author", "affiliation": [], "orcid": None}],
    "journal": "Journal of Archive Tests",
    "date": {"year": "2024", "month": "May", "day": "12"},
    "abstract": "Molecular representations support navigation through chemical space.",
    "pmcid": None, "mesh": ["Molecular Structure"],
}


class FakeArchivePubMed:
    async def discover_works(self, **kwargs):
        return [PUBMED_ARCHIVE_ITEM], None

    async def close(self):
        return None


def test_archive_scope_lexicon_search_work_and_timeline(tmp_path):
    app = create_app(
        tmp_path / "archive.db",
        pubmed_factory=FakeArchivePubMed,
        scheduler_enabled=False,
    )
    with TestClient(app) as client:
        created = client.post("/archives", data={
            "title": "Chemical Space",
            "description": "How molecular spaces are represented and navigated.",
        })
        assert created.status_code == 200
        assert "Chemical Space" in created.text
        assert "档案初稿已建立" in created.text
        assert "Advanced / Expert Mode" in created.text

        scope = client.post("/archives/1/scope", data={
            "core_concepts": "chemical space\nmolecular space",
            "background_concepts": "descriptors\nQSAR",
            "exclusions": "astronomical chemical composition",
            "notes": "Molecular design scope only.",
        })
        assert "Scope 新版本已保存" in scope.text
        client.post("/archives/1/scope", data={
            "core_concepts": "chemical space\nmolecular space\nchemical universe",
            "background_concepts": "descriptors",
            "exclusions": "astronomical chemical composition",
            "notes": "Expanded synonym boundary.",
        })
        background = client.post("/archives/1/background", data={
            "title": "Chemical representation foundations",
            "content": "Descriptors and fingerprints provide stable background.",
            "source_url": "https://example.org/background",
        })
        assert "Background 已挂接" in background.text
        client.post("/archives/1/concept-sets", data={
            "name": "Chemical Space", "description": "Anchor concept",
            "terms_text": "chemical space\nmolecular space\n? chemical universe",
            "source": "manual",
        })
        lexicon = client.post("/archives/1/concept-sets", data={
            "name": "Representation", "description": "Representation methods",
            "terms_text": "molecular representation\ndescriptor\n- astronomical composition",
            "source": "mesh",
        })
        assert "Concept Set 已加入" in lexicon.text
        term_update = client.post("/archives/1/terms/3/status", data={"status": "include"})
        assert "术语状态已更新" in term_update.text
        strategy_page = client.post("/archives/1/search-strategies")
        assert "Search Strategy" in strategy_page.text
        assert "Q1" in strategy_page.text and "Q2" in strategy_page.text
        assert "chemical space" in strategy_page.text
        assert "chemical universe" in strategy_page.text
        executed = client.post("/archives/1/search-strategies/1/execute")
        assert executed.status_code == 200
        assert "去重后新增 1" in executed.text
        assert "Navigating molecular chemical space" in executed.text
        assert "2024-05-12" in executed.text
        listing = client.get("/archives")
        assert "1</strong><span>Works" in listing.text
        monitor = client.get("/monitor?period=all")
        assert "Navigating molecular chemical space" not in monitor.text
        assert "archive-1-s1-Q1" not in monitor.text

    async def verify():
        async with app.state.database.get_session() as session:
            archive = (await session.execute(select(TopicArchive))).scalar_one()
            assert archive.revision == 11
            assert (await session.execute(select(func.count(ArchiveScope.id)))).scalar_one() == 3
            assert (await session.execute(select(func.count(ConceptSet.id)))).scalar_one() == 2
            strategy = (await session.execute(select(SearchStrategy))).scalar_one()
            assert len(strategy.queries) == 2 and strategy.executed_at is not None
            membership = (await session.execute(select(ArchiveWork))).scalar_one()
            assert membership.matched_queries == ["Q1", "Q2"]
            assert (await session.execute(select(func.count(Work.id)))).scalar_one() == 1
            assert (await session.execute(select(func.count(Record.id)))).scalar_one() == 1
            assert (await session.execute(select(func.count(DiscoveryEvent.id)))).scalar_one() == 2
            hidden = (await session.execute(select(MonitorSubscription))).scalars().all()
            assert len(hidden) == 2
            assert all(item.subscription_type == "archive_search" and not item.enabled for item in hidden)
            assert (await session.execute(select(func.count(ArchiveRevision.id)))).scalar_one() == 11

    asyncio.run(verify())


async def test_work_merge_preserves_one_archive_membership(tmp_path):
    database = Database(tmp_path / "archive-merge.db")
    await database.init_db()
    async with database.get_session() as session:
        archive = TopicArchive(title="Merge-safe archive")
        keep = Work(work_id="W-archive-keep", title="Canonical")
        merge = Work(work_id="W-archive-merge", title="Duplicate")
        session.add_all([archive, keep, merge])
        await session.flush()
        session.add_all([
            ArchiveWork(
                archive_id=archive.id, work_id=keep.id,
                matched_queries=["Q1"], added_at=datetime(2026, 1, 2),
            ),
            ArchiveWork(
                archive_id=archive.id, work_id=merge.id,
                matched_queries=["Q2"], added_at=datetime(2026, 1, 1),
            ),
        ])
        await session.flush()
        await WorkIdentityResolver(session).merge_work(
            keep, merge, reason="archive test", evidence={"source": "test"}, confirmed=True,
        )
        memberships = (await session.execute(select(ArchiveWork))).scalars().all()
        assert len(memberships) == 1
        assert memberships[0].work_id == keep.id
        assert memberships[0].matched_queries == ["Q1", "Q2"]
        assert memberships[0].added_at == datetime(2026, 1, 1)
    await database.close()
