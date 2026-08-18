"""
核心数据模型

关键设计决策：
1. Work/Record 分离：一个 Work 可以有多个 Record (preprint v1/v2, VoR, etc.)
2. Evidence/Publication/Claim 三维独立
3. Identity resolution 本身需要 provenance
4. null/unknown/unresolved 是合法状态
"""

from datetime import datetime
from enum import Enum
from typing import Optional
from sqlalchemy import (
    String,
    Integer,
    Float,
    DateTime,
    Text,
    ForeignKey,
    JSON,
    Index,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def normalize_doi(raw: str) -> str:
    """DOI 规范化：统一大小写、去除 URL 前缀和空白

    所有入库/查询路径必须先经过这个函数，否则同一篇文献的
    'https://doi.org/10.1038/X'、'10.1038/x'、' 10.1038/X ' 会被当成不同实体。
    """
    doi = raw.strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if doi.lower().startswith(prefix):
            doi = doi[len(prefix):]
            break
    return doi.strip().lower()


# ============================================================================
# Evidence & Status Enums
# ============================================================================


class EvidenceLevel(str, Enum):
    """我们看到了什么材料（认识论维度）"""

    E0 = "E0"  # Metadata only (title, authors, journal, date, DOI)
    E1 = "E1"  # + Abstract
    E2 = "E2"  # + Preprint full text (未经同行评议)
    E3 = "E3"  # + Accepted Manuscript
    E4 = "E4"  # + Version of Record
    E5 = "E5"  # + Supplementary / Data / Code


class PublicationStatus(str, Enum):
    """论文的出版状态（制度/流程维度）"""

    PREPRINT = "PREPRINT"
    ACTIVE = "ACTIVE"
    CORRECTED = "CORRECTED"
    ERRATUM = "ERRATUM"
    EXPRESSION_OF_CONCERN = "EXPRESSION_OF_CONCERN"
    PARTIALLY_RETRACTED = "PARTIALLY_RETRACTED"
    RETRACTED = "RETRACTED"
    WITHDRAWN = "WITHDRAWN"


class ClaimStatus(str, Enum):
    """基于论文的断言是否有效（知识有效性维度）"""

    VALID = "VALID"
    RECHECK_REQUIRED = "RECHECK_REQUIRED"
    DISPUTED = "DISPUTED"
    INVALIDATED = "INVALIDATED"
    UNVERIFIED = "UNVERIFIED"


class IdentityEvidenceType(str, Enum):
    """Work identity 判断的证据类型

    每个值描述实际发生的证据，不能脱离具体判断场景复用：
    - DOI_EXACT: 该 DOI 已经在某个 Work 下注册过（同一个标识符）
    - EXPLICIT_CROSSREF_RELATION: Crossref relation 字段显式声明了版本/预印本关系
    - PUBLISHER_RELATION: 出版商元数据（非 Crossref relation）声明的关系
    - EXACT_TITLE_FIRST_AUTHOR: 标题完全相同 + 第一作者完全相同的模糊匹配。
      命名里不含 YEAR，因为当前 _fuzzy_match_work() 根本没有比较年份——
      provenance 必须描述"实际用了什么证据"，不能预支未来才会加的信号。
    - NEW_WORK: 未匹配到任何已有 Work，新建
    - MANUAL_CONFIRMATION: 人工审核后确认
    """

    DOI_EXACT = "DOI_EXACT"
    EXPLICIT_CROSSREF_RELATION = "EXPLICIT_CROSSREF_RELATION"
    PUBLISHER_RELATION = "PUBLISHER_RELATION"
    EXACT_TITLE_FIRST_AUTHOR = "EXACT_TITLE_FIRST_AUTHOR"
    NEW_WORK = "NEW_WORK"
    MANUAL_CONFIRMATION = "MANUAL_CONFIRMATION"


class IdentityStatus(str, Enum):
    """Identity edge 的确认状态"""

    CANDIDATE = "CANDIDATE"  # 系统提议，等待审核
    PROVISIONAL = "PROVISIONAL"  # 系统自动确认，但可能错
    CONFIRMED = "CONFIRMED"  # 人工确认或高置信度自动确认
    REJECTED = "REJECTED"  # 明确不是同一个 Work


# ============================================================================
# Core Entities
# ============================================================================


class Work(Base):
    """
    学术作品的抽象概念

    一个 Work 可能有多个 Record：
    - ChemRxiv v1
    - ChemRxiv v2
    - Journal VoR
    - Correction
    """

    __tablename__ = "works"

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    # 核心元数据
    title: Mapped[str] = mapped_column(Text)
    canonical_doi: Mapped[Optional[str]] = mapped_column(String(255), index=True)

    # 时间
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # 关系
    records: Mapped[list["Record"]] = relationship(back_populates="work")
    identifiers: Mapped[list["WorkIdentifier"]] = relationship(back_populates="work")


class Record(Base):
    """
    Work 的具体实例化

    同一个 Work 的不同版本、不同出版形式
    """

    __tablename__ = "records"

    id: Mapped[int] = mapped_column(primary_key=True)
    record_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # 创建 Record 时 Work 可能尚未解析，resolve_or_create_work() 之后才回填
    work_id: Mapped[Optional[int]] = mapped_column(ForeignKey("works.id"), index=True)

    # 元数据
    title: Mapped[str] = mapped_column(Text)
    authors: Mapped[dict] = mapped_column(JSON)  # [{name, affiliation, orcid}, ...]
    journal: Mapped[Optional[str]] = mapped_column(String(255))
    # publication_date 是 raw_date_parts 按 precision 补全后的派生值，仅用于排序/展示，
    # 不能反过来当作"我们确切知道这一天发表"的证据
    publication_date: Mapped[Optional[datetime]] = mapped_column(DateTime)
    publication_date_precision: Mapped[Optional[str]] = mapped_column(
        String(8)
    )  # day, month, year
    raw_date_parts: Mapped[Optional[list]] = mapped_column(JSON)  # Crossref date-parts 原样保留
    doi: Mapped[Optional[str]] = mapped_column(String(255), unique=True, index=True)

    # Abstract
    abstract: Mapped[Optional[str]] = mapped_column(Text)

    # 三维状态
    evidence_level: Mapped[str] = mapped_column(String(2), default=EvidenceLevel.E0.value)
    publication_status: Mapped[str] = mapped_column(
        String(32), default=PublicationStatus.ACTIVE.value
    )

    # 其他元数据
    extra_metadata: Mapped[dict] = mapped_column(JSON, default=dict)  # pmid, pmcid, arxiv_id, etc.

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # 关系
    work: Mapped["Work"] = relationship(back_populates="records")

    __table_args__ = (Index("ix_records_publication_date", "publication_date"),)


class WorkIdentifier(Base):
    """
    Work 的各种标识符

    一个 Work 可能有多个标识符：
    - preprint DOI
    - VoR DOI
    - PMID
    - PMCID
    - arXiv ID
    """

    __tablename__ = "work_identifiers"

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), index=True)

    identifier_type: Mapped[str] = mapped_column(String(32))  # doi, pmid, pmcid, arxiv, etc.
    # 调用方负责在写入前 normalize；目前只有 doi 有 normalize_doi() 保证。
    # pmid/pmcid/arxiv 等其他 type 暂未定义各自的 canonicalization 规则，
    # 在那之前 unique 约束只对已经 normalize 的 identifier_type 真正有意义。
    identifier_value: Mapped[str] = mapped_column(String(255), index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # 关系
    work: Mapped["Work"] = relationship(back_populates="identifiers")

    __table_args__ = (
        UniqueConstraint(
            "identifier_type", "identifier_value", name="uq_work_identifier_type_value"
        ),
    )


class IdentityEdge(Base):
    """
    Work identity resolution 的证据

    "这两个 Record 属于同一个 Work" 本身是一个需要证据支持的判断
    """

    __tablename__ = "identity_edges"

    id: Mapped[int] = mapped_column(primary_key=True)

    source_record_id: Mapped[int] = mapped_column(ForeignKey("records.id"), index=True)
    target_work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), index=True)

    # 证据
    evidence_type: Mapped[str] = mapped_column(String(64))
    confidence: Mapped[Optional[float]] = mapped_column(Float)
    evidence_detail: Mapped[dict] = mapped_column(JSON, default=dict)

    # 状态
    status: Mapped[str] = mapped_column(
        String(16), default=IdentityStatus.CANDIDATE.value, index=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        # 一个 Record 在任意时刻最多只能有一条生效的 CONFIRMED identity edge。
        # 没有这个约束，confirm_edge() 可以对同一个 record 确认两条指向不同
        # Work 的 edge：record.work_id 会指向后一次 confirm 的 Work，但前一条
        # edge 仍然显示 CONFIRMED——数据库里会同时存在两个"已确认"却互相矛盾
        # 的历史记录。旧的 CONFIRMED edge 需要先被 reject 才能确认新的。
        Index(
            "uq_identity_edges_one_confirmed_per_record",
            "source_record_id",
            unique=True,
            sqlite_where=text(f"status = '{IdentityStatus.CONFIRMED.value}'"),
        ),
    )


