"""
Work Identity Resolution 的 regression test

与 test_phase0.py 不同：这里不调用真实的 Crossref/LLM API，只用手工构造的
Record 和 Crossref relation dict 驱动 resolver，验证 identity 判断本身的正确性。

覆盖的 gold cases（对应 review 里的 P0-D）：
- A: 同一个 DOI 重复摄入 -> 同一个 Record，不重复创建
- B: DOI 的大小写/URL 变体 -> 规范化后命中同一个 Record/Work
- C: preprint -> VoR，Crossref 显式关系存在且目标已入库 -> 自动 CONFIRMED
- D: 反向导入顺序（先 VoR 再 preprint）-> 同样能通过显式关系确认
- E: 完全不同的两个 Work，标题人名都不同 -> 各自新建，不会被误合并
- F: 相同标题 + 相同第一作者，但语义上是两个不同 Work -> 只产出 CANDIDATE，
     不会自动 materialize（因为当前 fuzzy match 本质是精确匹配，没有资格自动确认）
- I: candidate 人工确认（confirm_edge）-> record.work_id 正确回填
- J: candidate 人工拒绝（reject_edge）-> record.work_id 保持 None，两个 Work 保持独立
- K: 已经 confirmed 的 edge 被人工撤销（reject_edge）-> record.work_id 被清空，
     merge 是可逆的，不是只改一个 enum
"""

import pytest
from pathlib import Path

from src.core.database import Database
from src.core.models import Record, Work, IdentityStatus, IdentityEvidenceType
from src.core.work_identity import WorkIdentityResolver
from src.core.ingestion import get_or_create_record


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test_identity.db")
    await database.init_db()
    yield database
    await database.close()


def make_record_factory(title: str, first_author: str, **kwargs):
    """构造一个 record_factory，签名匹配 get_or_create_record 的约定"""

    def factory(normalized_doi: str) -> Record:
        return Record(
            record_id=f"R_{normalized_doi.replace('/', '_')}",
            work_id=None,
            title=title,
            authors=[{"name": first_author}],
            doi=normalized_doi,
            evidence_level="E1",
            **kwargs,
        )

    return factory


async def resolve_and_maybe_materialize(
    session, resolver: WorkIdentityResolver, record: Record, relations: dict | None = None
):
    """test_phase0.py 里那套调用序列的复用版本"""
    edge = await resolver.resolve_or_create_work(record, relations)
    materialized = await resolver.materialize_if_confirmed(record, edge)
    return edge, materialized


# ---------------------------------------------------------------------------
# A: 同一 DOI 重复摄入
# ---------------------------------------------------------------------------


async def test_duplicate_doi_ingestion_reuses_record(db: Database):
    async with db.get_session() as session:
        factory = make_record_factory("Some Paper", "Smith J")

        record1, created1 = await get_or_create_record(session, "10.1/ABC", factory)
        record2, created2 = await get_or_create_record(session, "10.1/ABC", factory)

        assert created1 is True
        assert created2 is False
        assert record1.id == record2.id


# ---------------------------------------------------------------------------
# B: DOI 大小写 / URL 变体
# ---------------------------------------------------------------------------


async def test_doi_case_and_url_variants_resolve_to_same_record(db: Database):
    async with db.get_session() as session:
        factory = make_record_factory("Some Paper", "Smith J")

        record1, created1 = await get_or_create_record(session, "10.1/ABC", factory)
        record2, created2 = await get_or_create_record(
            session, "https://doi.org/10.1/abc", factory
        )
        record3, created3 = await get_or_create_record(session, "  10.1/Abc  ", factory)

        assert created1 is True
        assert created2 is False
        assert created3 is False
        assert record1.id == record2.id == record3.id
        assert record1.doi == "10.1/abc"


# ---------------------------------------------------------------------------
# C / D: Crossref 显式关系（正向 / 反向导入顺序）
# ---------------------------------------------------------------------------


