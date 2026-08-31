"""Batch B1 two-source retrieval, decision resolution, and corpus materialization."""

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import select

from src.core.models import (
    ArchiveWork,
    EffectiveSearchPlan,
    RetrievalHit,
    RetrievalRun,
    Work,
)
from src.web.app import create_app

PUBMED_ITEMS = [
    {
        "PMID": "1001",
        "DOI": "10.1000/shared",
        "title": "Shared covalent inhibitor landmark",
        "authors": [{"name": "Alpha Author", "affiliation": [], "orcid": None}],
        "journal": "Journal A",
        "date": {"year": "2015", "month": "Jan", "day": "01"},
        "abstract": "A targeted covalent inhibitor study.",
        "pmcid": None,
        "mesh": ["Enzyme Inhibitors"],
    },
    {
        "PMID": "1002",
        "DOI": "10.1000/pubmed-only",
        "title": "PubMed-only covalent inhibitor",
        "authors": [{"name": "Beta Author", "affiliation": [], "orcid": None}],
        "journal": "Journal B",
        "date": {"year": "2018", "month": "Feb", "day": "02"},
        "abstract": "An electrophilic warhead study.",
        "pmcid": None,
        "mesh": [],
    },
    {
        "PMID": "1003",
        "DOI": None,
        "title": "Identifier-only covalent probe",
        "authors": [{"name": "Delta Author", "affiliation": [], "orcid": None}],
        "journal": "Journal D",
        "date": {"year": "2019", "month": "Mar", "day": "03"},
        "abstract": "A covalent target engagement probe.",
        "pmcid": None,
        "mesh": [],
    },
]

OPENALEX_ITEMS = [
    {
        "id": "https://openalex.org/W1",
        "doi": "https://doi.org/10.1000/shared",
        "display_name": "Shared covalent inhibitor landmark",
        "authorships": [
            {
                "author": {"display_name": "Alpha Author", "orcid": None},
                "institutions": [],
            }
        ],
        "primary_location": {"source": {"display_name": "Journal A"}},
        "publication_date": "2015-01-01",
        "abstract_inverted_index": {"targeted": [0], "covalent": [1]},
        "ids": {},
        "type": "article",
        "cited_by_count": 200,
    },
    {
        "id": "https://openalex.org/W2",
        "doi": "https://doi.org/10.1000/openalex-only",
        "display_name": "OpenAlex-only covalent chemistry",
        "authorships": [
            {
                "author": {"display_name": "Gamma Author", "orcid": None},
                "institutions": [],
            }
        ],
        "primary_location": {"source": {"display_name": "Journal C"}},
        "publication_date": "2020-03-03",
        "abstract_inverted_index": {"warhead": [0], "selectivity": [1]},
        "ids": {},
        "type": "article",
        "cited_by_count": 50,
    },
    {
        "id": "https://openalex.org/W3",
        "doi": None,
        "display_name": "Identifier-only covalent probe",
        "authorships": [
            {
                "author": {"display_name": "Delta Author", "orcid": None},
                "institutions": [],
            }
        ],
        "primary_location": {"source": {"display_name": "Journal D"}},
        "publication_date": "2019-03-03",
        "abstract_inverted_index": {"target": [0], "engagement": [1]},
        "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/1003"},
        "type": "article",
        "cited_by_count": 20,
    },
]


class FakePubMed:
    def __init__(self):
        self.calls = 0

    async def discover_works(self, **kwargs):
        self.calls += 1
        return (PUBMED_ITEMS, None) if self.calls == 1 else ([], None)

    async def close(self):
        return None


class FakeOpenAlex:
    def __init__(self):
        self.calls = 0

    async def search_works(self, **kwargs):
        self.calls += 1
        return (OPENALEX_ITEMS, None) if self.calls == 1 else ([], None)

    async def close(self):
        return None


def test_effective_plan_two_source_dedup_and_metrics(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.web.app.benchmark_landmarks",
        lambda topic: [
            {"identifier_type": "doi", "id": "10.1000/shared"},
            {"identifier_type": "doi", "id": "10.1000/openalex-only"},
        ],
    )
    app = create_app(
        tmp_path / "discovery.db",
        pubmed_factory=FakePubMed,
        openalex_factory=FakeOpenAlex,
        scheduler_enabled=False,
    )
    with TestClient(app) as client:
        created = client.post("/archives", data={"title": "Targeted Covalent Inhibitor Design"})
        assert created.status_code == 200
        decision = client.post(
            "/archives/1/review-items/1/decide",
            data={"decision": "INCLUDE", "rationale": "benchmark scope"},
        )
        assert decision.status_code == 200
        discovered = client.post("/archives/1/discover", data={"max_results_per_query": "10"})
        assert discovered.status_code == 200
        assert "PubMed + OpenAlex 自动发现已完成" in discovered.text
        assert "Canonical corpus" in discovered.text
        assert "100%" in discovered.text
        assert "reversible covalent inhibitor" in discovered.text

    async def verify():
        async with app.state.database.get_session() as session:
            plan = (await session.execute(select(EffectiveSearchPlan))).scalar_one()
            assert plan.applied_decisions[0]["decision"] == "INCLUDE"
            assert any(
                "reversible covalent inhibitor" in concept["terms"] for concept in plan.concepts
            )
            assert set(plan.compiled_queries) == {"pubmed", "openalex"}
            run = (await session.execute(select(RetrievalRun))).scalar_one()
            assert run.status == "COMPLETED"
            assert run.unique_work_count == 4
            assert run.landmark_recall == 1.0
            assert run.source_metrics["pubmed"]["unique_works"] == 3
            assert run.source_metrics["openalex"]["unique_works"] == 3
            assert len((await session.execute(select(RetrievalHit))).scalars().all()) == 6
            assert len((await session.execute(select(ArchiveWork))).scalars().all()) == 4
            assert len((await session.execute(select(Work))).scalars().all()) == 4

    asyncio.run(verify())


class FailingOpenAlex:
    async def search_works(self, **kwargs):
        raise TimeoutError("OpenAlex timed out")

    async def close(self):
        return None


def test_one_source_failure_keeps_successful_corpus(tmp_path):
    app = create_app(
        tmp_path / "partial.db",
        pubmed_factory=FakePubMed,
        openalex_factory=FailingOpenAlex,
        scheduler_enabled=False,
    )
    with TestClient(app) as client:
        client.post("/archives", data={"title": "Targeted Covalent Inhibitor Design"})
        page = client.post("/archives/1/discover", data={"max_results_per_query": "10"})
        assert page.status_code == 200
        assert "COMPLETED_WITH_ERRORS" in page.text
        assert "OpenAlex timed out" in page.text

    async def verify():
        async with app.state.database.get_session() as session:
            run = (await session.execute(select(RetrievalRun))).scalar_one()
            assert run.status == "COMPLETED_WITH_ERRORS"
            assert run.unique_work_count == 3
            assert "openalex" in run.error

    asyncio.run(verify())
