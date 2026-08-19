"""
L1 Literature Card 生成器
"""

import hashlib
import uuid
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..llm.client import LLMClient
from ..core.models import Record, SourceSnapshot, AnalysisArtifact
from skills.l1_literature_card.contract import L1Input, L1Output, SKILL_VERSION, SCHEMA_VERSION
from skills.l1_literature_card.validator import validate_l1_output


SYSTEM_PROMPT = """You are extracting key information from a scientific abstract to create a quick-scan literature card.

Your output will be used by researchers to decide (in 10-20 seconds) whether they want to read the full paper.

Constraints:
- ALL information must come directly from the abstract
- NO detailed protocols, limitations, future work, or citation context
- Keep it concise — this is for quick scanning, not deep reading
- If a field's information is not explicitly in the abstract, set it to null and set *_missing_reason to "NOT_STATED"
- For evidence_spans: copy the exact sentence(s) from the abstract that support each non-null field
"""


def build_prompt(input_data: L1Input) -> str:
    authors_str = ", ".join(input_data.authors[:3])
    if len(input_data.authors) > 3:
        authors_str += f" et al. ({len(input_data.authors)} total)"

    return f"""Title: {input_data.title}

Authors: {authors_str}

Journal: {input_data.journal or "Not specified"}

Date: {input_data.publication_date or "Not specified"}

Abstract:
{input_data.abstract}

---

Extract the following information and output as JSON:

1. one_sentence: Single sentence summarizing core contribution (HARD LIMIT: 200 chars)
2. tags: 3-5 tags (research type, field, technique)
3. research_object: Main study object (protein, compound, cell line, etc. — max 150 chars).
   If not mentioned in abstract: set to null, set research_object_missing_reason to "NOT_STATED".
4. major_method: Primary methods/platforms (max 150 chars).
   If not mentioned: null + major_method_missing_reason = "NOT_STATED".
5. author_reported_result: Main result from abstract (max 200 chars, prefer quantitative).
   If not present: null + author_reported_result_missing_reason = "NOT_STATED".
6. evidence_spans: For each non-null field above, copy the exact sentence(s) from the abstract
   that support it. Keys: "research_object", "major_method", "author_reported_result".
   Only include keys for non-null fields.

CRITICAL:
- one_sentence: ≤200 characters
- research_object: ≤150 characters
- major_method: ≤150 characters
- author_reported_result: ≤200 characters
- evidence_spans values must be copied verbatim from the abstract above
"""


async def generate_l1_card(input_data: L1Input, llm_client: LLMClient) -> L1Output:
    """Generate and validate an L1 Literature Card."""
    prompt = build_prompt(input_data)
    output = await llm_client.call_with_schema(
        prompt=prompt,
        schema=L1Output,
        system_prompt=SYSTEM_PROMPT,
    )
    validate_l1_output(input_data, output)
    return output


async def run_l1(
    session: AsyncSession,
    record: Record,
    snapshot: SourceSnapshot,
    llm_client: LLMClient,
) -> AnalysisArtifact:
    """
    Single entry point for L1 generation + persistence.

    Enforces snapshot integrity BEFORE calling the LLM: the snapshot must
    belong to this record, and its analysis_text must match its recorded
    analysis_hash. This guarantees the AnalysisArtifact's snapshot_id is
    provably the text that was actually analyzed, not just a label attached
    after the fact.
    """
    if snapshot.record_id != record.id:
        raise ValueError(
            f"Snapshot {snapshot.id} belongs to record {snapshot.record_id}, not {record.id}"
        )
    if not snapshot.analysis_text:
        raise ValueError(f"Snapshot {snapshot.id} has no analysis_text to extract from")
    if snapshot.analysis_hash:
        actual_hash = hashlib.sha256(snapshot.analysis_text.encode()).hexdigest()
        if actual_hash != snapshot.analysis_hash:
            raise ValueError(
                f"Snapshot {snapshot.id} analysis_text does not match its recorded "
                f"analysis_hash — snapshot integrity check failed"
            )

    input_data = L1Input(
        work_id=f"W{record.work_id}",
        record_id=record.record_id,
        title=record.title,
        authors=[a["name"] for a in record.authors],
        abstract=snapshot.analysis_text,
        journal=record.journal,
        publication_date=record.publication_date.isoformat() if record.publication_date else None,
        evidence_level=record.evidence_level,
        snapshot_id=snapshot.id,
    )
    output = await generate_l1_card(input_data, llm_client)
    return await persist_l1_artifact(session, record, snapshot, output)