async def test_explicit_relation_confirms_when_target_already_ingested(db: Database):
    async with db.get_session() as session:
        resolver = WorkIdentityResolver(session)

        # 先导入 VoR
        vor_factory = make_record_factory("Protein Folding Breakthrough", "Lee K")
        vor, _ = await get_or_create_record(session, "10.1/vor", vor_factory)
        vor_edge, vor_materialized = await resolve_and_maybe_materialize(session, resolver, vor)
        assert vor_materialized is True
        assert vor.work_id == vor_edge.target_work_id

        # 再导入 preprint，Crossref 显式声明 is-preprint-of -> VoR DOI
        preprint_factory = make_record_factory("Protein Folding Breakthrough (preprint)", "Lee K")
        preprint, _ = await get_or_create_record(session, "10.1/preprint", preprint_factory)
        relations = {"is-preprint-of": [{"id": "10.1/VOR"}]}  # 大小写变体，验证 normalize 生效
        edge, materialized = await resolve_and_maybe_materialize(
            session, resolver, preprint, relations
        )

        assert edge.status == IdentityStatus.CONFIRMED.value
        assert edge.evidence_type == IdentityEvidenceType.EXPLICIT_CROSSREF_RELATION.value
        assert materialized is True
        assert preprint.work_id == vor.work_id  # 合并到同一个 Work


async def test_one_way_pending_relation_resolves_when_target_arrives(
    db: Database,
):
    """
    反向导入顺序（先 preprint 再 VoR）目前无法通过显式关系合并——
    这是已知限制（review 里的 P0 item 4），这个测试记录当前行为，
    而不是断言这是期望行为。等 PendingIdentifierRelation 实现后，
    这个测试应该被替换成"能够合并"的断言。
    """
    async with db.get_session() as session:
        resolver = WorkIdentityResolver(session)

        # 先导入 preprint，此时 VoR 尚未入库，relation 无法解析
        preprint_factory = make_record_factory("Protein Folding Breakthrough (preprint)", "Lee K")
        preprint, _ = await get_or_create_record(session, "10.1/preprint2", preprint_factory)
        relations = {"is-preprint-of": [{"id": "10.1/vor2"}]}
        edge, materialized = await resolve_and_maybe_materialize(
            session, resolver, preprint, relations
        )

        # 找不到目标 Work，退化成新建 Work（NEW_WORK, CONFIRMED）
        assert edge.evidence_type == IdentityEvidenceType.NEW_WORK.value
        assert materialized is True

        # 后导入 VoR，这里没有给它任何 relation（模拟 Crossref 没有提供
        # has-preprint/has-version 反向声明的情况——不是所有出版商的
        # metadata 都会双向声明）。没有 relation 证据，fuzzy match 也因为
        # 标题不完全相同（少了 "(preprint)"）而不命中，于是两者成为独立的
        # Work —— 这正是"没有 PendingIdentifierRelation 就会丢数据"的场景。
        # 如果 VoR 自己的 relation 里带 has-preprint，见下面
        # test_reverse_import_order_resolves_via_reciprocal_relation。
        vor_factory = make_record_factory("Protein Folding Breakthrough", "Lee K")
        vor, _ = await get_or_create_record(session, "10.1/vor2", vor_factory)
        vor_edge, vor_materialized = await resolve_and_maybe_materialize(session, resolver, vor)

        assert vor.work_id == preprint.work_id
        assert vor_edge.evidence_type == IdentityEvidenceType.EXPLICIT_CROSSREF_RELATION.value
        assert vor_materialized is True


async def test_reverse_import_order_resolves_via_reciprocal_relation(db: Database):
    """
    Crossref 的 intra-work relation 是成对定义的：一篇论文说
    is-preprint-of/is-version-of，对应的另一篇通常会有 has-preprint/has-version
    反过来指回来。如果后导入的 VoR 自己的 metadata 里带着 has-preprint，
    resolver 现在应该能够识别，而不需要等 PendingIdentifierRelation。
    """
    async with db.get_session() as session:
        resolver = WorkIdentityResolver(session)

        # 先导入 preprint，此时 VoR 尚未入库
        preprint_factory = make_record_factory("Attention Is All You Need (preprint)", "Vaswani A")
        preprint, _ = await get_or_create_record(session, "10.1/attn-preprint", preprint_factory)
        edge, materialized = await resolve_and_maybe_materialize(
            session, resolver, preprint, {"is-preprint-of": [{"id": "10.1/attn-vor"}]}
        )
        assert edge.evidence_type == IdentityEvidenceType.NEW_WORK.value  # 目标还不存在

        # 后导入 VoR，它自己的 relation 里有 has-preprint 指回 preprint
        vor_factory = make_record_factory("Attention Is All You Need", "Vaswani A")
        vor, _ = await get_or_create_record(session, "10.1/attn-vor", vor_factory)
        vor_edge, vor_materialized = await resolve_and_maybe_materialize(
            session, resolver, vor, {"has-preprint": [{"id": "10.1/ATTN-PREPRINT"}]}
        )

        assert vor_edge.evidence_type == IdentityEvidenceType.EXPLICIT_CROSSREF_RELATION.value
        assert vor_edge.status == IdentityStatus.CONFIRMED.value
        assert vor_materialized is True
        assert vor.work_id == preprint.work_id


