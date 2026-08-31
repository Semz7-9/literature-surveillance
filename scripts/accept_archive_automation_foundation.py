"""Deterministic acceptance for Archive Automation Foundation."""

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from src.core.models import (
    ArchiveBackgroundLink,
    ArchiveBuildRun,
    ArchiveBuildStep,
    ArchiveScope,
    BackgroundNode,
    BackgroundProfile,
    TopicArchive,
)
from src.web.app import create_app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database", type=Path, default=Path("data/archive_automation_acceptance.db")
    )
    parser.add_argument("--reset", action="store_true")
    return parser.parse_args()


async def metrics(app) -> dict:
    async with app.state.database.get_session() as session:
        archive = (await session.execute(select(TopicArchive))).scalar_one()
        run = (
            await session.execute(
                select(ArchiveBuildRun).where(ArchiveBuildRun.archive_id == archive.id)
            )
        ).scalar_one()
        steps = (
            (
                await session.execute(
                    select(ArchiveBuildStep)
                    .where(ArchiveBuildStep.run_id == run.id)
                    .order_by(ArchiveBuildStep.id)
                )
            )
            .scalars()
            .all()
        )
        links = (
            (
                await session.execute(
                    select(ArchiveBackgroundLink).where(
                        ArchiveBackgroundLink.archive_id == archive.id
                    )
                )
            )
            .scalars()
            .all()
        )
        return {
            "archive": archive,
            "run": run,
            "steps": steps,
            "attached": sum(link.status == "ATTACHED" for link in links),
            "candidates": sum(link.status == "CANDIDATE" for link in links),
            "scopes": (await session.execute(select(func.count(ArchiveScope.id)))).scalar_one(),
            "profiles": (
                await session.execute(select(func.count(BackgroundProfile.id)))
            ).scalar_one(),
            "nodes": (await session.execute(select(func.count(BackgroundNode.id)))).scalar_one(),
        }


def main() -> None:
    args = parse_args()
    if args.reset and args.database.exists():
        args.database.unlink()

    app = create_app(args.database, scheduler_enabled=False, archive_ai_enabled=False)
    with TestClient(app) as client:
        response = client.post(
            "/archives",
            data={"title": "Targeted Covalent Inhibitor Design"},
        )
        assert response.status_code == 200
        assert "Archive Planning" in response.text
        assert "SEARCHING" in response.text

    # Recreate the complete app to exercise durable close/reopen behavior.
    reopened = create_app(args.database, scheduler_enabled=False, archive_ai_enabled=False)
    with TestClient(reopened) as client:
        response = client.get("/archives/1")
        assert response.status_code == 200 and "SEARCHING" in response.text
        result = asyncio.run(metrics(reopened))

    assert result["run"].status == "PAUSED" and result["run"].state == "SEARCHING"
    assert [step.status for step in result["steps"]] == [
        "COMPLETED",
        "COMPLETED",
        "COMPLETED",
        "PENDING",
        "PENDING",
    ]
    assert result["scopes"] == 1
    assert result["attached"] == 3 and result["candidates"] == 1
    assert result["profiles"] == 7 and result["nodes"] == 9
    print(
        f"state={result['run'].state} status={result['run'].status} "
        f"attached={result['attached']} candidates={result['candidates']} "
        f"profiles={result['profiles']} nodes={result['nodes']}"
    )
    print("PASS")


if __name__ == "__main__":
    main()
