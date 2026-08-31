"""Batch A: operator-aware, auditable Archive planning without database syntax."""

import hashlib
import json
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.models import (
    AIProposal,
    ArchiveBackgroundLink,
    ArchiveBuildRun,
    ArchiveOperatorContext,
    ArchiveScopeDraft,
    ArchiveSearchPlan,
    BackgroundNode,
    GenerationRun,
    OperatorLens,
    OperatorProfile,
    ReviewItem,
    TopicArchive,
)
from ..llm.client import LLMClient
from .archive import record_archive_revision
from .archive_builder import get_build_step, run_archive_foundation

MAX_REVIEW_ITEMS = 5
PLANNER_VERSION = "archive-planning-v1"


class ScopeAmbiguityOutput(BaseModel):
    question: str
    options: list[Literal["INCLUDE", "BACKGROUND_ONLY", "EXCLUDE"]]
    recommendation: Literal["INCLUDE", "BACKGROUND_ONLY", "EXCLUDE"]
    confidence: float = Field(ge=0, le=1)
    impact: Literal["LOW", "MEDIUM", "HIGH"] = "MEDIUM"


class ScopeDraftOutput(BaseModel):
    core_scope: str
    included_domains: list[str]
    excluded_domains: list[str]
    temporal_scope: dict = Field(default_factory=dict)
    object_scope: list[str]
    method_scope: list[str]
    ambiguities: list[ScopeAmbiguityOutput] = Field(default_factory=list)
    reasoning_summary: str


class SearchConceptOutput(BaseModel):
    label: str
    terms: list[str]
    purpose: str


class SearchPlanOutput(BaseModel):
    concepts: list[SearchConceptOutput]
    historical_vocabulary: list[str] = Field(default_factory=list)
    hard_exclusions: list[str] = Field(default_factory=list)
    soft_exclusions: list[str] = Field(default_factory=list)
    source_targets: list[str] = Field(default_factory=list)
    rationale: str


class ArchivePlanningOutput(BaseModel):
    scope: ScopeDraftOutput
    search_plan: SearchPlanOutput


