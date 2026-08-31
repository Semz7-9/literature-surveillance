"""Archive Automation Foundation: durable build state and shared background resolution."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.models import (
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
from ..llm.routing import AI_ROLES, AIContributionResult
from .archive import record_archive_revision

BUILD_STAGES = ("SCOPING", "BACKGROUND", "LEXICON", "SEARCHING", "ASSEMBLING")
OPERATOR_CONTRIBUTION_TYPES = {
    "OBSERVATION",
    "INTERPRETATION",
    "HEURISTIC",
    "PRINCIPLE",
    "OPEN_QUESTION",
}


BACKGROUND_CATALOG = {
    "Medicinal Chemistry": {
        "description": "How molecular structure is translated into useful biological activity.",
        "nodes": {
            "Structure–Activity Relationships": "Relationships between chemical structure, potency, selectivity, and developability.",
            "Target Engagement": "Principles connecting molecular design with durable and selective target engagement.",
        },
    },
    "Organic Chemistry": {
        "description": "Molecular reactivity, mechanism, and synthesis.",
        "nodes": {
            "Electrophile–Nucleophile Chemistry": "Reactivity framework for covalent bond formation between electrophiles and nucleophiles.",
            "Covalent Bond Formation": "Mechanistic and structural constraints governing covalent reactions.",
        },
    },
    "Chemical Biology": {
        "description": "Chemical approaches for interrogating biological systems.",
        "nodes": {
            "Chemoproteomics": "Proteome-scale methods for observing ligand engagement and reactive-site selectivity.",
        },
    },
    "Physical Chemistry": {
        "description": "Quantitative principles governing molecular behavior and reaction processes.",
        "nodes": {
            "Reaction Kinetics": "Rate laws and kinetic parameters that separate binding from chemical reaction steps.",
        },
    },
    "Computational Chemistry": {
        "description": "Computational representations and models of molecular systems.",
        "nodes": {
            "Molecular Modeling": "Computational methods for molecular structure, interaction, and reactivity.",
        },
    },
    "Pharmacology": {
        "description": "Drug action, exposure, efficacy, and safety in biological systems.",
        "nodes": {
            "PK/PD": "Relationships among exposure, target modulation, efficacy, and toxicity.",
        },
    },
    "Molecular Biology": {
        "description": "Molecular mechanisms underlying biological function.",
        "nodes": {
            "Protein Function": "How protein structure, state, and interactions determine biological function.",
        },
    },
}


@dataclass(frozen=True)
class BackgroundCandidate:
    node_id: int
    confidence: str
    relevance: float
    reason: str


class BackgroundResolver(Protocol):
    async def resolve(
        self,
        session: AsyncSession,
        *,
        topic: str,
        focus: str,
        provisional_scope: ArchiveScope,
    ) -> list[BackgroundCandidate]: ...


async def ensure_background_library(session: AsyncSession) -> dict[str, BackgroundNode]:
    """Idempotently seed the small shared library needed by the resolver skeleton."""
    nodes: dict[str, BackgroundNode] = {}
    for title, data in BACKGROUND_CATALOG.items():
        profile = (
            await session.execute(select(BackgroundProfile).where(BackgroundProfile.title == title))
        ).scalar_one_or_none()
        if profile is None:
            profile = BackgroundProfile(
                title=title,
                discipline=title,
                description=data["description"],
            )
            session.add(profile)
            await session.flush()
        for node_title, summary in data["nodes"].items():
            node = (
                await session.execute(
                    select(BackgroundNode).where(
                        BackgroundNode.profile_id == profile.id,
                        BackgroundNode.title == node_title,
                    )
                )
            ).scalar_one_or_none()
            if node is None:
                node = BackgroundNode(
                    profile_id=profile.id,
                    title=node_title,
                    canonical_summary=summary,
                )
                session.add(node)
                await session.flush()
            nodes[node_title] = node
    return nodes


class RuleBasedBackgroundResolver:
    """Deterministic resolver boundary used until role-routed AI is enabled."""

    async def resolve(
        self,
        session: AsyncSession,
        *,
        topic: str,
        focus: str,
        provisional_scope: ArchiveScope,
    ) -> list[BackgroundCandidate]:
        nodes = await ensure_background_library(session)
        text = f"{topic} {focus} {' '.join(provisional_scope.core_concepts)}".lower()
        scores: dict[str, tuple[str, float, str]] = {}

        def choose(node: str, confidence: str, relevance: float, reason: str) -> None:
            current = scores.get(node)
            if current is None or relevance > current[1]:
                scores[node] = (confidence, relevance, reason)

        if any(term in text for term in ("covalent inhibitor", "共价抑制", "covalent drug")):
            choose("Electrophile–Nucleophile Chemistry", "HIGH", 0.96, "Topic 明确涉及共价反应设计")
            choose("Reaction Kinetics", "HIGH", 0.91, "共价抑制需要区分结合与反应动力学")
            choose("Structure–Activity Relationships", "HIGH", 0.89, "Topic 聚焦 inhibitor design")
            choose("Chemoproteomics", "MEDIUM", 0.68, "可能需要评估蛋白组层面的选择性")
            choose("PK/PD", "LOW", 0.35, "可能相关，但当前 Topic/Focus 证据不足")
        keyword_rules = (
            (("medicinal", "drug design", "药物化学"), "Structure–Activity Relationships"),
            (
                ("electrophile", "nucleophile", "covalent", "亲电", "亲核"),
                "Electrophile–Nucleophile Chemistry",
            ),
            (("kinetic", "mechanism", "动力学", "机制"), "Reaction Kinetics"),
            (("chemoproteom", "chemical biology", "化学生物"), "Chemoproteomics"),
            (("pharmac", "pk/pd", "药理"), "PK/PD"),
            (("computational", "modeling", "计算化学"), "Molecular Modeling"),
            (("protein", "molecular biology", "蛋白", "分子生物"), "Protein Function"),
        )
        for keywords, node_title in keyword_rules:
            if any(keyword in text for keyword in keywords):
                choose(node_title, "HIGH", 0.82, f"Topic/Focus 命中 {node_title} 相关语义")
        return [
            BackgroundCandidate(nodes[title].id, confidence, relevance, reason)
            for title, (confidence, relevance, reason) in sorted(
                scores.items(), key=lambda item: item[1][1], reverse=True
            )
        ]


async def create_archive_build_run(
    session: AsyncSession,
    archive: TopicArchive,
) -> ArchiveBuildRun:
    latest_version = (
        await session.execute(
            select(func.max(ArchiveBuildRun.input_version)).where(
                ArchiveBuildRun.archive_id == archive.id
            )
        )
    ).scalar_one() or 0
    run = ArchiveBuildRun(
        archive_id=archive.id,
        state="CREATED",
        status="PENDING",
        input_version=latest_version + 1,
    )
    session.add(run)
    await session.flush()
    session.add_all(
        [
            ArchiveBuildStep(
                run_id=run.id,
                stage=stage,
                status="PENDING",
                input_version=run.input_version,
            )
            for stage in BUILD_STAGES
        ]
    )
    await session.flush()
    return run


async def get_build_step(
    session: AsyncSession, run: ArchiveBuildRun, stage: str
) -> ArchiveBuildStep:
    latest = (
        await session.execute(
            select(ArchiveBuildStep)
            .where(
                ArchiveBuildStep.run_id == run.id,
                ArchiveBuildStep.stage == stage,
            )
            .order_by(ArchiveBuildStep.attempt.desc())
            .limit(1)
        )
    ).scalar_one()
    if latest.status == "FAILED":
        latest = ArchiveBuildStep(
            run_id=run.id,
            stage=stage,
            attempt=latest.attempt + 1,
            status="PENDING",
            input_version=run.input_version,
        )
        session.add(latest)
        await session.flush()
    return latest


async def run_archive_foundation(
    session: AsyncSession,
    archive: TopicArchive,
    run: ArchiveBuildRun,
    resolver: BackgroundResolver | None = None,
) -> ArchiveBuildRun:
    """Run A1+A2 synchronously; later stages remain durable and resumable."""
    resolver = resolver or RuleBasedBackgroundResolver()
    run.status = "RUNNING"
    run.started_at = run.started_at or datetime.utcnow()
    run.error = None

    scope_step = await get_build_step(session, run, "SCOPING")
    if scope_step.status != "COMPLETED":
        run.state = "SCOPING"
        scope_step.status = "RUNNING"
        scope_step.started_at = datetime.utcnow()
        try:
            version = (
                await session.execute(
                    select(func.max(ArchiveScope.version)).where(
                        ArchiveScope.archive_id == archive.id
                    )
                )
            ).scalar_one() or 0
            scope = ArchiveScope(
                archive_id=archive.id,
                version=version + 1,
                core_concepts=[archive.title],
                background_concepts=[archive.focus] if archive.focus else [],
                exclusions=[],
                notes="Auto Archive Builder provisional scope；等待 Automatic Scope 批次深化。",
            )
            session.add(scope)
            await session.flush()
            scope_step.output_artifact = {
                "scope_id": scope.id,
                "scope_version": scope.version,
                "provisional": True,
                "topic": archive.title,
                "focus": archive.focus,
            }
            scope_step.status = "COMPLETED"
            scope_step.finished_at = datetime.utcnow()
            await record_archive_revision(
                session,
                archive,
                "AUTO_SCOPE_DRAFT",
                "自动建立 provisional Scope",
                scope_step.output_artifact,
            )
        except Exception as exc:
            scope_step.status = "FAILED"
            scope_step.error = str(exc)
            scope_step.finished_at = datetime.utcnow()
            run.status = "FAILED"
            run.error = str(exc)
            return run
    else:
        scope = await session.get(ArchiveScope, scope_step.output_artifact.get("scope_id"))

    background_step = await get_build_step(session, run, "BACKGROUND")
    if background_step.status != "COMPLETED":
        run.state = "BACKGROUND"
        background_step.status = "RUNNING"
        background_step.started_at = datetime.utcnow()
        try:
            candidates = await resolver.resolve(
                session,
                topic=archive.title,
                focus=archive.focus,
                provisional_scope=scope,
            )
            attached = candidate_count = 0
            link_ids: list[int] = []
            for candidate in candidates:
                if candidate.confidence == "LOW":
                    continue
                link = (
                    await session.execute(
                        select(ArchiveBackgroundLink).where(
                            ArchiveBackgroundLink.archive_id == archive.id,
                            ArchiveBackgroundLink.node_id == candidate.node_id,
                        )
                    )
                ).scalar_one_or_none()
                if link is None:
                    status = "ATTACHED" if candidate.confidence == "HIGH" else "CANDIDATE"
                    link = ArchiveBackgroundLink(
                        archive_id=archive.id,
                        node_id=candidate.node_id,
                        relevance=candidate.relevance,
                        confidence=candidate.confidence,
                        selection_reason=candidate.reason,
                        selected_by="BACKGROUND_RESOLVER",
                        status=status,
                    )
                    session.add(link)
                    await session.flush()
                link_ids.append(link.id)
                attached += int(link.status == "ATTACHED")
                candidate_count += int(link.status == "CANDIDATE")
            background_step.output_artifact = {
                "link_ids": link_ids,
                "attached": attached,
                "candidates": candidate_count,
                "resolver": type(resolver).__name__,
            }
            background_step.status = "COMPLETED"
            background_step.finished_at = datetime.utcnow()
            await record_archive_revision(
                session,
                archive,
                "AUTO_BACKGROUND",
                f"Background Auto：挂接 {attached} 个共享节点，{candidate_count} 个待确认",
                background_step.output_artifact,
            )
        except Exception as exc:
            background_step.status = "FAILED"
            background_step.error = str(exc)
            background_step.finished_at = datetime.utcnow()
            run.status = "FAILED"
            run.error = str(exc)
            return run

    # A3 begins at LEXICON in the next batch. Keeping this explicit makes the
    # persisted run resumable rather than pretending the whole Archive is ready.
    run.state = "LEXICON"
    run.status = "PAUSED"
    return run


async def add_operator_contribution(
    session: AsyncSession,
    *,
    archive_id: int,
    node_id: int,
    contribution_type: str,
    raw_text: str,
) -> BackgroundOperatorContribution:
    if contribution_type not in OPERATOR_CONTRIBUTION_TYPES:
        raise ValueError("不支持的 Operator Contribution 类型")
    if not raw_text.strip():
        raise ValueError("原始想法不能为空")
    contribution = BackgroundOperatorContribution(
        archive_id=archive_id,
        node_id=node_id,
        contribution_type=contribution_type,
        raw_text=raw_text.strip(),
    )
    session.add(contribution)
    await session.flush()
    return contribution


async def record_ai_contribution(
    session: AsyncSession,
    *,
    archive_id: int | None,
    node_id: int,
    result: AIContributionResult,
    input_refs: list[dict],
) -> BackgroundAIContribution:
    if result.role not in AI_ROLES:
        raise ValueError("不支持的 AI Contribution role")
    contribution = BackgroundAIContribution(
        archive_id=archive_id,
        node_id=node_id,
        provider=result.provider,
        model=result.model,
        role=result.role,
        input_refs=input_refs,
        output=result.output,
    )
    session.add(contribution)
    await session.flush()
    return contribution