# ============================================================================
# Knowledge Extraction
# ============================================================================


class Claim(Base):
    """
    从文献中提取的具体断言

    每个 Claim 必须有：
    - 内容
    - 来源 Record
    - 位置（section, paragraph）
    - Evidence Level
    """

    __tablename__ = "claims"

    id: Mapped[int] = mapped_column(primary_key=True)
    claim_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    # 内容
    content: Mapped[str] = mapped_column(Text)
    claim_type: Mapped[str] = mapped_column(String(64))  # method, result, limitation, etc.

    # 来源
    source_record_id: Mapped[int] = mapped_column(ForeignKey("records.id"), index=True)
    location: Mapped[Optional[str]] = mapped_column(Text)  # section, page, paragraph
    source_evidence_level: Mapped[str] = mapped_column(String(2))

    # 状态：新提取的 Claim 默认未验证，只有人工/规则/验证流程可以升为 VALID
    status: Mapped[str] = mapped_column(String(32), default=ClaimStatus.UNVERIFIED.value)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class Relation(Base):
    """
    Work 之间的关系

    例如：
    - cites
    - extends
    - contradicts
    - applies_method_from
    """

    __tablename__ = "relations"

    id: Mapped[int] = mapped_column(primary_key=True)

    source_work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), index=True)
    target_work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), index=True)

    relation_type: Mapped[str] = mapped_column(String(64), index=True)
    evidence_record_id: Mapped[Optional[int]] = mapped_column(ForeignKey("records.id"))
    evidence_level: Mapped[str] = mapped_column(String(2))

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ============================================================================
# User State
# ============================================================================


