"""Live PubMed + OpenAlex benchmark acceptance for Batch B1."""

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

from src.core.models import EffectiveSearchPlan, RetrievalRun
from src.web.app import create_app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database", type=Path, default=Path("data/archive_discovery_acceptance.db")
    )
    parser.add_argument(
        "--case",
        type=Path,
        default=Path("benchmarks/archive_cases/targeted_covalent_inhibitors.json"),
    )
    parser.add_argument("--max-results-per-query", type=int, default=50)
    parser.add_argument("--reset", action="store_true")
    return parser.parse_args()


async def inspect_database(app) -> tuple[EffectiveSearchPlan, RetrievalRun]:
    async with app.state.database.get_session() as session:
        plan = (await session.execute(select(EffectiveSearchPlan))).scalar_one()
        run = (await session.execute(select(RetrievalRun))).scalar_one()
        return plan, run


def main() -> None:
    args = parse_args()
    case = json.loads(args.case.read_text(encoding="utf-8"))
    if args.reset and args.database.exists():
        args.database.unlink()

    app = create_app(
        args.database,
        archive_ai_enabled=False,
        scheduler_enabled=False,
    )
    with TestClient(app) as client:
        created = client.post(
            "/archives",
            data={"title": case["topic"], "focus": case["optional_focus"]},
        )
        assert created.status_code == 200
        decided = client.post(
            "/archives/1/review-items/1/decide",
            data={"decision": "INCLUDE", "rationale": "live benchmark"},
        )
        assert decided.status_code == 200
        discovered = client.post(
            "/archives/1/discover",
            data={"max_results_per_query": str(args.max_results_per_query)},
        )
        assert discovered.status_code == 200
        assert "Canonical corpus" in discovered.text

    reopened = create_app(
        args.database,
        archive_ai_enabled=False,
        scheduler_enabled=False,
    )
    with TestClient(reopened):
        plan, run = asyncio.run(inspect_database(reopened))

    assert set(plan.compiled_queries) == {"pubmed", "openalex"}
    assert run.status in {"COMPLETED", "COMPLETED_WITH_ERRORS"}
    assert run.unique_work_count > 0
    assert run.landmark_recall is not None
    target = case["landmark_recall_target"]
    print(
        f"case={case['case_id']} status={run.status} "
        f"pubmed={run.source_metrics.get('pubmed', {}).get('retrieved_hits', 0)} "
        f"openalex={run.source_metrics.get('openalex', {}).get('retrieved_hits', 0)} "
        f"unique_works={run.unique_work_count} "
        f"landmark_recall={len(run.landmark_found)}/{run.landmark_total} "
        f"({run.landmark_recall:.0%}) target={target:.0%}"
    )
    if run.error:
        print(f"provider_errors={run.error}")
    assert run.landmark_recall >= target
    print("PASS")


if __name__ == "__main__":
    main()
