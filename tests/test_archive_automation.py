"""Archive Automation Foundation acceptance and recovery tests."""

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from src.core.database import Database
from src.core.models import (
    ArchiveBackgroundLink,
    ArchiveBuildRun,
    ArchiveBuildStep,
    ArchiveScope,
    BackgroundAIContribution,
    BackgroundNode,
    BackgroundOperatorContribution,
    BackgroundProfile,
    TopicArchive,
)
from src.llm.routing import (
    AIContributionRequest,
    AIContributionResult,
    RoleBasedModelRouter,
)
from src.web.app import create_app
from src.workflows.archive_builder import (
    BackgroundResolver,
    create_archive_build_run,
    record_ai_contribution,
    run_archive_foundation,
)


def test_topic_only_build_persists_shared_background_and_operator_thought(tmp_path):
    database_path = tmp_path / "automation.db"
    app = create_app(database_path, scheduler_enabled=False)
    with TestClient(app) as client:
        response = client.post(
            "/archives",
            data={
                "title": "Targeted Covalent Inhibitor Design",
                "focus": "medicinal chemistry / mechanism",
            },
        )
        assert response.status_code == 200
        assert "Archive Planning 已完成" in response.text
        assert "Electrophile–Nucleophile Chemistry" in response.text
        assert "Reaction Kinetics" in response.text
        assert "Structure–Activity Relationships" in response.text
        assert "是否把“Chemoproteomics”纳入背景" in response.text
        assert "SEARCHING" in response.text
        assert "Advanced / Expert Mode" in response.text

        note = "亲核/亲电更像一种反应叙事参照系。"
        saved = client.post(
            "/archives/1/background-links/1/operator-notes",
            data={
                "contribution_type": "INTERPRETATION",
                "raw_text": note,
            },
        )
        # Link ordering is resolver-defined, and id=1 is a high-confidence attached node.
        assert saved.status_code == 200
        assert note in saved.text

        listing = client.get("/archives")
        assert "PAUSED" in listing.text and "SEARCHING" in listing.text

    # A completely new app instance proves close/reopen recovery from SQLite.
    reopened = create_app(database_path, scheduler_enabled=False)
    with TestClient(reopened) as client:
        page = client.get("/archives/1")
        assert page.status_code == 200
        assert "Targeted Covalent Inhibitor Design" in page.text
        assert "亲核/亲电更像一种反应叙事参照系。" in page.text
        assert "3 nodes" in page.text
        # A second Archive reuses the same library rows instead of copying text.
        client.post("/archives", data={"title": "Reversible Covalent Inhibitors"})

    async def verify():
        async with reopened.state.database.get_session() as session:
            first = (
                await session.execute(
                    select(TopicArchive).where(
                        TopicArchive.title == "Targeted Covalent Inhibitor Design"
                    )
                )
            ).scalar_one()
            assert first.background_mode == "AUTO"
            assert first.focus == "medicinal chemistry / mechanism"
            run = (
                await session.execute(
                    select(ArchiveBuildRun).where(ArchiveBuildRun.archive_id == first.id)
                )
            ).scalar_one()
            assert run.state == "SEARCHING" and run.status == "PAUSED"
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
            assert [step.status for step in steps] == [
                "COMPLETED",
                "COMPLETED",
                "COMPLETED",
                "PENDING",
                "PENDING",
            ]
            assert steps[0].started_at and steps[0].finished_at
            assert steps[0].input_version == 1 and steps[0].output_artifact["provisional"]
            assert (
                await session.execute(
                    select(func.count(ArchiveScope.id)).where(ArchiveScope.archive_id == first.id)
                )
            ).scalar_one() == 1
            assert (
                await session.execute(select(func.count(BackgroundProfile.id)))
            ).scalar_one() == 7
            assert (await session.execute(select(func.count(BackgroundNode.id)))).scalar_one() == 9
            assert (
                await session.execute(
                    select(func.count(ArchiveBackgroundLink.id)).where(
                        ArchiveBackgroundLink.archive_id == first.id
                    )
                )
            ).scalar_one() == 4
            assert (
                await session.execute(select(func.count(BackgroundOperatorContribution.id)))
            ).scalar_one() == 1

            contribution = (
                await session.execute(select(BackgroundOperatorContribution))
            ).scalar_one()
            node = await session.get(BackgroundNode, contribution.node_id)
            canonical_before = node.canonical_summary
            await record_ai_contribution(
                session,
                archive_id=first.id,
                node_id=node.id,
                result=AIContributionResult(
                    provider="test-provider",
                    model="test-model",
                    role="CRITIQUE",
                    output={"possible_objection": "Boundary depends on context."},
                ),
                input_refs=[{"kind": "operator_contribution", "id": contribution.id}],
            )
            await session.commit()
            assert contribution.raw_text == "亲核/亲电更像一种反应叙事参照系。"
            assert node.canonical_summary == canonical_before
            ai = (await session.execute(select(BackgroundAIContribution))).scalar_one()
            assert ai.provider == "test-provider" and ai.role == "CRITIQUE"
            assert ai.input_refs[0]["id"] == contribution.id

    asyncio.run(verify())


class AlwaysFailResolver(BackgroundResolver):
    async def resolve(self, session, *, topic, focus, provisional_scope):
        raise RuntimeError("resolver unavailable")


async def test_failed_background_step_is_persisted_and_retryable(tmp_path):
    database = Database(tmp_path / "retry.db")
    await database.init_db()
    async with database.get_session() as session:
        archive = TopicArchive(title="Retryable Topic", focus="mechanism")
        session.add(archive)
        await session.flush()
        run = await create_archive_build_run(session, archive)
        await run_archive_foundation(session, archive, run, AlwaysFailResolver())
        await session.commit()
        assert run.status == "FAILED" and run.state == "BACKGROUND"
        assert run.error == "resolver unavailable"

        await run_archive_foundation(session, archive, run)
        await session.commit()
        assert run.status == "PAUSED" and run.state == "LEXICON"
        attempts = (
            (
                await session.execute(
                    select(ArchiveBuildStep)
                    .where(
                        ArchiveBuildStep.run_id == run.id,
                        ArchiveBuildStep.stage == "BACKGROUND",
                    )
                    .order_by(ArchiveBuildStep.attempt)
                )
            )
            .scalars()
            .all()
        )
        assert [item.status for item in attempts] == ["FAILED", "COMPLETED"]
        assert attempts[0].error == "resolver unavailable"
        assert attempts[1].attempt == 2 and attempts[1].output_artifact["attached"] >= 1
    await database.close()


class CritiqueProvider:
    async def contribute(self, request):
        return AIContributionResult(
            provider="replaceable-provider",
            model="critic-v1",
            role=request.role,
            output={"critique": request.prompt},
        )


async def test_role_router_uses_capability_role_instead_of_vendor_name():
    router = RoleBasedModelRouter()
    router.register("CRITIQUE", CritiqueProvider())
    result = await router.contribute(
        AIContributionRequest(
            role="CRITIQUE",
            prompt="check boundary",
            input_refs=[{"scope": 1}],
        )
    )
    assert result.role == "CRITIQUE"
    assert result.provider == "replaceable-provider"
