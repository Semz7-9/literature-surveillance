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
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
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
            doi = doi[len(prefix) :]
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
    SUPERSEDED = "SUPERSEDED"  # 后续 Work merge 已使候选边失去独立意义


class WorkStatus(str, Enum):
    ACTIVE = "ACTIVE"
    MERGED = "MERGED"


class PendingRelationStatus(str, Enum):
    PENDING = "PENDING"
    RESOLVED = "RESOLVED"
    CONFLICT = "CONFLICT"
    DISMISSED = "DISMISSED"


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
    # Canonical metadata is a projection of these records, never a permanent
    # copy of whichever record happened to be imported first.
    preferred_record_id: Mapped[Optional[int]] = mapped_column(ForeignKey("records.id"))
    first_public_record_id: Mapped[Optional[int]] = mapped_column(ForeignKey("records.id"))
    status: Mapped[str] = mapped_column(String(16), default=WorkStatus.ACTIVE.value, index=True)
    merged_into_work_id: Mapped[Optional[int]] = mapped_column(ForeignKey("works.id"))

    # 时间
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # 关系
    records: Mapped[list["Record"]] = relationship(
        back_populates="work", foreign_keys="Record.work_id"
    )
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
    publication_date_precision: Mapped[Optional[str]] = mapped_column(String(8))  # day, month, year
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
    work: Mapped["Work"] = relationship(back_populates="records", foreign_keys=[work_id])

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


class SourceSnapshot(Base):
    """
    Record 摘要文本的不变快照

    raw_abstract 保留来源原文（可能含 JATS/XML 标签）；analysis_text 是
    经过 normalize_abstract_text() 处理后的纯文本版本——LLM 抽取和
    evidence_spans 逐字匹配都必须针对 analysis_text，不能针对
    raw_abstract 或可变的 Record.abstract，否则标签会导致 "证据在原文
    里找不到" 的伪失败，而且证据锚点会在 Record 被重新抓取覆盖后失效。
    """

    __tablename__ = "source_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    record_id: Mapped[int] = mapped_column(ForeignKey("records.id"), index=True)

    raw_abstract: Mapped[Optional[str]] = mapped_column(Text)
    analysis_text: Mapped[Optional[str]] = mapped_column(Text)
    raw_hash: Mapped[Optional[str]] = mapped_column(String(64))  # SHA-256 of raw_abstract
    analysis_hash: Mapped[Optional[str]] = mapped_column(String(64))  # SHA-256 of analysis_text
    normalizer_version: Mapped[Optional[str]] = mapped_column(String(32))
    source_name: Mapped[str] = mapped_column(String(64))  # "crossref", "pubmed", etc.
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PendingIdentifierRelation(Base):
    """An explicit source relation whose target identifier is not yet usable."""

    __tablename__ = "pending_identifier_relations"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_record_id: Mapped[int] = mapped_column(ForeignKey("records.id"), index=True)
    target_identifier_type: Mapped[str] = mapped_column(String(32), index=True)
    target_identifier_value: Mapped[str] = mapped_column(String(255), index=True)
    relation_type: Mapped[str] = mapped_column(String(64))
    evidence_source: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(16), default=PendingRelationStatus.PENDING.value, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    __table_args__ = (
        UniqueConstraint(
            "source_record_id",
            "target_identifier_type",
            "target_identifier_value",
            "relation_type",
            name="uq_pending_identifier_relation",
        ),
    )


class WorkMergeAudit(Base):
    """Immutable provenance for a conservative Work merge."""

    __tablename__ = "work_merge_audits"

    id: Mapped[int] = mapped_column(primary_key=True)
    merged_from_work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), index=True)
    merged_into_work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), index=True)
    reason: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AnalysisArtifact(Base):
    """
    持久化的分析输出（L1/L2/L3 等）

    analysis 结果落盘到这里，不再只是打印。snapshot_id 指向分析时
    使用的原始文本快照（必填，每个 L1 artifact 都必须可追溯到具体
    snapshot），保证"用什么数据得出什么结论"可复现。skill_version/
    schema_version 记录生成时使用的 skill 语义和输出 schema 版本，
    supersedes_id 指向被取代的上一个 artifact，用于同一 record 重新
    生成（重试/模型升级/摘要更新）时保留历史而不是产生 UNIQUE 冲突。
    """

    __tablename__ = "analysis_artifacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    artifact_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    record_id: Mapped[int] = mapped_column(ForeignKey("records.id"), index=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("source_snapshots.id"), index=True)

    analysis_type: Mapped[str] = mapped_column(String(32), index=True)  # "L1", "L2", etc.
    skill_version: Mapped[str] = mapped_column(String(32))
    schema_version: Mapped[str] = mapped_column(String(16))
    supersedes_id: Mapped[Optional[int]] = mapped_column(ForeignKey("analysis_artifacts.id"))

    content: Mapped[dict] = mapped_column(JSON)  # analysis output dict
    markdown: Mapped[Optional[str]] = mapped_column(Text)  # rendered Markdown

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


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


