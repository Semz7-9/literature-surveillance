"""
Work Identity Resolver

将 Record 映射到 Work 的核心逻辑

关键设计：
1. Identity resolution 本身需要 provenance
2. 支持 candidate/provisional/confirmed 状态
3. 优先使用显式关系（Crossref relation）
4. 模糊匹配需要人工审核
5. proposal 与 materialization 分离：只有 CONFIRMED 的 edge 才能回填 record.work_id。
   CANDIDATE/PROVISIONAL 的 edge 只是一个"系统认为可能是同一个 Work"的断言，
   在人工确认之前不能污染 canonical Work 的 records 集合。
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
    normalize_doi,
)


# Crossref 的 intra-work relation 是成对（reciprocal）定义的：一篇论文说
# "is-preprint-of B"，B 的 metadata 里通常会有 "has-preprint A" 反过来指回来。
# 只检查其中一个方向会在"先导入 preprint 后导入 VoR"时错过关系——即使 VoR
# 自己的 relation 字段里已经带着能找到 preprint 的证据。这里不区分方向，两个
# 方向都当作"这两个 identifier 属于同一个 Work"的证据来处理。
INTRA_WORK_RELATIONS = {
    "is-preprint-of",
    "has-preprint",
    "is-version-of",
    "has-version",
}


class WorkIdentityResolver:
    """Work identity resolution 逻辑"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def resolve_or_create_work(
        self, record: Record, crossref_relations: dict | None = None
    ) -> IdentityEdge:
        """
        为 Record 找到或创建对应的 IdentityEdge

        流程：
        1. 检查是否有显式关系（Crossref is-version-of）
        2. 检查 DOI 是否已经有 Work
        3. 尝试 title + first author 模糊匹配（candidate，需要人工审核）
        4. 如果都没有，创建新 Work（confirmed，因为不存在合并风险）

        返回的 IdentityEdge 描述"系统提议的身份判断"，而不是已经生效的合并。
        调用方必须检查 edge.status：
        - 只有 status == CONFIRMED 时才能 record.work_id = edge.target_work_id
        - CANDIDATE / PROVISIONAL 需要走 get_candidate_edges() → confirm_edge()
          的人工审核路径，在此之前 record.work_id 必须保持 None

        Args:
            record: 要解析的 Record
            crossref_relations: Crossref API 返回的 relation 字段

        Returns:
            IdentityEdge（未必是 CONFIRMED 状态）
        """
        # 1. 检查显式关系
        if crossref_relations:
            edge = await self._resolve_from_explicit_relation(record, crossref_relations)
            if edge:
                return edge

        # 2. 检查 DOI 是否已注册
        if record.doi:
            work = await self._find_work_by_identifier("doi", record.doi)
            if work:
                return await self._create_identity_edge(
                    record,
                    work,
                    IdentityEvidenceType.DOI_EXACT,
                    1.0,
                    IdentityStatus.CONFIRMED,
                    {"reason": "DOI already registered to this Work"},
                )

        # 3. 尝试模糊匹配（title + first author），结果是 candidate，需要人工审核
        edge = await self._fuzzy_match_work(record)
        if edge:
            return edge

        # 4. 创建新 Work（没有合并风险，可以直接 confirmed）
        edge = await self._create_new_work(record)
        return edge

    async def materialize_if_confirmed(self, record: Record, edge: IdentityEdge) -> bool:
        """
        如果 edge 已经是 CONFIRMED，把 record.work_id 回填到该 Work

        这是 proposal → materialization 的唯一合法入口。CANDIDATE/PROVISIONAL
        的 edge 传进来时不会有任何效果，调用方不需要在外面再判断一次状态。

        Returns:
            True 表示已经回填（record.work_id 已设置），False 表示未回填
        """
        if edge.status != IdentityStatus.CONFIRMED.value:
            return False
        record.work_id = edge.target_work_id
        await self.session.flush()
        return True

    async def _resolve_from_explicit_relation(
        self, record: Record, relations: dict
    ) -> Optional[IdentityEdge]:
        """从 Crossref explicit relation 解析（双向：is-X-of 和 has-X 都算证据）"""
        for relation_type in INTRA_WORK_RELATIONS:
            if relation_type in relations:
                for related in relations[relation_type]:
                    related_doi = related.get("id")
                    if related_doi:
                        related_doi = normalize_doi(related_doi)
                        work = await self._find_work_by_identifier("doi", related_doi)
                        if work:
                            return await self._create_identity_edge(
                                record,
                                work,
                                IdentityEvidenceType.EXPLICIT_CROSSREF_RELATION,
                                1.0,
                                IdentityStatus.CONFIRMED,
                                {"relation_type": relation_type, "related_doi": related_doi},
                            )

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

    async def _fuzzy_match_work(self, record: Record) -> Optional[IdentityEdge]:
        """
        模糊匹配 Work

        使用 title + first author。当前实现本质上仍是精确匹配（title 完全相同 +
        first author 完全相同），不是真正的相似度匹配，所以统一产出 CANDIDATE，
        交给人工审核，而不是自动 provisional 合并——避免"看起来有把握"但其实
        只是同名撞车的情况被直接合并进 canonical Work。
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
            # 检查 first author（只看已经 confirmed 的 record，避免在未审核的
            # provisional/candidate 匹配基础上继续叠加匹配）
            work_records = await self.session.execute(
                select(Record).where(Record.work_id == work.id)
            )
            work_first_record = work_records.scalars().first()

            if work_first_record and work_first_record.authors:
                work_first_author = work_first_record.authors[0].get("name")

                if work_first_author == first_author:
                    return await self._create_identity_edge(
                        record,
                        work,
                        IdentityEvidenceType.EXACT_TITLE_FIRST_AUTHOR,
                        0.7,
                        IdentityStatus.CANDIDATE,
                        {
                            "title_match": "exact",
                            "first_author": first_author,
                        },
                    )

        return None

    async def _create_new_work(self, record: Record) -> IdentityEdge:
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

        # 创建自动确认的 identity edge（新建 Work 不存在合并风险，可以直接 confirmed）
        edge = await self._create_identity_edge(
            record,
            work,
            IdentityEvidenceType.NEW_WORK,
            1.0,
            IdentityStatus.CONFIRMED,
            {"reason": "no matching Work found, created new one"},
        )

        await self.session.flush()
        return edge

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
        """获取需要人工审核的 edges（CANDIDATE 和 PROVISIONAL 都尚未 materialize）"""
        stmt = select(IdentityEdge).where(
            IdentityEdge.status.in_(
                [IdentityStatus.CANDIDATE.value, IdentityStatus.PROVISIONAL.value]
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def confirm_edge(self, edge_id: int) -> None:
        """
        人工确认 identity edge，并把 record.work_id materialize 到目标 Work

        Raises:
            ValueError: 如果该 record 已经有另一条生效的 CONFIRMED edge。
                必须先 reject_edge() 那一条，再确认这一条——不允许同一个
                record 同时存在两条互相矛盾的"已确认"身份判断。
        """
        stmt = select(IdentityEdge).where(IdentityEdge.id == edge_id)
        result = await self.session.execute(stmt)
        edge = result.scalar_one()

        existing_stmt = select(IdentityEdge).where(
            IdentityEdge.source_record_id == edge.source_record_id,
            IdentityEdge.status == IdentityStatus.CONFIRMED.value,
            IdentityEdge.id != edge.id,
        )
        existing = (await self.session.execute(existing_stmt)).scalar_one_or_none()
        if existing:
            raise ValueError(
                f"Record {edge.source_record_id} already has a CONFIRMED edge "
                f"(id={existing.id}, target_work_id={existing.target_work_id}). "
                f"Reject it before confirming edge {edge_id}."
            )

        edge.status = IdentityStatus.CONFIRMED.value
        edge.updated_at = datetime.utcnow()

        record_stmt = select(Record).where(Record.id == edge.source_record_id)
        record_result = await self.session.execute(record_stmt)
        record = record_result.scalar_one()
        record.work_id = edge.target_work_id

        await self.session.flush()

    async def reject_edge(self, edge_id: int) -> None:
        """
        拒绝 identity edge

        由于 CANDIDATE/PROVISIONAL 从不 materialize record.work_id（参见
        materialize_if_confirmed），这里通常不需要"拆回去"。但如果这条 edge
        此前被错误地 confirm 过又需要撤销，这里会同时清空 record.work_id，
        保证 reject 的操作是真正可逆的，而不是只改一个 enum。
        """
        stmt = select(IdentityEdge).where(IdentityEdge.id == edge_id)
        result = await self.session.execute(stmt)
        edge = result.scalar_one()

        edge.status = IdentityStatus.REJECTED.value
        edge.updated_at = datetime.utcnow()

        record_stmt = select(Record).where(Record.id == edge.source_record_id)
        record_result = await self.session.execute(record_stmt)
        record = record_result.scalar_one()
        if record.work_id == edge.target_work_id:
            record.work_id = None

        await self.session.flush()
