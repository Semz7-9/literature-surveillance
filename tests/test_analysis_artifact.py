"""
AnalysisArtifact 持久化的 regression test（P0 item 3 + run_l1 完整性检查）

不调用真实 LLM——persist_l1_artifact 直接接受手工构造的 L1Output；
run_l1 的完整性检查在真正调用 llm_client 之前就应该抛错，所以传 None
进去验证检查顺序是安全的。

沿用 test_identity_resolution.py 里的 db fixture 模式。
"""

from pathlib import Path

import pytest
from sqlalchemy import select

from src.core.database import Database
from src.core.models import Record, AnalysisArtifact
from src.core.artifact import create_source_snapshot
from src.workflows.l1_generator import persist_l1_artifact, run_l1
from skills.l1_literature_card.contract import L1Output


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test_artifact.db")
    await database.init_db()
    yield database
    await database.close()


def make_record(doi: str, abstract: str = "KRAS mutations drive cancer.") -> Record:
    return Record(
        record_id=f"R_{doi.replace('/', '_')}",
        work_id=1,
        title="Some Paper",
        authors=[{"name": "Smith J"}],
        doi=doi,
        abstract=abstract,
        evidence_level="E1",
    )


def make_output(**overrides) -> L1Output:
    data = dict(
        one_sentence="A study of KRAS mutations in cancer.",
        tags=["KRAS", "cancer", "mutation"],
        research_object="KRAS",
        research_object_missing_reason=None,
        major_method=None,
        major_method_missing_reason="NOT_STATED",
        author_reported_result=None,
        author_reported_result_missing_reason="NOT_STATED",
        evidence_spans={"research_object": "KRAS mutations drive cancer."},
    )
    data.update(overrides)
    return L1Output(**data)


# ---------------------------------------------------------------------------
# Idempotency: same record/snapshot/output -> same artifact row
# ---------------------------------------------------------------------------


async def test_persist_l1_artifact_is_idempotent(db: Database):
    async with db.get_session() as session:
        record = make_record("10.1/idem")
        session.add(record)
        await session.flush()
        snapshot = await create_source_snapshot(session, record, "crossref")

        output = make_output()
        artifact1 = await persist_l1_artifact(session, record, snapshot, output)
        artifact2 = await persist_l1_artifact(session, record, snapshot, output)

        assert artifact1.id == artifact2.id

        count_stmt_result = await session.execute(
            select(AnalysisArtifact).where(AnalysisArtifact.record_id == record.id)
        )
        rows = count_stmt_result.scalars().all()
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Version bump -> new artifact, linked via supersedes_id
# ---------------------------------------------------------------------------


async def test_persist_l1_artifact_versions_on_skill_bump(db: Database, monkeypatch):
    async with db.get_session() as session:
        record = make_record("10.1/version")
        session.add(record)
        await session.flush()
        snapshot = await create_source_snapshot(session, record, "crossref")

        output = make_output()

        import src.workflows.l1_generator as l1_generator_module

        monkeypatch.setattr(l1_generator_module, "SKILL_VERSION", "l1-literature-card-v1")
        monkeypatch.setattr(l1_generator_module, "SCHEMA_VERSION", "1")
        old_artifact = await persist_l1_artifact(session, record, snapshot, output)
        assert old_artifact.skill_version == "l1-literature-card-v1"

        monkeypatch.setattr(l1_generator_module, "SKILL_VERSION", "l1-literature-card-v2")
        monkeypatch.setattr(l1_generator_module, "SCHEMA_VERSION", "2")
        new_artifact = await persist_l1_artifact(session, record, snapshot, output)

        assert new_artifact.id != old_artifact.id
        assert new_artifact.supersedes_id == old_artifact.id
        assert new_artifact.skill_version == "l1-literature-card-v2"


# ---------------------------------------------------------------------------
# run_l1 integrity checks fire before touching llm_client
# ---------------------------------------------------------------------------


async def test_run_l1_rejects_snapshot_from_different_record(db: Database):
    async with db.get_session() as session:
        record_a = make_record("10.1/record-a")
        record_b = make_record("10.1/record-b")
        session.add_all([record_a, record_b])
        await session.flush()

        snapshot_of_a = await create_source_snapshot(session, record_a, "crossref")

        with pytest.raises(ValueError, match="belongs to record"):
            await run_l1(session, record_b, snapshot_of_a, llm_client=None)


async def test_run_l1_rejects_tampered_snapshot_hash(db: Database):
    async with db.get_session() as session:
        record = make_record("10.1/tampered")
        session.add(record)
        await session.flush()

        snapshot = await create_source_snapshot(session, record, "crossref")
        # tamper with the recorded hash directly, simulating corruption/manual edit
        snapshot.analysis_hash = "not-the-real-hash"

        with pytest.raises(ValueError, match="integrity"):
            await run_l1(session, record, snapshot, llm_client=None)