class MonitorSubscription(Base):
    """A user's durable instruction describing what should be monitored."""

    __tablename__ = "monitor_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    subscription_type: Mapped[str] = mapped_column(String(32))  # journal, topic, preprint
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class SourceCursor(Base):
    """Incremental progress for one monitor subscription."""

    __tablename__ = "source_cursors"

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("monitor_subscriptions.id"), unique=True, index=True
    )
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    cursor_value: Mapped[Optional[str]] = mapped_column(Text)
    last_seen_identifier: Mapped[Optional[str]] = mapped_column(String(255))
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    state: Mapped[dict] = mapped_column(JSON, default=dict)


class DiscoveryEvent(Base):
    """Provenance for why a Work appeared in the monitor inbox."""

    __tablename__ = "discovery_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("monitor_subscriptions.id"), index=True)
    external_identifier: Mapped[str] = mapped_column(String(255), index=True)
    work_id: Mapped[Optional[int]] = mapped_column(ForeignKey("works.id"), index=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    last_metadata_hash: Mapped[Optional[str]] = mapped_column(String(64))
    source_url: Mapped[Optional[str]] = mapped_column(Text)
    raw_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="DISCOVERED", index=True)

    __table_args__ = (
        UniqueConstraint(
            "subscription_id", "external_identifier", name="uq_discovery_subscription_identifier"
        ),
    )


class MonitorRun(Base):
    """Immutable operational history for one subscription execution."""

    __tablename__ = "monitor_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("monitor_subscriptions.id"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    window_from: Mapped[Optional[datetime]] = mapped_column(DateTime)
    window_until: Mapped[Optional[datetime]] = mapped_column(DateTime)
    cursor_start: Mapped[Optional[str]] = mapped_column(Text)
    cursor_end: Mapped[Optional[str]] = mapped_column(Text)
    discovered: Mapped[int] = mapped_column(Integer, default=0)
    created: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    l1_generated: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    has_more: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="RUNNING", index=True)
    error: Mapped[Optional[str]] = mapped_column(Text)

    __table_args__ = (
        Index(
            "uq_monitor_runs_one_running_per_subscription",
            "subscription_id",
            unique=True,
            sqlite_where=text("status = 'RUNNING'"),
        ),
    )


class SourceHealth(Base):
    """
    数据源健康状态

    监控外部 API 是否稳定
    """

    __tablename__ = "source_health"

    id: Mapped[int] = mapped_column(primary_key=True)

    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), unique=True)

    last_success: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_failure: Mapped[Optional[datetime]] = mapped_column(DateTime)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)

    latency_ms: Mapped[Optional[float]] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), default="unknown")  # healthy, degraded, down

    last_error: Mapped[Optional[str]] = mapped_column(Text)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


# ============================================================================
# Topic Archive v0.1
# ============================================================================


