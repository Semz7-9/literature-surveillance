"""Batch A Archive Planning product and audit acceptance."""

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from src.core.models import (
    AIProposal,
    ArchiveBuildRun,
    ArchiveOperatorContext,
    ArchiveScopeDraft,
    ArchiveSearchPlan,
    GenerationRun,
    HumanDecision,
    OperatorProfile,
    ReviewItem,
)
from src.web.app import create_app
from src.workflows.archive_planning import (
    ArchivePlanningOutput,
    ScopeAmbiguityOutput,
    ScopeDraftOutput,
    SearchConceptOutput,
    SearchPlanOutput,
)


def test_operator_context_scope_search_plan_and_separate_human_decision(tmp_path):
    app = create_app(tmp_path / "planning.db", scheduler_enabled=False)
    with TestClient(app) as client:
        profile_page = client.get("/operator")
        assert profile_page.status_code == 200 and "Immutable v1" in profile_page.text
        updated = client.post(
            "/operator",
            data={
                "name": "药物化学研究者",
                "research_interests": "covalent inhibitors\nchemical biology",
                "active_projects": "Targeted inhibitor archive",
                "conceptual_preferences": "mechanism before taxonomy",
                "methodological_principles": "separate evidence from interpretation",
                "terminology_preferences": "warhead requires context",
                "note_conventions": "原始观察永远保留。",
            },
        )
        assert "Profile 新版本已保存" in updated.text
        lens = client.post(
            "/operator/lenses",
            data={
                "title": "反应叙事 Lens",
                "lens_type": "CONCEPTUAL",
                "content": "亲核/亲电是一种反应叙事参照系。",
            },
        )
        assert "Reusable Operator Lens" in lens.text

        created = client.post(
            "/archives",
            data={
                "title": "Targeted Covalent Inhibitor Design",
            },
        )
        assert created.status_code == 200
        assert "Automatic ScopeDraft" in created.text
        assert "Semantic Search Plan" in created.text
        assert "Covalent inhibition" in created.text
        assert "pubmed · openalex · semantic_scholar" in created.text
        assert "SEARCHING" in created.text
        assert "3 pending" in created.text or "4 pending" in created.text
        assert "[Title/Abstract]" not in created.text

        decided = client.post(
            "/archives/1/review-items/1/decide",
            data={
                "decision": "INCLUDE",
                "rationale": "研究对象需要覆盖可逆共价策略。",
            },
        )
        assert "人工决策已单独保存" in decided.text
        assert "系统建议" in decided.text and "人工决定：INCLUDE" in decided.text

    async def verify():
        async with app.state.database.get_session() as session:
            profiles = (
                (await session.execute(select(OperatorProfile).order_by(OperatorProfile.version)))
                .scalars()
                .all()
            )
            assert [profile.version for profile in profiles] == [1, 2]
            context = (await session.execute(select(ArchiveOperatorContext))).scalar_one()
            assert context.profile_id == profiles[1].id and context.profile_version == 2
            assert len(context.selected_lens_ids) == 1
            run = (await session.execute(select(ArchiveBuildRun))).scalar_one()
            assert run.state == "SEARCHING" and run.status == "PAUSED"
            scope = (await session.execute(select(ArchiveScopeDraft))).scalar_one()
            plan = (await session.execute(select(ArchiveSearchPlan))).scalar_one()
            assert scope.status == "PROVISIONAL" and scope.generated_by == "SYSTEM"
            assert plan.scope_draft_id == scope.id and len(plan.concepts) == 3
            assert plan.source_targets == ["pubmed", "openalex", "semantic_scholar"]
            assert (await session.execute(select(func.count(ReviewItem.id)))).scalar_one() == 3
            generation = (await session.execute(select(GenerationRun))).scalar_one()
            assert generation.provider == "deterministic"
            assert generation.status == "SUCCEEDED" and generation.parse_status == "VALID"
            assert len(generation.prompt_hash) == 64 and len(generation.text_hash) == 64
            review = await session.get(ReviewItem, 1)
            proposal = await session.get(AIProposal, review.proposal_id)
            decision = (await session.execute(select(HumanDecision))).scalar_one()
            assert proposal.payload["recommendation"] == "INCLUDE"
            assert proposal.status == "OPERATOR_RESOLVED"
            assert decision.decision == "INCLUDE"
            assert decision.rationale == "研究对象需要覆盖可逆共价策略。"
            assert decision.reviewer_metadata["actor"] == "local_operator"

    asyncio.run(verify())