# ---------------------------------------------------------------------------
# E: 完全不同的两篇论文，不应该被误合并
# ---------------------------------------------------------------------------


async def test_unrelated_papers_create_separate_works(db: Database):
    async with db.get_session() as session:
        resolver = WorkIdentityResolver(session)

        r1, _ = await get_or_create_record(
            session, "10.1/x1", make_record_factory("Quantum Computing Advances", "Alice A")
        )
        r2, _ = await get_or_create_record(
            session, "10.1/x2", make_record_factory("Deep Sea Fish Migration", "Bob B")
        )

        edge1, m1 = await resolve_and_maybe_materialize(session, resolver, r1)
        edge2, m2 = await resolve_and_maybe_materialize(session, resolver, r2)

        assert m1 is True and m2 is True
        assert r1.work_id != r2.work_id


# ---------------------------------------------------------------------------
# F: 相同标题 + 相同第一作者，但是不同 Work -> 只能是 candidate，不能自动合并
# ---------------------------------------------------------------------------


async def test_same_title_and_first_author_only_produces_candidate(db: Database):
    async with db.get_session() as session:
        resolver = WorkIdentityResolver(session)

        r1, _ = await get_or_create_record(
            session, "10.1/dup1", make_record_factory("A Survey of Machine Learning", "Zhang W")
        )
        edge1, m1 = await resolve_and_maybe_materialize(session, resolver, r1)
        assert m1 is True

        r2, _ = await get_or_create_record(
            session, "10.1/dup2", make_record_factory("A Survey of Machine Learning", "Zhang W")
        )
        edge2, m2 = await resolve_and_maybe_materialize(session, resolver, r2)

        # 关键断言：即使 title+first_author 完全一致，也不能自动 materialize，
        # 必须停在 CANDIDATE 等人工确认——这是本轮修复要保证的核心不变量
        assert edge2.status == IdentityStatus.CANDIDATE.value
        assert edge2.evidence_type == IdentityEvidenceType.EXACT_TITLE_FIRST_AUTHOR.value
        assert m2 is False
        assert r2.work_id is None  # 没有被污染到 r1 的 Work


# ---------------------------------------------------------------------------
# I / J / K: 人工审核路径的可逆性
# ---------------------------------------------------------------------------


async def test_confirm_edge_materializes_record(db: Database):
    async with db.get_session() as session:
        resolver = WorkIdentityResolver(session)

        r1, _ = await get_or_create_record(
            session, "10.1/conf1", make_record_factory("Graph Neural Networks", "Wu Y")
        )
        await resolve_and_maybe_materialize(session, resolver, r1)

        r2, _ = await get_or_create_record(
            session, "10.1/conf2", make_record_factory("Graph Neural Networks", "Wu Y")
        )
        edge2, materialized = await resolve_and_maybe_materialize(session, resolver, r2)
        assert materialized is False

        await resolver.confirm_edge(edge2.id)
        await session.refresh(r2)

        assert r2.work_id == edge2.target_work_id
        assert r2.work_id == r1.work_id


async def test_reject_candidate_edge_leaves_records_separate(db: Database):
    async with db.get_session() as session:
        resolver = WorkIdentityResolver(session)

        r1, _ = await get_or_create_record(
            session, "10.1/rej1", make_record_factory("Statistical Methods Review", "Kim S")
        )
        await resolve_and_maybe_materialize(session, resolver, r1)

        r2, _ = await get_or_create_record(
            session, "10.1/rej2", make_record_factory("Statistical Methods Review", "Kim S")
        )
        edge2, _ = await resolve_and_maybe_materialize(session, resolver, r2)

        await resolver.reject_edge(edge2.id)
        await session.refresh(r2)

        assert r2.work_id is None
        assert edge2.status == IdentityStatus.REJECTED.value


