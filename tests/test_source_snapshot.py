"""
SourceSnapshot 的 regression test

覆盖 review 里的 P0 item 2：Crossref 摘要可能带 JATS/XML 标签，
raw_abstract 必须原样保留、analysis_text 必须是标签剥离后的纯文本，
两者各自有自己的 hash，且 hash 必须与实际存的文本一致。

沿用 test_identity_resolution.py 里的 db fixture 模式（tmp_path + Database）。
"""

import hashlib
from pathlib import Path

import pytest

from src.core.database import Database
from src.core.models import Record
from src.core.artifact import create_source_snapshot
from src.core.text_normalize import normalize_abstract_text, NORMALIZER_VERSION


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test_snapshot.db")
    await database.init_db()
    yield database
    await database.close()


def make_record(doi: str, abstract: str | None, **kwargs) -> Record:
    return Record(
        record_id=f"R_{doi.replace('/', '_')}",
        work_id=None,
        title="Some Paper",
        authors=[{"name": "Smith J"}],
        doi=doi,
        abstract=abstract,
        evidence_level="E1" if abstract else "E0",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Pure function: normalize_abstract_text
# ---------------------------------------------------------------------------


def test_normalize_strips_jats_tags_and_unescapes_entities():
    raw = "<jats:p>KRAS &amp; <jats:italic>BRAF</jats:italic> mutations drive cancer.</jats:p>"
    result = normalize_abstract_text(raw)
    assert result == "KRAS & BRAF mutations drive cancer."


def test_normalize_none_and_empty_return_none():
    assert normalize_abstract_text(None) is None
    assert normalize_abstract_text("") is None
    assert normalize_abstract_text("   ") is None


def test_normalize_collapses_whitespace():
    raw = "Line one.\n\n  Line   two."
    assert normalize_abstract_text(raw) == "Line one. Line two."


# ---------------------------------------------------------------------------
# create_source_snapshot with JATS-tagged abstract
# ---------------------------------------------------------------------------


async def test_create_source_snapshot_strips_tags_for_analysis_text(db: Database):
    async with db.get_session() as session:
        raw_abstract = "<jats:p>We report a novel <jats:italic>KRAS</jats:italic> inhibitor.</jats:p>"
        record = make_record("10.1/jats", raw_abstract)
        session.add(record)
        await session.flush()

        snapshot = await create_source_snapshot(session, record, "crossref")

        assert snapshot.raw_abstract == raw_abstract
        assert snapshot.analysis_text == "We report a novel KRAS inhibitor."
        assert snapshot.raw_hash == hashlib.sha256(raw_abstract.encode()).hexdigest()
        assert snapshot.analysis_hash == hashlib.sha256(
            snapshot.analysis_text.encode()
        ).hexdigest()
        assert snapshot.normalizer_version == NORMALIZER_VERSION
        assert snapshot.source_name == "crossref"
        assert snapshot.record_id == record.id


# ---------------------------------------------------------------------------
# create_source_snapshot with abstract=None
# ---------------------------------------------------------------------------


async def test_create_source_snapshot_handles_missing_abstract(db: Database):
    async with db.get_session() as session:
        record = make_record("10.1/noabstract", None)
        session.add(record)
        await session.flush()

        snapshot = await create_source_snapshot(session, record, "crossref")

        assert snapshot.raw_abstract is None
        assert snapshot.analysis_text is None
        assert snapshot.raw_hash is None
        assert snapshot.analysis_hash is None
        assert snapshot.normalizer_version is None
