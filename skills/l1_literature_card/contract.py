"""
L1 Literature Card Skill contract
"""

from enum import Enum
from pydantic import BaseModel, Field


class MissingReason(str, Enum):
    NOT_STATED = "NOT_STATED"          # 摘要中未提及该信息
    NOT_APPLICABLE = "NOT_APPLICABLE"  # 与本研究类型不相关
    PARSE_FAILED = "PARSE_FAILED"      # LLM 未能可靠提取


class L1Input(BaseModel):
    work_id: str
    record_id: str
    title: str
    authors: list[str]
    abstract: str
    journal: str | None = None
    publication_date: str | None = None
    evidence_level: str = Field(pattern="^E[1-5]$")
    snapshot_id: int | None = None  # links artifact to the SourceSnapshot used

    class Config:
        json_schema_extra = {
            "example": {
                "work_id": "W001",
                "record_id": "R001",
                "title": "Discovery of covalent inhibitors targeting KRASG12C",
                "authors": ["Smith J", "Chen L"],
                "abstract": "KRAS mutations are prevalent in cancer...",
                "evidence_level": "E1",
            }
        }


class L1Output(BaseModel):
    """
    L1 Skill 输出

    必须简洁，不得包含详细方法、局限性等全文级信息。
    research_object/major_method/author_reported_result 允许为 None，
    但为 None 时必须同时提供对应的 *_missing_reason。
    evidence_spans 为每个非 None 字段提供摘要原文依据（逐字引用）。
    """

    one_sentence: str = Field(
        max_length=200,
        description="一句话总结研究核心贡献",
    )

    tags: list[str] = Field(
        max_length=5,
        description="3-5个标签，例如研究类型、领域、技术",
    )

    research_object: str | None = Field(
        None,
        max_length=150,
        description="研究的主要对象（蛋白、化合物、细胞系等），摘要中未提及时为 null",
    )
    research_object_missing_reason: MissingReason | None = None

    major_method: str | None = Field(
        None,
        max_length=150,
        description="主要方法或技术平台，摘要中未提及时为 null",
    )
    major_method_missing_reason: MissingReason | None = None

    author_reported_result: str | None = Field(
        None,
        max_length=200,
        description="作者报告的主要结果（来自 Abstract），摘要中未提及时为 null",
    )
    author_reported_result_missing_reason: MissingReason | None = None

    evidence_spans: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "field_name → verbatim quote from abstract. "
            "For each non-null field above, copy the exact sentence(s) that support it."
        ),
    )

    class Config:
        json_schema_extra = {
            "example": {
                "one_sentence": "开发了针对KRASG12C突变的共价抑制剂",
                "tags": ["covalent inhibitor", "KRAS", "cancer"],
                "research_object": "KRASG12C mutant protein",
                "research_object_missing_reason": None,
                "major_method": "structure-based drug design",
                "major_method_missing_reason": None,
                "author_reported_result": "Lead compound showed IC50 of 8 nM",
                "author_reported_result_missing_reason": None,
                "evidence_spans": {
                    "research_object": "KRAS mutations drive tumor growth",
                    "major_method": "We used structure-based design",
                    "author_reported_result": "IC50 of 8 nM was measured",
                },
            }
        }


EVIDENCE_PERMISSION = {
    "E0": [],
    "E1": ["one_sentence", "tags", "research_object", "major_method", "author_reported_result"],
    "E2": ["one_sentence", "tags", "research_object", "major_method", "author_reported_result"],
    "E3": ["one_sentence", "tags", "research_object", "major_method", "author_reported_result"],
    "E4": ["one_sentence", "tags", "research_object", "major_method", "author_reported_result"],
    "E5": ["one_sentence", "tags", "research_object", "major_method", "author_reported_result"],
}


def validate_evidence_permission(evidence_level: str, output: L1Output) -> tuple[bool, list[str]]:
    if evidence_level not in EVIDENCE_PERMISSION:
        return False, [f"Unknown evidence level: {evidence_level}"]
    if evidence_level == "E0":
        return False, ["E0 (metadata only) cannot generate L1 card"]
    return True, []