async def test_reject_after_confirm_reverses_materialization(db: Database):
    """
    K: 已经 confirm 过的 edge 被人工撤销，record.work_id 必须真正被清空，
    而不是只把 edge 的状态字段改掉——这是"merge 可逆"这个不变量的直接验证。
    """
    async with db.get_session() as session:
        resolver = WorkIdentityResolver(session)

        r1, _ = await get_or_create_record(
            session, "10.1/undo1", make_record_factory("Language Model Scaling Laws", "Chen X")
        )
        await resolve_and_maybe_materialize(session, resolver, r1)

        r2, _ = await get_or_create_record(
            session, "10.1/undo2", make_record_factory("Language Model Scaling Laws", "Chen X")
        )
        edge2, _ = await resolve_and_maybe_materialize(session, resolver, r2)

        await resolver.confirm_edge(edge2.id)
        await session.refresh(r2)
        assert r2.work_id == r1.work_id  # 确认合并已生效

        await resolver.reject_edge(edge2.id)
        await session.refresh(r2)

        assert r2.work_id is None  # merge 被撤销，不再挂在 r1 的 Work 下
        assert edge2.status == IdentityStatus.REJECTED.value


# ---------------------------------------------------------------------------
# WorkIdentifier 的 DOI 唯一约束（P0-B）
# ---------------------------------------------------------------------------


async def test_work_identifier_doi_uniqueness_enforced_at_db_level(db: Database):
    """
    两个不同的 Work 不能注册同一个规范化后的 DOI identifier——这个 invariant
    必须由数据库 unique constraint 保证，而不能只依赖 resolver 的判断逻辑。
    """
    from src.core.models import WorkIdentifier
    from sqlalchemy.exc import IntegrityError

    async with db.get_session() as session:
        work_a = Work(work_id="WA", title="A", canonical_doi="10.1/dup-doi")
        work_b = Work(work_id="WB", title="B", canonical_doi="10.1/dup-doi")
        session.add_all([work_a, work_b])
        await session.flush()

        session.add(
            WorkIdentifier(work_id=work_a.id, identifier_type="doi", identifier_value="10.1/dup-doi")
        )
        await session.flush()

        session.add(
            WorkIdentifier(work_id=work_b.id, identifier_type="doi", identifier_value="10.1/dup-doi")
        )
        with pytest.raises(IntegrityError):
            await session.flush()


# ---------------------------------------------------------------------------
# 一个 Record 不能同时有两条互相矛盾的 CONFIRMED edge
# ---------------------------------------------------------------------------


async def test_confirm_edge_rejects_second_confirmation_for_same_record(db: Database):
    """
    confirm_edge() 必须拒绝"这个 record 已经有另一条生效 CONFIRMED edge"的情况，
    而不是让 record.work_id 悄悄指向新 Work，同时留一条陈旧但依然是 CONFIRMED
    的旧 edge 在数据库里，产生自相矛盾的历史。
    """
    async with db.get_session() as session:
        resolver = WorkIdentityResolver(session)

        r1, _ = await get_or_create_record(
            session, "10.1/multi1", make_record_factory("Multi Confirm Paper", "Patel R")
        )
        edge_a, _ = await resolve_and_maybe_materialize(session, resolver, r1)  # NEW_WORK, confirmed

        # 手工构造一条指向另一个 Work 的候选 edge，模拟"系统提议了第二种身份判断"
        from src.core.models import IdentityEdge, IdentityEvidenceType

        other_work = Work(work_id="W_other", title="Unrelated")
        session.add(other_work)
        await session.flush()

        edge_b = IdentityEdge(
            source_record_id=r1.id,
            target_work_id=other_work.id,
            evidence_type=IdentityEvidenceType.MANUAL_CONFIRMATION.value,
            confidence=1.0,
            status=IdentityStatus.CANDIDATE.value,
        )
        session.add(edge_b)
        await session.flush()

        with pytest.raises(ValueError, match="already has a CONFIRMED edge"):
            await resolver.confirm_edge(edge_b.id)

        # 确认失败后原有状态不受影响
        await session.refresh(r1)
        assert r1.work_id == edge_a.target_work_id
        assert edge_b.status == IdentityStatus.CANDIDATE.value

        # 先 reject 旧的，再 confirm 新的才应该成功
        await resolver.reject_edge(edge_a.id)
        assert await resolver._find_work_by_identifier("doi", r1.doi) is None
        await resolver.confirm_edge(edge_b.id)
        await session.refresh(r1)
        assert r1.work_id == other_work.id
        assert edge_b.status == IdentityStatus.CONFIRMED.value
