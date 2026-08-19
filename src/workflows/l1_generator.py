"""
L1 Literature Card 生成器
"""

import hashlib
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession

from ..llm.client import LLMClient
from ..core.models import Record, SourceSnapshot, AnalysisArtifact
from skills.l1_literature_card.contract import L1Input, L1Output
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


async def persist_l1_artifact(
    session: AsyncSession,
    record: Record,
    snapshot: SourceSnapshot | None,
    output: L1Output,
) -> AnalysisArtifact:
    """
    Persist an L1Output as an AnalysisArtifact and render its Markdown.

    Links the artifact to the SourceSnapshot used during generation so the
    analysis can always be traced back to the exact abstract text it used.
    """
    artifact_id = f"A_L1_{record.record_id}"
    markdown = render_l1_markdown(record, snapshot, output)

    artifact = AnalysisArtifact(
        artifact_id=artifact_id,
        record_id=record.id,
        snapshot_id=snapshot.id if snapshot else None,
        analysis_type="L1",
        content=output.model_dump(),
        markdown=markdown,
    )
    session.add(artifact)
    await session.flush()
    return artifact


def render_l1_markdown(
    record: Record,
    snapshot: SourceSnapshot | None,
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
        f"snapshot `{snapshot.id}` (hash: `{snapshot.content_hash or 'n/a'}`, "
        f"source: {snapshot.source_name}, fetched: {snapshot.fetched_at.date()})"
        if snapshot
        else "no snapshot"
    )
    lines += [
        "---",
        f"*Evidence grounded in {snap_note}*",
        f"*Generated: {datetime.utcnow().isoformat()}*",
    ]
    return "\n".join(lines)