class ReadingQueue(Base):
    """
    用户的阅读意图队列

    从 L0 → L1 → L2 → L3
    """

    __tablename__ = "reading_queue"

    id: Mapped[int] = mapped_column(primary_key=True)

    work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), index=True)
    requested_level: Mapped[str] = mapped_column(String(2))  # L0, L1, L2, L3
    priority: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[str] = mapped_column(
        String(16), default="pending", index=True
    )  # pending, processing, done

    requested_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    __table_args__ = (Index("ix_reading_queue_status_priority", "status", "priority"),)


class UserWorkState(Base):
    """
    用户对 Work 的状态标记

    Ignore / Keep / Archive Candidate
    """

    __tablename__ = "user_work_state"

    id: Mapped[int] = mapped_column(primary_key=True)

    work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), unique=True, index=True)
    state: Mapped[str] = mapped_column(String(16))  # ignore, keep, candidate
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)

    # match_reason：为什么出现在我面前
    match_reason: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


# ============================================================================
# Source Health
# ============================================================================


class Source(Base):
    """
    外部数据源

    Crossref, PubMed, X-MOL, etc.
    """

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)

    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    source_type: Mapped[str] = mapped_column(String(32))  # api, rss, scraper

    config: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class SourceHealth(Base):
    """
    数据源健康状态

    监控外部 API 是否稳定
    """

    __tablename__ = "source_health"

    id: Mapped[int] = mapped_column(primary_key=True)

    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)

    last_success: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_failure: Mapped[Optional[datetime]] = mapped_column(DateTime)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)

    latency_ms: Mapped[Optional[float]] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), default="unknown")  # healthy, degraded, down

    last_error: Mapped[Optional[str]] = mapped_column(Text)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