class TopicArchive(Base):
    """A durable, human-controlled research topic."""

    __tablename__ = "topic_archives"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    focus: Mapped[str] = mapped_column(Text, default="")
    background_mode: Mapped[str] = mapped_column(String(16), default="AUTO")
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", index=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class ArchiveScope(Base):
    """Immutable version of an Archive's inclusion and exclusion boundary."""

    __tablename__ = "archive_scopes"

    id: Mapped[int] = mapped_column(primary_key=True)
    archive_id: Mapped[int] = mapped_column(ForeignKey("topic_archives.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    core_concepts: Mapped[list[str]] = mapped_column(JSON, default=list)
    background_concepts: Mapped[list[str]] = mapped_column(JSON, default=list)
    exclusions: Mapped[list[str]] = mapped_column(JSON, default=list)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("archive_id", "version", name="uq_archive_scope_version"),)


class ArchiveBackground(Base):
    """Human-provided stable background attached to an Archive."""

    __tablename__ = "archive_backgrounds"

    id: Mapped[int] = mapped_column(primary_key=True)
    archive_id: Mapped[int] = mapped_column(ForeignKey("topic_archives.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)
    source_url: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ConceptSet(Base):
    """A named search concept, such as Representation or Navigation."""

    __tablename__ = "concept_sets"

    id: Mapped[int] = mapped_column(primary_key=True)
    archive_id: Mapped[int] = mapped_column(ForeignKey("topic_archives.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("archive_id", "name", name="uq_archive_concept_set_name"),)


class ConceptTerm(Base):
    """One auditable term in a Concept Set."""

    __tablename__ = "concept_terms"

    id: Mapped[int] = mapped_column(primary_key=True)
    concept_set_id: Mapped[int] = mapped_column(ForeignKey("concept_sets.id"), index=True)
    term: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="include")
    source: Mapped[str] = mapped_column(String(32), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("concept_set_id", "term", name="uq_concept_set_term"),)


class SearchStrategy(Base):
    """Versioned, inspectable Boolean queries generated from Concept Sets."""

    __tablename__ = "search_strategies"

    id: Mapped[int] = mapped_column(primary_key=True)
    archive_id: Mapped[int] = mapped_column(ForeignKey("topic_archives.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(32), default="pubmed")
    queries: Mapped[list[dict]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    executed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    __table_args__ = (
        UniqueConstraint("archive_id", "version", name="uq_archive_search_strategy_version"),
    )


class ArchiveWork(Base):
    """Membership of a canonical Work in an Archive corpus."""

    __tablename__ = "archive_works"

    id: Mapped[int] = mapped_column(primary_key=True)
    archive_id: Mapped[int] = mapped_column(ForeignKey("topic_archives.id"), index=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), index=True)
    strategy_id: Mapped[Optional[int]] = mapped_column(ForeignKey("search_strategies.id"))
    matched_queries: Mapped[list[str]] = mapped_column(JSON, default=list)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("archive_id", "work_id", name="uq_archive_work_membership"),)


class ArchiveRevision(Base):
    """Append-only user-visible history of meaningful Archive changes."""

    __tablename__ = "archive_revisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    archive_id: Mapped[int] = mapped_column(ForeignKey("topic_archives.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    change_type: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text)
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("archive_id", "version", name="uq_archive_revision_version"),
    )


# ============================================================================
# Archive Automation Foundation
# ============================================================================


class ArchiveBuildRun(Base):
    """Durable state machine for one automated Archive build attempt."""

    __tablename__ = "archive_build_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    archive_id: Mapped[int] = mapped_column(ForeignKey("topic_archives.id"), index=True)
    state: Mapped[str] = mapped_column(String(32), default="CREATED", index=True)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    input_version: Mapped[int] = mapped_column(Integer, default=1)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class ArchiveBuildStep(Base):
    """Append-only execution record for a resumable build stage."""

    __tablename__ = "archive_build_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("archive_build_runs.id"), index=True)
    stage: Mapped[str] = mapped_column(String(32), index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    input_version: Mapped[int] = mapped_column(Integer, default=1)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    output_artifact: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[Optional[str]] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("run_id", "stage", "attempt", name="uq_archive_build_step_attempt"),
    )


class BackgroundProfile(Base):
    """Reusable disciplinary background profile shared across Archives."""

    __tablename__ = "background_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    discipline: Mapped[str] = mapped_column(String(255), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class BackgroundNode(Base):
    """Canonical reusable knowledge node; contributions never overwrite its core."""

    __tablename__ = "background_nodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("background_profiles.id"), index=True)
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("background_nodes.id"), index=True)
    title: Mapped[str] = mapped_column(String(255), index=True)
    canonical_summary: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        UniqueConstraint("profile_id", "title", name="uq_background_profile_node_title"),
    )


class ArchiveBackgroundLink(Base):
    """Selection decision connecting an Archive to a shared BackgroundNode."""

    __tablename__ = "archive_background_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    archive_id: Mapped[int] = mapped_column(ForeignKey("topic_archives.id"), index=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("background_nodes.id"), index=True)
    relevance: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[str] = mapped_column(String(16), default="MEDIUM")
    selection_reason: Mapped[str] = mapped_column(Text, default="")
    selected_by: Mapped[str] = mapped_column(String(32), default="BACKGROUND_RESOLVER")
    status: Mapped[str] = mapped_column(String(16), default="ATTACHED", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (UniqueConstraint("archive_id", "node_id", name="uq_archive_background_node"),)


class BackgroundOperatorContribution(Base):
    """Immutable raw operator thought, deliberately separate from AI and canonical text."""

    __tablename__ = "background_operator_contributions"

    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("background_nodes.id"), index=True)
    archive_id: Mapped[Optional[int]] = mapped_column(ForeignKey("topic_archives.id"), index=True)
    contribution_type: Mapped[str] = mapped_column(String(32), index=True)
    raw_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class BackgroundAIContribution(Base):
    """Provenanced model output addressed by capability role, not a hard-coded vendor."""

    __tablename__ = "background_ai_contributions"

    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("background_nodes.id"), index=True)
    archive_id: Mapped[Optional[int]] = mapped_column(ForeignKey("topic_archives.id"), index=True)
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(32), index=True)
    input_refs: Mapped[list[dict]] = mapped_column(JSON, default=list)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class BackgroundSource(Base):
    """Source provenance supporting a canonical BackgroundNode."""

    __tablename__ = "background_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("background_nodes.id"), index=True)
    title: Mapped[str] = mapped_column(String(500))
    url: Mapped[Optional[str]] = mapped_column(Text)
    citation: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ============================================================================
