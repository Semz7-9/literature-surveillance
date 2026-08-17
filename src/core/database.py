"""
Database 初始化和连接管理
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool
from pathlib import Path

from ..core.models import Base


class Database:
    """数据库管理"""

    def __init__(self, db_path: str | Path):
        """
        Args:
            db_path: SQLite 数据库路径
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # 创建引擎（WAL 模式在连接时设置）
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{self.db_path}",
            poolclass=NullPool,  # SQLite 不需要连接池
            echo=False,
        )

        # Session factory
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def init_db(self):
        """初始化数据库（创建表）"""
        async with self.engine.begin() as conn:
            # 启用 WAL 模式
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.execute(text("PRAGMA busy_timeout=5000"))  # 5秒超时
            await conn.execute(text("PRAGMA foreign_keys=ON"))

            # 创建所有表
            await conn.run_sync(Base.metadata.create_all)

    async def close(self):
        """关闭数据库连接"""
        await self.engine.dispose()

    def get_session(self) -> AsyncSession:
        """获取数据库 session"""
        return self.session_factory()

    async def __aenter__(self):
        await self.init_db()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
