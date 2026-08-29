"""UI-0 smoke tests: render real persisted Work data and capture actions."""

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from src.core.models import (
    AnalysisArtifact, DiscoveryEvent, MonitorSubscription, Record, Source,
    SourceSnapshot, Work,
)
from src.web.app import create_app


def test_ui0_inbox_detail_and_actions(tmp_path: Path):
    db_path = tmp_path / "ui.db"
    app = create_app(db_path)

    async def seed():
        await app.state.database.init_db()
        async with app.state.database.get_session() as session:
            source = Source(name="crossref", source_type="api", config={})
            session.add(source)
            await session.flush()
            subscription = MonitorSubscription(
                name="Test Monitor", subscription_type="journal", source_id=source.id,
                config={"issn": "1234-5678"},
            )
            session.add(subscription)
            await session.flush()
            work = Work(work_id="W-ui-test", title="A real literature card")
            session.add(work)
            await session.flush()
            record = Record(
                record_id="R-ui-test", work_id=work.id, title=work.title,
                authors=[{"name": "Smith J"}], doi="10.1/ui", abstract="A useful abstract.",
                evidence_level="E1", journal="Journal of Testing",
            )
            session.add(record)
            await session.flush()
            work.preferred_record_id = record.id
            snapshot = SourceSnapshot(record_id=record.id, source_name="test")
            session.add(snapshot)
            await session.flush()
            session.add(AnalysisArtifact(
                artifact_id="artifact-ui-test", record_id=record.id, snapshot_id=snapshot.id,
                analysis_type="L1", skill_version="test", schema_version="1",
                content={
                    "one_sentence": "A concise summary.", "tags": ["test", "ui", "l1"],
                    "research_object": "UI", "major_method": "Testing",
                    "author_reported_result": "It works.",
                    "evidence_spans": {"research_object": "A useful abstract."},
                },
            ))
            session.add(DiscoveryEvent(
                source_id=source.id, subscription_id=subscription.id,
                external_identifier="10.1/ui", work_id=work.id,
                source_url="https://doi.org/10.1/ui", raw_metadata={}, status="L1_READY",
            ))
            await session.commit()

    asyncio.run(seed())
    with TestClient(app) as client:
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "学术专题档案" in dashboard.text
        assert "定期文献更新" in dashboard.text
        inbox = client.get("/monitor")
        assert inbox.status_code == 200
        assert "A real literature card" in inbox.text
        detail = client.get("/works/1")
        assert detail.status_code == 200
        assert "A concise summary." in detail.text
        assert client.post("/works/1/user-state", data={"state": "keep"}).status_code == 200
        assert client.post("/works/1/reading-queue").status_code == 200
        assert client.get("/api/inbox").json()[0]["work_id"] == "W-ui-test"
