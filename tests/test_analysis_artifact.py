"""
AnalysisArtifact 持久化的 regression test（P0 item 3 + run_l1 完整性检查）

不调用真实 LLM——用 FakeLLMClient 代替真实的 LLMClient，call_with_schema
返回手工构造的固定 L1Output，并记录调用次数，用来验证 Gap 1（run_l1 在
调用 LLM 之前就应该先查一遍是否已有 artifact，真正做到"调两次只打一次
LLM"，而不仅仅是"数据库只有一行"）。

persist_l1_artifact 已经改名为私有的 _persist_l1_artifact，只能通过
run_l1() 触达，所以这里所有测试都改成走 run_l1()。

沿用 test_identity_resolution.py 里的 db fixture 模式。
"""

from pathlib import Path

import pytest
from sqlalchemy import select

from src.core.database import Database
from src.core.models import Record, AnalysisArtifact, Work
from src.core.artifact import create_source_snapshot
from src.workflows.l1_generator import run_l1
from skills.l1_literature_card.contract import L1Output


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test_artifact.db")
    await database.init_db()
    yield database
    await database.close()


def make_record(doi: str, abstract: str = "KRAS mutations drive cancer.", work_id: int | None = 1) -> Record:
    return Record(
        record_id=f"R_{doi.replace('/', '_')}",
        work_id=work_id,
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


class FakeLLMClient:
    """
    llm_client 的替身：call_with_schema 不打真实网络请求，直接返回一个
    固定的 L1Output，同时记录调用次数（call_count），用来断言 run_l1
    到底有没有真正跳过 LLM 调用。
    """

    def __init__(self, output: L1Output | None = None):
        self.output = output or make_output()
        self.call_count = 0

    async def call_with_schema(self, prompt: str, schema, system_prompt: str | None = None):
        self.call_count += 1
        return self.output


# ---------------------------------------------------------------------------
# Idempotency: same record/snapshot -> same artifact row, AND the LLM is
# only ever called once (Gap 1: idempotency must be call-idempotent, not
# just DB-write-idempotent)
# ---------------------------------------------------------------------------


async def test_run_l1_is_idempotent_and_does_not_repeat_llm_call(db: Database):
    async with db.get_session() as session:
        work = Work(work_id="work-idempotency", title="Some Paper")
        session.add(work)
        await session.flush()
        record = make_record("10.1/idem", work_id=work.id)
        session.add(record)
        await session.flush()
        snapshot = await create_source_snapshot(session, record, "crossref")

        fake_llm = FakeLLMClient()
        artifact1 = await run_l1(session, record, snapshot, fake_llm)
        artifact2 = await run_l1(session, record, snapshot, fake_llm)

        assert artifact1.id == artifact2.id
        assert fake_llm.call_count == 1

        count_stmt_result = await session.execute(
            select(AnalysisArtifact).where(AnalysisArtifact.record_id == record.id)
        )
        rows = count_stmt_result.scalars().all()
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Version bump -> new artifact, linked via supersedes_id
# ---------------------------------------------------------------------------


async def test_run_l1_versions_on_skill_bump(db: Database):
    async with db.get_session() as session:
        work = Work(work_id="work-version", title="Some Paper")
        session.add(work)
        await session.flush()
        record = make_record("10.1/version", work_id=work.id)
        session.add(record)
        await session.flush()
        snapshot = await create_source_snapshot(session, record, "crossref")

        # Simulate a pre-existing artifact from an older skill/schema version,
        # constructed directly via the ORM (not through _persist_l1_artifact —
        # this is standing in for data that was already there before this run).
        import uuid as uuid_module

        old_output = make_output()
        old_artifact = AnalysisArtifact(
            artifact_id=uuid_module.uuid4().hex,
            record_id=record.id,
            snapshot_id=snapshot.id,
            analysis_type="L1",
            skill_version="l1-literature-card-v1",
            schema_version="1",
            content=old_output.model_dump(),
            markdown="old markdown",
        )
        session.add(old_artifact)
        await session.flush()

        # run_l1 uses the module's current SKILL_VERSION/SCHEMA_VERSION, which
        # differ from the artifact just inserted above, so it should generate
        # a new artifact and link it via supersedes_id.
        fake_llm = FakeLLMClient()
        new_artifact = await run_l1(session, record, snapshot, fake_llm)

        assert new_artifact.id != old_artifact.id
        assert new_artifact.supersedes_id == old_artifact.id
        assert fake_llm.call_count == 1


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


async def test_run_l1_rejects_tampered_raw_hash(db: Database):
    """
    Gap 4: verify_snapshot_integrity must check raw_hash too, not just
    analysis_hash. Tampering with raw_abstract/raw_hash (leaving
    analysis_text/analysis_hash untouched) must still be caught.
    """
    async with db.get_session() as session:
        record = make_record("10.1/tampered-raw")
        session.add(record)
        await session.flush()

        snapshot = await create_source_snapshot(session, record, "crossref")
        # tamper with raw_abstract only — analysis_hash is still valid
        snapshot.raw_abstract = "something completely different"

        with pytest.raises(ValueError, match="integrity"):
            await run_l1(session, record, snapshot, llm_client=None)


# ---------------------------------------------------------------------------
# Gap 5: record.work_id is None must raise, not silently become "WNone"
# ---------------------------------------------------------------------------


async def test_run_l1_rejects_unconfirmed_identity_without_calling_llm(db: Database):
    async with db.get_session() as session:
        record = make_record("10.1/no-identity", work_id=None)
        session.add(record)
        await session.flush()

        snapshot = await create_source_snapshot(session, record, "crossref")

        fake_llm = FakeLLMClient()
        with pytest.raises(ValueError, match="work_id|identity"):
            await run_l1(session, record, snapshot, fake_llm)

        assert fake_llm.call_count == 0