class ArchivePlanner(Protocol):
    provider: str
    model: str
    origin: str
    system_prompt: str

    def prompt_for(
        self, *, topic: str, focus: str, operator_context: str, backgrounds: list[str]
    ) -> str: ...

    async def plan(
        self, *, topic: str, focus: str, operator_context: str, backgrounds: list[str]
    ) -> ArchivePlanningOutput: ...

    async def close(self) -> None: ...


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class RuleBasedArchivePlanner:
    """Deterministic fallback and benchmark baseline, not presented as an AI model."""

    provider = "deterministic"
    model = PLANNER_VERSION
    origin = "SYSTEM"
    system_prompt = ""

    def prompt_for(
        self, *, topic: str, focus: str, operator_context: str, backgrounds: list[str]
    ) -> str:
        return json.dumps(
            {
                "planner": PLANNER_VERSION,
                "topic": topic,
                "focus": focus,
                "operator_context": operator_context,
                "backgrounds": backgrounds,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    async def plan(
        self, *, topic: str, focus: str, operator_context: str, backgrounds: list[str]
    ) -> ArchivePlanningOutput:
        text = f"{topic} {focus}".lower()
        included = _dedupe([*backgrounds, focus])
        excluded: list[str] = []
        methods: list[str] = []
        objects = [topic]
        historical: list[str] = []
        hard_exclusions: list[str] = []
        soft_exclusions: list[str] = []
        ambiguities: list[ScopeAmbiguityOutput] = []
        concepts = [
            SearchConceptOutput(
                label="Core topic",
                terms=[topic],
                purpose="High-recall topic anchor",
            )
        ]

        if "covalent" in text or "共价" in text:
            included = _dedupe(
                [
                    *included,
                    "Medicinal Chemistry",
                    "Organic Chemistry",
                    "Reaction Kinetics",
                ]
            )
            excluded = ["non-targeted broadly reactive compounds without a defined target"]
            methods = ["kinetic characterization", "structure–activity relationships"]
            objects = ["targeted covalent inhibitors", "warheads", "target engagement"]
            historical = ["irreversible inhibitor", "affinity labeling"]
            hard_exclusions = ["non-targeted toxic reactive compounds"]
            soft_exclusions = ["general covalent chemistry without inhibitor design relevance"]
            concepts = [
                SearchConceptOutput(
                    label="Covalent inhibition",
                    terms=["targeted covalent inhibitor", "covalent inhibitor", "covalent drug"],
                    purpose="Core recall",
                ),
                SearchConceptOutput(
                    label="Mechanism",
                    terms=["electrophile", "warhead", "target engagement", "reaction kinetics"],
                    purpose="Mechanism and reactivity branch",
                ),
                SearchConceptOutput(
                    label="Design",
                    terms=["medicinal chemistry", "structure activity relationship", "selectivity"],
                    purpose="Design and optimization branch",
                ),
            ]
            ambiguities = [
                ScopeAmbiguityOutput(
                    question="是否将 reversible covalent inhibitors 纳入主体？",
                    options=["INCLUDE", "BACKGROUND_ONLY", "EXCLUDE"],
                    recommendation="INCLUDE",
                    confidence=0.78,
                    impact="HIGH",
                ),
                ScopeAmbiguityOutput(
                    question="Chemoproteomics 应作为主体研究分支还是背景方法？",
                    options=["INCLUDE", "BACKGROUND_ONLY", "EXCLUDE"],
                    recommendation="BACKGROUND_ONLY",
                    confidence=0.72,
                    impact="MEDIUM",
                ),
                ScopeAmbiguityOutput(
                    question="是否纳入没有明确靶点的 reactive covalent compounds？",
                    options=["INCLUDE", "BACKGROUND_ONLY", "EXCLUDE"],
                    recommendation="EXCLUDE",
                    confidence=0.84,
                    impact="HIGH",
                ),
            ]
        elif "chemical space" in text or "化学空间" in text:
            included = _dedupe([*included, "molecular representation", "compound libraries"])
            excluded = ["astronomical or interstellar chemical composition"]
            methods = ["molecular descriptors", "dimensionality reduction", "similarity search"]
            historical = ["molecular space", "chemical universe"]
            hard_exclusions = ["interstellar chemical space"]
            concepts = [
                SearchConceptOutput(
                    label="Chemical space",
                    terms=["chemical space", "molecular space", "chemical universe"],
                    purpose="Core and historical vocabulary",
                ),
                SearchConceptOutput(
                    label="Representation",
                    terms=["molecular descriptor", "fingerprint", "molecular representation"],
                    purpose="Representation branch",
                ),
            ]
            ambiguities = [
                ScopeAmbiguityOutput(
                    question="Metabolomics 中的 chemical space 是否纳入主体？",
                    options=["INCLUDE", "BACKGROUND_ONLY", "EXCLUDE"],
                    recommendation="BACKGROUND_ONLY",
                    confidence=0.7,
                    impact="MEDIUM",
                )
            ]

        return ArchivePlanningOutput(
            scope=ScopeDraftOutput(
                core_scope=f"围绕“{topic}”建立可修订的学术专题边界。",
                included_domains=included,
                excluded_domains=excluded,
                temporal_scope={"mode": "ALL_AVAILABLE", "reason": "初始探索不预设年代截断"},
                object_scope=objects,
                method_scope=methods,
                ambiguities=ambiguities[:MAX_REVIEW_ITEMS],
                reasoning_summary=(
                    "基于 Topic、可选 Focus、共享背景与 Operator Context 形成初稿；"
                    "边界判断保持 provisional。"
                ),
            ),
            search_plan=SearchPlanOutput(
                concepts=concepts,
                historical_vocabulary=historical,
                hard_exclusions=hard_exclusions,
                soft_exclusions=soft_exclusions,
                source_targets=["pubmed", "openalex", "semantic_scholar"],
                rationale=(
                    "先保存 provider-neutral 语义计划；数据库语法由 Batch B compiler 生成。"
                ),
            ),
        )

    async def close(self) -> None:
        return None


class LLMArchivePlanner:
    """One structured reasoning call behind the fixed Archive workflow."""

    origin = "AI"
    system_prompt = (
        "You are the planning capability inside a durable academic archive workflow. "
        "Return suggestions, never claim that a human made a decision."
    )

    def __init__(self, client: LLMClient):
        self.client = client
        self.provider = client.provider
        self.model = client.model

    def prompt_for(
        self, *, topic: str, focus: str, operator_context: str, backgrounds: list[str]
    ) -> str:
        return (
            f"Topic: {topic}\nOptional focus: {focus or '(none)'}\n"
            f"Operator context:\n{operator_context or '(none)'}\n"
            f"Shared background nodes: {', '.join(backgrounds) or '(none)'}\n\n"
            "Produce a conservative provisional scope and a provider-neutral semantic search plan. "
            "Do not write PubMed/OpenAlex syntax. Distinguish hard exclusions from soft exclusions. "
            "Only include genuine high-impact ambiguities needing operator judgment, maximum five. "
            "Recommendations must use INCLUDE, BACKGROUND_ONLY, or EXCLUDE."
        )

    async def plan(
        self, *, topic: str, focus: str, operator_context: str, backgrounds: list[str]
    ) -> ArchivePlanningOutput:
        prompt = self.prompt_for(
            topic=topic,
            focus=focus,
            operator_context=operator_context,
            backgrounds=backgrounds,
        )
        return await self.client.call_with_schema(
            prompt,
            ArchivePlanningOutput,
            system_prompt=self.system_prompt,
        )

    async def close(self) -> None:
        await self.client.close()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def ensure_default_operator_profile(session: AsyncSession) -> OperatorProfile:
    profile = (
        await session.execute(
            select(OperatorProfile)
            .where(OperatorProfile.profile_key == "default")
            .order_by(OperatorProfile.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if profile is None:
        profile = OperatorProfile(
            name="默认研究者",
            note_conventions="保留来源、区分事实/解释/启发式，不用 AI 改写覆盖原始想法。",
        )
        session.add(profile)
        await session.flush()
    return profile


async def ensure_archive_operator_context(
    session: AsyncSession,
    archive: TopicArchive,
) -> ArchiveOperatorContext:
    context = (
        await session.execute(
            select(ArchiveOperatorContext).where(ArchiveOperatorContext.archive_id == archive.id)
        )
    ).scalar_one_or_none()
    if context is not None:
        return context
    profile = await ensure_default_operator_profile(session)
    lens_ids = list(
        (
            await session.execute(
                select(OperatorLens.id).where(
                    OperatorLens.profile_id == profile.id,
                    OperatorLens.status == "ACTIVE",
                )
            )
        )
        .scalars()
        .all()
    )
    context = ArchiveOperatorContext(
        archive_id=archive.id,
        profile_id=profile.id,
        profile_version=profile.version,
        selected_lens_ids=lens_ids,
        archive_context=archive.focus,
    )
    session.add(context)
    await session.flush()
    return context


async def render_operator_context(
    session: AsyncSession,
    context: ArchiveOperatorContext,
) -> str:
    profile = await session.get(OperatorProfile, context.profile_id)
    lenses = []
    if context.selected_lens_ids:
        lenses = list(
            (
                await session.execute(
                    select(OperatorLens).where(OperatorLens.id.in_(context.selected_lens_ids))
                )
            )
            .scalars()
            .all()
        )
    if profile is None:
        return context.archive_context
    return "\n".join(
        filter(
            None,
            [
                f"Research interests: {', '.join(profile.research_interests)}",
                f"Active projects: {', '.join(profile.active_projects)}",
                f"Conceptual preferences: {', '.join(profile.conceptual_preferences)}",
                f"Methodological principles: {', '.join(profile.methodological_principles)}",
                f"Terminology preferences: {', '.join(profile.terminology_preferences)}",
                f"Note conventions: {profile.note_conventions}",
                *(f"Lens — {lens.title}: {lens.content}" for lens in lenses),
                f"Archive context: {context.archive_context}",
            ],
        )
    )


async def _execute_planner(
    session: AsyncSession,
    archive: TopicArchive,
    run: ArchiveBuildRun,
    planner: ArchivePlanner,
    *,
    operator_context: str,
    backgrounds: list[str],
) -> tuple[ArchivePlanningOutput, GenerationRun]:
    prompt = planner.prompt_for(
        topic=archive.title,
        focus=archive.focus,
        operator_context=operator_context,
        backgrounds=backgrounds,
    )
    prompt_hash = _sha256(
        json.dumps(
            {"system": planner.system_prompt, "user": prompt},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    text_hash = _sha256(
        json.dumps(
            {
                "topic": archive.title,
                "focus": archive.focus,
                "operator_context": operator_context,
                "backgrounds": backgrounds,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    cache_key = _sha256(f"{planner.provider}:{planner.model}:REASONING:{prompt_hash}:{text_hash}")
    generation = GenerationRun(
        archive_id=archive.id,
        build_run_id=run.id,
        role="REASONING",
        provider=planner.provider,
        model=planner.model,
        prompt_hash=prompt_hash,
        text_hash=text_hash,
        cache_key=cache_key,
        input_refs=[
            {"kind": "topic_archive", "id": archive.id, "revision": archive.revision},
            {"kind": "archive_build_run", "id": run.id, "input_version": run.input_version},
        ],
    )
    session.add(generation)
    await session.flush()
    try:
        output = await planner.plan(
            topic=archive.title,
            focus=archive.focus,
            operator_context=operator_context,
            backgrounds=backgrounds,
        )
        serialized = output.model_dump_json()
        generation.output_hash = _sha256(serialized)
        generation.parse_status = "VALID"
        generation.status = "SUCCEEDED"
        generation.finished_at = datetime.utcnow()
        return output, generation
    except Exception as exc:
        generation.parse_status = "FAILED"
        generation.status = "FAILED"
        generation.error_category = type(exc).__name__
        generation.error = str(exc)[:2000]
        generation.finished_at = datetime.utcnow()
        await session.flush()
        raise


async def run_archive_planning(
    session: AsyncSession,
    archive: TopicArchive,
    run: ArchiveBuildRun,
    planner: ArchivePlanner | None = None,
) -> ArchiveBuildRun:
    """Run Batch A through SearchPlan, falling back without conflating AI and system output."""
    selected_planner = planner or RuleBasedArchivePlanner()
    context = await ensure_archive_operator_context(session, archive)
    run = await run_archive_foundation(session, archive, run)
    if run.status == "FAILED":
        await selected_planner.close()
        return run

    planning_step = await get_build_step(session, run, "LEXICON")
    if planning_step.status == "COMPLETED":
        run.state = "SEARCHING"
        run.status = "PAUSED"
        await selected_planner.close()
        return run
    planning_step.status = "RUNNING"
    planning_step.started_at = datetime.utcnow()
    run.state = "LEXICON"
    run.status = "RUNNING"

    background_links = list(
        (
            await session.execute(
                select(ArchiveBackgroundLink).where(
                    ArchiveBackgroundLink.archive_id == archive.id,
                    ArchiveBackgroundLink.status == "ATTACHED",
                )
            )
        )
        .scalars()
        .all()
    )
    backgrounds = []
    for link in background_links:
        node = await session.get(BackgroundNode, link.node_id)
        if node:
            backgrounds.append(node.title)
    context_text = await render_operator_context(session, context)

    try:
        try:
            output, generation = await _execute_planner(
                session,
                archive,
                run,
                selected_planner,
                operator_context=context_text,
                backgrounds=backgrounds,
            )
            origin = selected_planner.origin
        except Exception:
            if isinstance(selected_planner, RuleBasedArchivePlanner):
                raise
            fallback = RuleBasedArchivePlanner()
            output, generation = await _execute_planner(
                session,
                archive,
                run,
                fallback,
                operator_context=context_text,
                backgrounds=backgrounds,
            )
            origin = fallback.origin
    except Exception as exc:
        planning_step.status = "FAILED"
        planning_step.error = str(exc)
        planning_step.finished_at = datetime.utcnow()
        run.status = "FAILED"
        run.error = str(exc)
        return run
    finally:
        await selected_planner.close()

    scope_version = (
        await session.execute(
            select(func.max(ArchiveScopeDraft.version)).where(
                ArchiveScopeDraft.archive_id == archive.id
            )
        )
    ).scalar_one() or 0
    scope_data = output.scope.model_dump()
    scope_data["ambiguities"] = scope_data["ambiguities"][:MAX_REVIEW_ITEMS]
    scope_draft = ArchiveScopeDraft(
        archive_id=archive.id,
        build_run_id=run.id,
        version=scope_version + 1,
        core_scope=scope_data["core_scope"],
        included_domains=scope_data["included_domains"],
        excluded_domains=scope_data["excluded_domains"],
        temporal_scope=scope_data["temporal_scope"],
        object_scope=scope_data["object_scope"],
        method_scope=scope_data["method_scope"],
        ambiguities=scope_data["ambiguities"],
        reasoning_summary=scope_data["reasoning_summary"],
        generated_by=origin,
    )
    session.add(scope_draft)
    await session.flush()

    plan_version = (
        await session.execute(
            select(func.max(ArchiveSearchPlan.version)).where(
                ArchiveSearchPlan.archive_id == archive.id
            )
        )
    ).scalar_one() or 0
    plan_data = output.search_plan.model_dump()
    search_plan = ArchiveSearchPlan(
        archive_id=archive.id,
        build_run_id=run.id,
        scope_draft_id=scope_draft.id,
        version=plan_version + 1,
        concepts=plan_data["concepts"],
        historical_vocabulary=plan_data["historical_vocabulary"],
        hard_exclusions=plan_data["hard_exclusions"],
        soft_exclusions=plan_data["soft_exclusions"],
        source_targets=plan_data["source_targets"],
        rationale=plan_data["rationale"],
        generated_by=origin,
    )
    session.add(search_plan)
    await session.flush()

    session.add_all(
        [
            AIProposal(
                archive_id=archive.id,
                build_run_id=run.id,
                generation_run_id=generation.id,
                proposal_type="SCOPE_DRAFT",
                target_type="ArchiveScopeDraft",
                target_id=scope_draft.id,
                payload={"version": scope_draft.version},
                confidence=1.0,
                impact="HIGH",
                origin=origin,
                status="AUTO_APPLIED",
            ),
            AIProposal(
                archive_id=archive.id,
                build_run_id=run.id,
                generation_run_id=generation.id,
                proposal_type="SEARCH_PLAN",
                target_type="ArchiveSearchPlan",
                target_id=search_plan.id,
                payload={"version": search_plan.version},
                confidence=1.0,
                impact="HIGH",
                origin=origin,
                status="AUTO_APPLIED",
            ),
        ]
    )
    await session.flush()

    review_ids = []
    for ambiguity in scope_data["ambiguities"][:MAX_REVIEW_ITEMS]:
        proposal = AIProposal(
            archive_id=archive.id,
            build_run_id=run.id,
            generation_run_id=generation.id,
            proposal_type="SCOPE_AMBIGUITY",
            target_type="ArchiveScopeDraft",
            target_id=scope_draft.id,
            payload=ambiguity,
            confidence=float(ambiguity["confidence"]),
            impact=ambiguity["impact"],
            origin=origin,
            status="NEEDS_REVIEW",
        )
        session.add(proposal)
        await session.flush()
        item = ReviewItem(
            archive_id=archive.id,
            build_run_id=run.id,
            proposal_id=proposal.id,
            item_type="SCOPE_DECISION",
            question=ambiguity["question"],
            options=[
                {"value": option, "label": option.replace("_", " ").title()}
                for option in ambiguity["options"]
            ],
            impact=ambiguity["impact"],
        )
        session.add(item)
        await session.flush()
        review_ids.append(item.id)

    planning_step.status = "COMPLETED"
    planning_step.finished_at = datetime.utcnow()
    planning_step.output_artifact = {
        "scope_draft_id": scope_draft.id,
        "search_plan_id": search_plan.id,
        "generation_run_id": generation.id,
        "review_item_ids": review_ids,
        "review_item_count": len(review_ids),
        "planner_origin": origin,
    }
    run.state = "SEARCHING"
    run.status = "PAUSED"
    await record_archive_revision(
        session,
        archive,
        "AUTO_PLANNING",
        f"自动形成 ScopeDraft v{scope_draft.version} 与 SearchPlan v{search_plan.version}；"
        f"{len(review_ids)} 个问题待审",
        planning_step.output_artifact,
    )
    return run
