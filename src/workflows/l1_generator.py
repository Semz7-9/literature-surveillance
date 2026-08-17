"""
L1 Literature Card 生成器

将 Abstract 转换为 L1 卡片的完整实现
"""

from ..llm.client import LLMClient
from skills.l1_literature_card.contract import L1Input, L1Output
from skills.l1_literature_card.validator import validate_l1_output


SYSTEM_PROMPT = """You are extracting key information from a scientific abstract to create a quick-scan literature card.

Your output will be used by researchers to decide (in 10-20 seconds) whether they want to read the full paper.

Constraints:
- ALL information must come from the abstract
- NO detailed protocols, limitations, future work, or citation context
- Keep it concise - this is for quick scanning, not deep reading
- If information is not in the abstract, use "not specified" rather than guessing
"""


def build_prompt(input_data: L1Input) -> str:
    """构建 L1 生成的 prompt"""
    authors_str = ", ".join(input_data.authors[:3])
    if len(input_data.authors) > 3:
        authors_str += f" et al. ({len(input_data.authors)} total)"

    prompt = f"""Title: {input_data.title}

Authors: {authors_str}

Journal: {input_data.journal or "Not specified"}

Date: {input_data.publication_date or "Not specified"}

Abstract:
{input_data.abstract}

---

Extract the following information and output as JSON:

1. one_sentence: A single sentence summarizing the core contribution (HARD LIMIT: 200 characters maximum including spaces and punctuation)
2. tags: 3-5 tags (research type, field, technique) - keep them concise
3. research_object: Main object of study (max 150 chars: protein, compound, cell line, pathway, etc.)
4. major_method: Primary methods or platforms used (max 150 chars, comma-separated if multiple)
5. author_reported_result: Main result as reported in abstract (HARD LIMIT: 200 characters, keep quantitative if available)
6. visual_status: "public" if this is an open-access journal/preprint, "unavailable" otherwise

CRITICAL LENGTH CONSTRAINTS:
- one_sentence: MUST be ≤200 characters (count carefully before outputting)
- research_object: MUST be ≤150 characters
- major_method: MUST be ≤150 characters
- author_reported_result: MUST be ≤200 characters

Remember: Extract only from the abstract. Do not infer details not explicitly stated.
"""

    return prompt


async def generate_l1_card(
    input_data: L1Input,
    llm_client: LLMClient,
) -> L1Output:
    """
    生成 L1 Literature Card

    Args:
        input_data: L1 输入数据
        llm_client: LLM 客户端

    Returns:
        验证后的 L1 输出

    Raises:
        ValidationError: 如果输出不符合约束
    """
    # 构建 prompt
    prompt = build_prompt(input_data)

    # 调用 LLM
    output = await llm_client.call_with_schema(
        prompt=prompt,
        schema=L1Output,
        system_prompt=SYSTEM_PROMPT,
    )

    # 验证
    validate_l1_output(input_data, output)

    return output
