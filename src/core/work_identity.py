"""
Work Identity Resolver

将 Record 映射到 Work 的核心逻辑

关键设计：
1. Identity resolution 本身需要 provenance
2. 支持 candidate/provisional/confirmed 状态
3. 优先使用显式关系（Crossref relation）
4. 模糊匹配需要人工审核
"""

from typing import Optional
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.models import (
    Work,
    Record,
    WorkIdentifier,
    IdentityEdge,
    IdentityEvidenceType,
    IdentityStatus,
)


class WorkIdentityResolver:
    """Work identity resolution 逻辑"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def resolve_or_create_work(
        self, record: Record, crossref_relations: dict | None = None
    ) -> Work:
        """
        为 Record 找到或创建对应的 Work

        流程：
        1. 检查是否有显式关系（Crossref is-version-of）
        2. 检查 DOI 是否已经有 Work
        3. 尝试 title + first author 匹配
        4. 如果都没有，创建新 Work

        Args:
            record: 要解析的 Record
            crossref_relations: Crossref API 返回的 relation 字段

        Returns:
            关联的 Work
        """
        # 1. 检查显式关系
        if crossref_relations:
            work = await self._resolve_from_explicit_relation(record, crossref_relations)
            if work:
                return work

        # 2. 检查 DOI 是否已注册
        if record.doi:
            work = await self._find_work_by_identifier("doi", record.doi)
            if work:
                await self._create_identity_edge(
                    record,
                    work,
                    IdentityEvidenceType.EXACT_TITLE_AUTHOR_MATCH,
                    1.0,
                    IdentityStatus.CONFIRMED,
                    {"reason": "DOI already registered"},
                )
                return work

        # 3. 尝试模糊匹配（title + first author）
        work = await self._fuzzy_match_work(record)
        if work:
            return work

        # 4. 创建新 Work
        work = await self._create_new_work(record)
        return work

    async def _resolve_from_explicit_relation(
        self, record: Record, relations: dict
    ) -> Optional[Work]:
        """从 Crossref explicit relation 解析"""
        # is-version-of, is-preprint-of, has-preprint, etc.
        for relation_type in ["is-version-of", "is-preprint-of"]:
            if relation_type in relations:
                for related in relations[relation_type]:
                    related_doi = related.get("id")
                    if related_doi:
                        work = await self._find_work_by_identifier("doi", related_doi)
                        if work:
                            await self._create_identity_edge(
                                record,
                                work,
                                IdentityEvidenceType.EXPLICIT_CROSSREF_RELATION,
                                1.0,
                                IdentityStatus.CONFIRMED,
                                {"relation_type": relation_type, "related_doi": related_doi},
                            )
                            return work

        return None

    async def _find_work_by_identifier(
        self, identifier_type: str, identifier_value: str
    ) -> Optional[Work]:
        """通过 identifier 查找 Work"""
        stmt = (
            select(Work)
            .join(WorkIdentifier)
            .where(
                WorkIdentifier.identifier_type == identifier_type,
                WorkIdentifier.identifier_value == identifier_value,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def _fuzzy_match_work(self, record: Record) -> Optional[Work]:
        """
        模糊匹配 Work

        使用 title + first author + year
        如果匹配度高，创建 provisional edge
        否则创建 candidate edge 等待人工审核
        """
        # 提取 first author
        first_author = None
        if record.authors and len(record.authors) > 0:
            first_author = record.authors[0].get("name")

        if not first_author:
            return None

        # 查找相似的 Work
        # 简化版：exact title match + first author match
        # 实际应该用更复杂的相似度算法
        stmt = select(Work).where(Work.title == record.title)
        result = await self.session.execute(stmt)
        candidates = result.scalars().all()

        for work in candidates:
            # 检查 first author
            work_records = await self.session.execute(
                select(Record).where(Record.work_id == work.id)
            )
            work_first_record = work_records.scalars().first()

            if work_first_record and work_first_record.authors:
                work_first_author = work_first_record.authors[0].get("name")

                if work_first_author == first_author:
                    # 高置信度匹配
                    await self._create_identity_edge(
                        record,
                        work,
                        IdentityEvidenceType.EXACT_TITLE_AUTHOR_MATCH,
                        0.95,
                        IdentityStatus.PROVISIONAL,
                        {
                            "title_match": "exact",
                            "first_author": first_author,
                        },
                    )
                    return work

        return None

    async def _create_new_work(self, record: Record) -> Work:
        """创建新 Work"""
        work = Work(
            work_id=f"W{datetime.utcnow().timestamp()}",  # 临时 ID 生成策略
            title=record.title,
            canonical_doi=record.doi,
        )
        self.session.add(work)
        await self.session.flush()

        # 添加 identifier
        if record.doi:
            identifier = WorkIdentifier(
                work_id=work.id,
                identifier_type="doi",
                identifier_value=record.doi,
            )
            self.session.add(identifier)

        # 添加其他 identifier (PMID, PMCID, etc.)
        for id_type, id_value in record.extra_metadata.items():
            if id_type in ["pmid", "pmcid", "arxiv_id"]:
                identifier = WorkIdentifier(
                    work_id=work.id,
                    identifier_type=id_type,
                    identifier_value=id_value,
                )
                self.session.add(identifier)

        # 创建自动确认的 identity edge
        await self._create_identity_edge(
            record,
            work,
            IdentityEvidenceType.EXACT_TITLE_AUTHOR_MATCH,
            1.0,
            IdentityStatus.CONFIRMED,
            {"reason": "new work created"},
        )

        await self.session.flush()
        return work

    async def _create_identity_edge(
        self,
        record: Record,
        work: Work,
        evidence_type: IdentityEvidenceType,
        confidence: float,
        status: IdentityStatus,
        evidence_detail: dict,
    ) -> IdentityEdge:
        """创建 identity edge"""
        edge = IdentityEdge(
            source_record_id=record.id,
            target_work_id=work.id,
            evidence_type=evidence_type.value,
            confidence=confidence,
            evidence_detail=evidence_detail,
            status=status.value,
        )
        self.session.add(edge)
        await self.session.flush()
        return edge

    async def get_candidate_edges(self) -> list[IdentityEdge]:
        """获取需要人工审核的 candidate edges"""
        stmt = select(IdentityEdge).where(
            IdentityEdge.status == IdentityStatus.CANDIDATE.value
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def confirm_edge(self, edge_id: int) -> None:
        """人工确认 identity edge"""
        stmt = select(IdentityEdge).where(IdentityEdge.id == edge_id)
        result = await self.session.execute(stmt)
        edge = result.scalar_one()

        edge.status = IdentityStatus.CONFIRMED.value
        edge.updated_at = datetime.utcnow()
        await self.session.flush()

    async def reject_edge(self, edge_id: int) -> None:
        """拒绝 identity edge"""
        stmt = select(IdentityEdge).where(IdentityEdge.id == edge_id)
        result = await self.session.execute(stmt)
        edge = result.scalar_one()

        edge.status = IdentityStatus.REJECTED.value
        edge.updated_at = datetime.utcnow()
        await self.session.flush()
