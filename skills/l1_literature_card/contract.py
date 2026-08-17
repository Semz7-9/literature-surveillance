"""
L1 Literature Card Skill

将 Abstract 转换为分级阅读卡片的第一层：
- 一句话总结
- 标签
- 研究对象
- 主要方法
- 作者报告的结果

约束：
- 只能用于 E1+ (有 Abstract)
- 输出必须通过 schema validation
- 不得包含全文级信息（methods detail, limitations, citation context）
"""

from pydantic import BaseModel, Field


class L1Input(BaseModel):
    """L1 Skill 输入"""

    work_id: str
    record_id: str
    title: str
    authors: list[str]
    abstract: str
    journal: str | None = None
    publication_date: str | None = None

    # 必须是 E1+
    evidence_level: str = Field(pattern="^E[1-5]$")

    class Config:
        json_schema_extra = {
            "example": {
                "work_id": "W001",
                "record_id": "R001",
                "title": "Discovery of covalent inhibitors targeting KRASG12C",
                "authors": ["Smith J", "Chen L", "Johnson M"],
                "abstract": "KRAS mutations are prevalent in cancer...",
                "journal": "Nature",
                "publication_date": "2024-03-15",
                "evidence_level": "E1",
            }
        }


class L1Output(BaseModel):
    """
    L1 Skill 输出

    必须简洁，不得包含详细方法、局限性等全文级信息
    """

    one_sentence: str = Field(
        max_length=200,
        description="一句话总结研究核心贡献",
    )

    tags: list[str] = Field(
        max_length=5,
        description="3-5个标签，例如研究类型、领域、技术",
    )

    research_object: str = Field(
        max_length=150,
        description="研究的主要对象（蛋白、化合物、细胞系等）",
    )

    major_method: str = Field(
        max_length=150,
        description="主要方法或技术平台",
    )

    author_reported_result: str = Field(
        max_length=200,
        description="作者报告的主要结果（来自 Abstract）",
    )

    visual_status: str = Field(
        pattern="^(public|unavailable)$",
        description="图片可获得性",
    )

    class Config:
        json_schema_extra = {
            "example": {
                "one_sentence": "开发了针对KRASG12C突变的共价抑制剂，在临床前模型中显示抗肿瘤活性",
                "tags": ["covalent inhibitor", "KRAS", "cancer", "drug discovery"],
                "research_object": "KRASG12C mutant protein",
                "major_method": "structure-based drug design, biochemical assay, xenograft model",
                "author_reported_result": "Lead compound showed IC50 of 8 nM and tumor regression in mice",
                "visual_status": "public",
            }
        }


# Evidence Level 到允许字段的映射
EVIDENCE_PERMISSION = {
    "E0": [],  # E0 不能生成 L1
    "E1": ["one_sentence", "tags", "research_object", "major_method", "author_reported_result", "visual_status"],
    "E2": ["one_sentence", "tags", "research_object", "major_method", "author_reported_result", "visual_status"],
    "E3": ["one_sentence", "tags", "research_object", "major_method", "author_reported_result", "visual_status"],
    "E4": ["one_sentence", "tags", "research_object", "major_method", "author_reported_result", "visual_status"],
    "E5": ["one_sentence", "tags", "research_object", "major_method", "author_reported_result", "visual_status"],
}


def validate_evidence_permission(evidence_level: str, output: L1Output) -> tuple[bool, list[str]]:
    """
    验证当前 Evidence Level 是否允许输出这些字段

    Returns:
        (is_valid, violations)
    """
    if evidence_level not in EVIDENCE_PERMISSION:
        return False, [f"Unknown evidence level: {evidence_level}"]

    if evidence_level == "E0":
        return False, ["E0 (metadata only) cannot generate L1 card"]

    # E1+ 都允许 L1 的所有字段
    return True, []