class OverproducingPlanner:
    provider = "fake-provider"
    model = "planner-test"
    origin = "AI"
    system_prompt = "synthetic system prompt"

    def prompt_for(self, **kwargs):
        return "synthetic exact planning prompt"

    async def plan(self, **kwargs):
        ambiguities = [
            ScopeAmbiguityOutput(
                question=f"Question {index}",
                options=["INCLUDE", "EXCLUDE"],
                recommendation="INCLUDE",
                confidence=0.6,
                impact="MEDIUM",
            )
            for index in range(7)
        ]
        return ArchivePlanningOutput(
            scope=ScopeDraftOutput(
                core_scope="Synthetic scope",
                included_domains=["Domain"],
                excluded_domains=[],
                temporal_scope={"mode": "ALL"},
                object_scope=["Object"],
                method_scope=[],
                ambiguities=ambiguities,
                reasoning_summary="Synthetic reasoning",
            ),
            search_plan=SearchPlanOutput(
                concepts=[
                    SearchConceptOutput(
                        label="Core",
                        terms=["term"],
                        purpose="recall",
                    )
                ],
                source_targets=["pubmed"],
                rationale="Semantic only",
            ),
        )

    async def close(self):
        return None


def test_ai_planner_is_audited_and_review_queue_is_capped_at_five(tmp_path):
    app = create_app(
        tmp_path / "ai-planning.db",
        archive_planner_factory=OverproducingPlanner,
        scheduler_enabled=False,
    )
    with TestClient(app) as client:
        page = client.post("/archives", data={"title": "Synthetic Topic"})
        assert page.status_code == 200 and "5 pending" in page.text

    async def verify():
        async with app.state.database.get_session() as session:
            assert (await session.execute(select(func.count(ReviewItem.id)))).scalar_one() == 5
            generation = (await session.execute(select(GenerationRun))).scalar_one()
            assert generation.provider == "fake-provider" and generation.model == "planner-test"
            assert generation.status == "SUCCEEDED"
            scope = (await session.execute(select(ArchiveScopeDraft))).scalar_one()
            assert len(scope.ambiguities) == 5 and scope.generated_by == "AI"
            proposals = (
                (
                    await session.execute(
                        select(AIProposal).where(AIProposal.proposal_type == "SCOPE_AMBIGUITY")
                    )
                )
                .scalars()
                .all()
            )
            assert len(proposals) == 5 and all(item.origin == "AI" for item in proposals)

    asyncio.run(verify())


class FailingPlanner:
    provider = "unavailable-provider"
    model = "broken-model"
    origin = "AI"
    system_prompt = "synthetic system prompt"

    def prompt_for(self, **kwargs):
        return "prompt that fails at provider"

    async def plan(self, **kwargs):
        raise TimeoutError("provider timed out")

    async def close(self):
        return None


def test_failed_model_run_is_audited_before_deterministic_fallback(tmp_path):
    app = create_app(
        tmp_path / "fallback.db",
        archive_planner_factory=FailingPlanner,
        scheduler_enabled=False,
    )
    with TestClient(app) as client:
        page = client.post("/archives", data={"title": "Chemical Space"})
        assert page.status_code == 200
        assert "deterministic" in page.text
        assert "FAILED" in page.text and "SUCCEEDED" in page.text

    async def verify():
        async with app.state.database.get_session() as session:
            runs = (
                (await session.execute(select(GenerationRun).order_by(GenerationRun.id)))
                .scalars()
                .all()
            )
            assert len(runs) == 2
            assert runs[0].provider == "unavailable-provider"
            assert runs[0].status == "FAILED" and runs[0].error_category == "TimeoutError"
            assert runs[1].provider == "deterministic" and runs[1].status == "SUCCEEDED"
            scope = (await session.execute(select(ArchiveScopeDraft))).scalar_one()
            assert scope.generated_by == "SYSTEM"
            build = (await session.execute(select(ArchiveBuildRun))).scalar_one()
            assert build.status == "PAUSED" and build.state == "SEARCHING"

    asyncio.run(verify())
