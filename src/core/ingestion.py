"""
Record 摄入逻辑

处理重复 DOI 的检测：一个 DOI 对应一个具体 Record（不是 Work）。
Daily monitor 重复抓取同一篇文献时，应该在这里被拦截，而不是撞到数据库唯一约束。
"""

from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Record


async def get_record_by_doi(session: AsyncSession, doi: str) -> Optional[Record]:
    """
    通过 DOI 查找已存在的 Record

    Args:
        session: 数据库 session
        doi: DOI

    Returns:
        已存在的 Record，如果不存在则返回 None
    """
    stmt = select(Record).where(Record.doi == doi)
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
        doi: DOI
        record_factory: 无参可调用对象，返回新的 Record 实例（尚未 add 到 session）

    Returns:
        (record, created) - created 为 True 表示是新创建的
    """
    existing = await get_record_by_doi(session, doi)
    if existing:
        return existing, False

    record = record_factory()
    session.add(record)
    await session.flush()
    return record, True
