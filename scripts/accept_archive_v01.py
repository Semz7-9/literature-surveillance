"""Run the complete Topic Archive v0.1 chain against real PubMed data."""

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient

from src.web.app import create_app


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/archive_acceptance.db"))
    parser.add_argument("--reset", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    database_path = args.database.resolve()
    if args.reset:
        if not database_path.is_relative_to(PROJECT_ROOT.resolve()):
            raise ValueError("--reset only deletes databases inside the project workspace")
        for path in (
            database_path,
            database_path.with_name(database_path.name + "-wal"),
            database_path.with_name(database_path.name + "-shm"),
        ):
            if path.exists():
                path.unlink()
    app = create_app(database_path, scheduler_enabled=False)
    with TestClient(app) as client:
        steps = [
            client.post("/archives", data={
                "title": "Covalent Inhibitor Design",
                "description": "Representations and design strategies for targeted covalent inhibitors.",
            }),
            client.post("/archives/1/scope", data={
                "core_concepts": "covalent inhibitor\ntargeted covalent inhibitor",
                "background_concepts": "electrophile\nwarhead\nbinding kinetics",
                "exclusions": "irreversible toxicity",
                "notes": "Small-molecule targeted covalent inhibitor design.",
            }),
            client.post("/archives/1/background", data={
                "title": "Covalent inhibition background",
                "content": "Covalent inhibitors combine reversible recognition with controlled bond formation.",
            }),
            client.post("/archives/1/concept-sets", data={
                "name": "Covalent Inhibitor", "description": "Anchor concept",
                "terms_text": "covalent inhibitor\ntargeted covalent inhibitor",
                "source": "manual",
            }),
            client.post("/archives/1/concept-sets", data={
                "name": "Design", "description": "Design and representation",
                "terms_text": "inhibitor design\nmolecular design\n? warhead design",
                "source": "manual",
            }),
            client.post("/archives/1/search-strategies"),
        ]
        if any(response.status_code != 200 for response in steps):
            raise RuntimeError([response.status_code for response in steps])
        executed = client.post("/archives/1/search-strategies/1/execute")
        if executed.status_code != 200:
            raise RuntimeError(f"Archive search failed: {executed.status_code} {executed.text[:500]}")
    connection = sqlite3.connect(database_path)
    metrics = {
        "archives": connection.execute("select count(*) from topic_archives").fetchone()[0],
        "scope_versions": connection.execute("select count(*) from archive_scopes").fetchone()[0],
        "concept_sets": connection.execute("select count(*) from concept_sets").fetchone()[0],
        "strategies": connection.execute("select count(*) from search_strategies").fetchone()[0],
        "archive_works": connection.execute("select count(*) from archive_works").fetchone()[0],
        "revisions": connection.execute("select count(*) from archive_revisions").fetchone()[0],
    }
    duplicate_memberships = connection.execute(
        "select count(*) from (select archive_id,work_id from archive_works "
        "group by archive_id,work_id having count(*) > 1)"
    ).fetchone()[0]
    connection.close()
    passed = (
        metrics["archives"] == 1 and metrics["scope_versions"] == 1
        and metrics["concept_sets"] == 2 and metrics["strategies"] == 1
        and metrics["archive_works"] > 0 and duplicate_memberships == 0
    )
    print("Topic Archive v0.1:", " ".join(f"{key}={value}" for key, value in metrics.items()))
    print(f"duplicate_memberships={duplicate_memberships}")
    print("PASS" if passed else "FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