async def persist_l1_artifact(
    session: AsyncSession,
    record: Record,
    snapshot: SourceSnapshot,
    output: L1Output,
) -> AnalysisArtifact:
    """
    Persist an L1Output as an AnalysisArtifact, idempotently.

    Same record + same snapshot + same skill/schema version returns the
    existing artifact instead of creating a duplicate (safe to call again
    after a retry). A change in snapshot or skill/schema version creates a
    new artifact linked via supersedes_id, preserving history rather than
    overwriting it.
    """
    existing_stmt = (
        select(AnalysisArtifact)
        .where(
            AnalysisArtifact.record_id == record.id,
            AnalysisArtifact.snapshot_id == snapshot.id,
            AnalysisArtifact.analysis_type == "L1",
            AnalysisArtifact.skill_version == SKILL_VERSION,
            AnalysisArtifact.schema_version == SCHEMA_VERSION,
        )
        .order_by(AnalysisArtifact.created_at.desc())
    )
    existing = (await session.execute(existing_stmt)).scalars().first()
    if existing:
        return existing

    prior_stmt = (
        select(AnalysisArtifact)
        .where(
            AnalysisArtifact.record_id == record.id,
            AnalysisArtifact.analysis_type == "L1",
        )
        .order_by(AnalysisArtifact.created_at.desc())
    )
    prior = (await session.execute(prior_stmt)).scalars().first()

    markdown = render_l1_markdown(record, snapshot, output)
    artifact = AnalysisArtifact(
        artifact_id=uuid.uuid4().hex,
        record_id=record.id,
        snapshot_id=snapshot.id,
        analysis_type="L1",
        skill_version=SKILL_VERSION,
        schema_version=SCHEMA_VERSION,
        content=output.model_dump(),
        markdown=markdown,
        supersedes_id=prior.id if prior else None,
    )
    session.add(artifact)
    await session.flush()
    return artifact


def render_l1_markdown(
    record: Record,
    snapshot: SourceSnapshot,
    output: L1Output,
) -> str:
    """Render an L1Output to Markdown, grounding each field in its evidence span."""
    lines = [
        f"# {record.title}",
        "",
        f"> {output.one_sentence}",
        "",
        f"**Tags**: {', '.join(output.tags)}",
        "",
        "## L1 Card",
        "",
    ]

    def _field_line(label: str, value: str | None, reason, span: str | None) -> list[str]:
        if value is not None:
            result = [f"**{label}**: {value}"]
            if span:
                result.append(f"> *Evidence*: \"{span}\"")
        else:
            result = [f"**{label}**: *(not stated — {reason.value if reason else 'unknown'})*"]
        return result + [""]

    for row in _field_line(
        "Research Object",
        output.research_object,
        output.research_object_missing_reason,
        output.evidence_spans.get("research_object"),
    ):
        lines.append(row)

    for row in _field_line(
        "Major Method",
        output.major_method,
        output.major_method_missing_reason,
        output.evidence_spans.get("major_method"),
    ):
        lines.append(row)

    for row in _field_line(
        "Author-Reported Result",
        output.author_reported_result,
        output.author_reported_result_missing_reason,
        output.evidence_spans.get("author_reported_result"),
    ):
        lines.append(row)

    # Provenance footer
    snap_note = (
        f"snapshot `{snapshot.id}` (hash: `{snapshot.analysis_hash or 'n/a'}`, "
        f"source: {snapshot.source_name}, fetched: {snapshot.fetched_at.date()})"
    )
    lines += [
        "---",
        f"*Evidence grounded in {snap_note}*",
        f"*Generated: {datetime.utcnow().isoformat()}*",
    ]
    return "\n".join(lines)