# Archive Planning (Batch A)
# ============================================================================


class OperatorProfile(Base):
    """Reusable researcher context, independent from any one Archive."""

    __tablename__ = "operator_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_key: Mapped[str] = mapped_column(String(64), default="default", index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    research_interests: Mapped[list[str]] = mapped_column(JSON, default=list)
    active_projects: Mapped[list[str]] = mapped_column(JSON, default=list)
    conceptual_preferences: Mapped[list[str]] = mapped_column(JSON, default=list)
    methodological_principles: Mapped[list[str]] = mapped_column(JSON, default=list)
    terminology_preferences: Mapped[list[str]] = mapped_column(JSON, default=list)
    note_conventions: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        UniqueConstraint("profile_key", "version", name="uq_operator_profile_version"),
    )


class OperatorLens(Base):
    """Named, reusable operator perspective that can be applied to Archives."""

    __tablename__ = "operator_lenses"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("operator_profiles.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    lens_type: Mapped[str] = mapped_column(String(32), default="CONCEPTUAL")
    content: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("profile_id", "title", "version", name="uq_operator_lens_version"),
    )


class ArchiveOperatorContext(Base):
    """Snapshot-like selection of reusable operator context for one Archive."""

    __tablename__ = "archive_operator_contexts"

    id: Mapped[int] = mapped_column(primary_key=True)
    archive_id: Mapped[int] = mapped_column(
        ForeignKey("topic_archives.id"), unique=True, index=True
    )
    profile_id: Mapped[int] = mapped_column(ForeignKey("operator_profiles.id"), index=True)
    profile_version: Mapped[int] = mapped_column(Integer)
    selected_lens_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    archive_context: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class ArchiveScopeDraft(Base):
    """Structured machine proposal; not equivalent to an operator-approved ArchiveScope."""

    __tablename__ = "archive_scope_drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    archive_id: Mapped[int] = mapped_column(ForeignKey("topic_archives.id"), index=True)
    build_run_id: Mapped[int] = mapped_column(ForeignKey("archive_build_runs.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    core_scope: Mapped[str] = mapped_column(Text)
    included_domains: Mapped[list[str]] = mapped_column(JSON, default=list)
    excluded_domains: Mapped[list[str]] = mapped_column(JSON, default=list)
    temporal_scope: Mapped[dict] = mapped_column(JSON, default=dict)
    object_scope: Mapped[list[str]] = mapped_column(JSON, default=list)
    method_scope: Mapped[list[str]] = mapped_column(JSON, default=list)
    ambiguities: Mapped[list[dict]] = mapped_column(JSON, default=list)
    reasoning_summary: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="PROVISIONAL", index=True)
    generated_by: Mapped[str] = mapped_column(String(16), default="SYSTEM")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("archive_id", "version", name="uq_archive_scope_draft_version"),
    )


