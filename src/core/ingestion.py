"""
Record 摄入逻辑

处理重复 DOI 的检测：一个 DOI 对应一个具体 Record（不是 Work）。
Daily monitor 重复抓取同一篇文献时，应该在这里被拦截，而不是撞到数据库唯一约束。
"""

from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Record, normalize_doi


async def get_record_by_doi(session: AsyncSession, doi: str) -> Optional[Record]:
    """
    通过 DOI 查找已存在的 Record

    Args:
        session: 数据库 session
        doi: DOI（任意大小写/URL 形式，内部会 normalize 后再查询）

    Returns:
        已存在的 Record，如果不存在则返回 None
    """
    stmt = select(Record).where(Record.doi == normalize_doi(doi))
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_or_create_record(
    session: AsyncSession,
    doi: str,
    record_factory,
) -> tuple[Record, bool]:
    """
    获取已存在的 Record，或用 factory 创建新的

    Args:
        session: 数据库 session
        doi: DOI（任意大小写/URL 形式）
        record_factory: 接受一个位置参数 normalized_doi 的可调用对象，返回新的
            Record 实例（尚未 add 到 session）。normalized_doi 由本函数统一计算并
            传入——factory 不需要（也不应该）自己再调用 normalize_doi()，这样
            "DOI 必须先 normalize 才能入库"这个约束由唯一的调用点保证，而不是
            靠每个 factory 的作者自觉遵守。

    Returns:
        (record, created) - created 为 True 表示是新创建的
    """
    normalized_doi = normalize_doi(doi)
    existing = await get_record_by_doi(session, normalized_doi)
    if existing:
        return existing, False

    record = record_factory(normalized_doi)
    session.add(record)
    await session.flush()
    return record, True
