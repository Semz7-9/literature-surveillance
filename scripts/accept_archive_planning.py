"""Versioned benchmark acceptance for Batch A Archive Planning."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient
from sqlalchemy import select

from src.core.models import (
    AIProposal,
    ArchiveBuildRun,
    ArchiveScopeDraft,
    ArchiveSearchPlan,
    GenerationRun,
    HumanDecision,
    ReviewItem,
)
from src.web.app import create_app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database", type=Path, default=Path("data/archive_planning_acceptance.db")
    )
    parser.add_argument(
        "--case",
        type=Path,
        default=Path("benchmarks/archive_cases/targeted_covalent_inhibitors.json"),
    )
    parser.add_argument("--reset", action="store_true")
    return parser.parse_args()


async def inspect_database(app) -> dict:
    async with app.state.database.get_session() as session:
        return {
            "run": (await session.execute(select(ArchiveBuildRun))).scalar_one(),
            "scope": (await session.execute(select(ArchiveScopeDraft))).scalar_one(),
            "plan": (await session.execute(select(ArchiveSearchPlan))).scalar_one(),
            "reviews": (await session.execute(select(ReviewItem))).scalars().all(),
            "generations": (await session.execute(select(GenerationRun))).scalars().all(),
            "proposals": (await session.execute(select(AIProposal))).scalars().all(),
            "decisions": (await session.execute(select(HumanDecision))).scalars().all(),
        }


def main() -> None:
    args = parse_args()
    case = json.loads(args.case.read_text(encoding="utf-8"))
    if args.reset and args.database.exists():
        args.database.unlink()

    app = create_app(args.database, archive_ai_enabled=False, scheduler_enabled=False)
    with TestClient(app) as client:
        response = client.post(
            "/archives",
            data={
                "title": case["topic"],
                "focus": case["optional_focus"],
            },
        )
        assert response.status_code == 200
        assert "Semantic Search Plan" in response.text
        assert "[Title/Abstract]" not in response.text
        decision_response = client.post(
            "/archives/1/review-items/1/decide",
            data={"decision": "INCLUDE", "rationale": "benchmark operator decision"},
        )
        assert decision_response.status_code == 200

    reopened = create_app(args.database, archive_ai_enabled=False, scheduler_enabled=False)
    with TestClient(reopened) as client:
        assert client.get("/archives/1").status_code == 200
        result = asyncio.run(inspect_database(reopened))

    run = result["run"]
    scope = result["scope"]
    plan = result["plan"]
    assert run.status == "PAUSED" and run.state == "SEARCHING"
    all_terms = {term.lower() for concept in plan.concepts for term in concept["terms"]} | {
        term.lower() for term in plan.historical_vocabulary
    }
    assert set(case["expected_terms"]).issubset(all_terms)
    assert set(case["expected_source_targets"]).issubset(set(plan.source_targets))
    assert len(result["reviews"]) <= case["max_pending_review_items"]
    assert scope.status == "PROVISIONAL"
    assert result["generations"][0].status == "SUCCEEDED"
    assert len(result["generations"][0].prompt_hash) == 64
    assert len(result["decisions"]) == 1
    reviewed_proposal = next(
        proposal
        for proposal in result["proposals"]
        if proposal.id == result["decisions"][0].proposal_id
    )
    assert reviewed_proposal.payload["question"]
    print(
        f"case={case['case_id']} state={run.state} scope=v{scope.version} "
        f"plan=v{plan.version} terms={len(all_terms)} reviews={len(result['reviews'])} "
        f"generations={len(result['generations'])}"
    )
    print("PASS")


if __name__ == "__main__":
    main()