class ArchiveSearchPlan(Base):
    """Provider-neutral semantic plan compiled to database syntax only in Batch B."""

    __tablename__ = "archive_search_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    archive_id: Mapped[int] = mapped_column(ForeignKey("topic_archives.id"), index=True)
    build_run_id: Mapped[int] = mapped_column(ForeignKey("archive_build_runs.id"), index=True)
    scope_draft_id: Mapped[int] = mapped_column(ForeignKey("archive_scope_drafts.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    concepts: Mapped[list[dict]] = mapped_column(JSON, default=list)
    historical_vocabulary: Mapped[list[str]] = mapped_column(JSON, default=list)
    hard_exclusions: Mapped[list[str]] = mapped_column(JSON, default=list)
    soft_exclusions: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_targets: Mapped[list[str]] = mapped_column(JSON, default=list)
    rationale: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="PROVISIONAL", index=True)
    generated_by: Mapped[str] = mapped_column(String(16), default="SYSTEM")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("archive_id", "version", name="uq_archive_search_plan_version"),
    )


class GenerationRun(Base):
    """Privacy-conscious audit ledger for one model or deterministic generation attempt."""

    __tablename__ = "generation_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    archive_id: Mapped[Optional[int]] = mapped_column(ForeignKey("topic_archives.id"), index=True)
    build_run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("archive_build_runs.id"), index=True
    )
    role: Mapped[str] = mapped_column(String(32), index=True)
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    prompt_hash: Mapped[str] = mapped_column(String(64), index=True)
    text_hash: Mapped[str] = mapped_column(String(64), index=True)
    cache_key: Mapped[str] = mapped_column(String(128), index=True)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    parse_status: Mapped[str] = mapped_column(String(16), default="PENDING")
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[Optional[int]] = mapped_column(Integer)
    output_tokens: Mapped[Optional[int]] = mapped_column(Integer)
    error_category: Mapped[Optional[str]] = mapped_column(String(64))
    error: Mapped[Optional[str]] = mapped_column(Text)
    input_refs: Mapped[list[dict]] = mapped_column(JSON, default=list)
    output_hash: Mapped[Optional[str]] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="RUNNING", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class AIProposal(Base):
    """Machine suggestion whose lifecycle remains separate from operator decisions."""

    __tablename__ = "ai_proposals"

    id: Mapped[int] = mapped_column(primary_key=True)
    archive_id: Mapped[int] = mapped_column(ForeignKey("topic_archives.id"), index=True)
    build_run_id: Mapped[int] = mapped_column(ForeignKey("archive_build_runs.id"), index=True)
    generation_run_id: Mapped[int] = mapped_column(ForeignKey("generation_runs.id"), index=True)
    proposal_type: Mapped[str] = mapped_column(String(32), index=True)
    target_type: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[Optional[int]] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    impact: Mapped[str] = mapped_column(String(16), default="LOW", index=True)
    origin: Mapped[str] = mapped_column(String(16), default="AI")
    status: Mapped[str] = mapped_column(String(24), default="PROPOSED", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ReviewItem(Base):
    """Exception-driven question surfaced only when a proposal needs operator judgment."""

    __tablename__ = "review_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    archive_id: Mapped[int] = mapped_column(ForeignKey("topic_archives.id"), index=True)
    build_run_id: Mapped[int] = mapped_column(ForeignKey("archive_build_runs.id"), index=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("ai_proposals.id"), unique=True, index=True)
    item_type: Mapped[str] = mapped_column(String(32), index=True)
    question: Mapped[str] = mapped_column(Text)
    options: Mapped[list[dict]] = mapped_column(JSON, default=list)
    impact: Mapped[str] = mapped_column(String(16), default="MEDIUM", index=True)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class HumanDecision(Base):
    """Append-only operator resolution; never overwrites the originating AIProposal."""

    __tablename__ = "human_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    archive_id: Mapped[int] = mapped_column(ForeignKey("topic_archives.id"), index=True)
    review_item_id: Mapped[int] = mapped_column(ForeignKey("review_items.id"), index=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("ai_proposals.id"), index=True)
    decision: Mapped[str] = mapped_column(String(32), index=True)
    rationale: Mapped[str] = mapped_column(Text, default="")
    reviewer_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
