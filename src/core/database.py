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
            # UI-0/Monitor MVP 使用 create_all 管理本地 Phase-1 数据库。
            # create_all 不会给已有表补列，因此为纯 additive 变更提供一个
            # 极小兼容层；正式迁移体系仍留给后续 Alembic 阶段。
            columns = {
                row[1] for row in (
                    await conn.execute(text("PRAGMA table_info(discovery_events)"))
                ).fetchall()
            }
            if columns and "last_seen_at" not in columns:
                await conn.execute(text("ALTER TABLE discovery_events ADD COLUMN last_seen_at DATETIME"))
            if columns and "last_metadata_hash" not in columns:
                await conn.execute(
                    text("ALTER TABLE discovery_events ADD COLUMN last_metadata_hash VARCHAR(64)")
                )
            archive_columns = {
                row[1] for row in (
                    await conn.execute(text("PRAGMA table_info(topic_archives)"))
                ).fetchall()
            }
            if archive_columns and "focus" not in archive_columns:
                await conn.execute(
                    text("ALTER TABLE topic_archives ADD COLUMN focus TEXT NOT NULL DEFAULT ''")
                )
            if archive_columns and "background_mode" not in archive_columns:
                await conn.execute(text(
                    "ALTER TABLE topic_archives ADD COLUMN background_mode VARCHAR(16) "
                    "NOT NULL DEFAULT 'AUTO'"
                ))
            await conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_source_health_source_id "
                "ON source_health(source_id)"
            ))
            await conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_monitor_runs_one_running_per_subscription "
                "ON monitor_runs(subscription_id) WHERE status = 'RUNNING'"
            ))

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
