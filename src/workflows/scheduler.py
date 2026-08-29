"""Small in-process scheduler that makes enabled subscriptions unattended."""

import asyncio
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from sqlalchemy import select

from ..core.database import Database
from ..core.models import MonitorSubscription, Source, SourceCursor
from .monitor import run_monitor_subscription


class MonitorScheduler:
    def __init__(
        self,
        database: Database,
        adapter_factory: Callable[[Source], object],
        llm_factory: Callable[[], Awaitable[object | None]],
        *,
        default_interval_hours: int = 24,
        poll_seconds: int = 60,
    ):
        self.database = database
        self.adapter_factory = adapter_factory
        self.llm_factory = llm_factory
        self.default_interval_hours = default_interval_hours
        self.poll_seconds = poll_seconds
        self.task: asyncio.Task | None = None
        self.last_tick: datetime | None = None

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._loop(), name="literature-monitor-scheduler")

    async def stop(self) -> None:
        if self.task is None:
            return
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass
        self.task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_due_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A single subscription failure is persisted by the workflow;
                # an unexpected tick failure must not kill tomorrow's checks.
                pass
            await asyncio.sleep(self.poll_seconds)

    async def run_due_once(self, *, now: datetime | None = None) -> list[int]:
        now = now or datetime.utcnow()
        self.last_tick = now
        async with self.database.get_session() as session:
            subscription_ids = (await session.execute(
                select(MonitorSubscription).where(
                    MonitorSubscription.enabled.is_(True)
                ).order_by(MonitorSubscription.id)
            )).scalars().all()
            subscription_ids = [subscription.id for subscription in subscription_ids]
        due_ids: list[int] = []
        for subscription_id in subscription_ids:
            adapter = None
            llm_client = None
            async with self.database.get_session() as session:
                subscription = await session.get(MonitorSubscription, subscription_id)
                if subscription is None or not subscription.enabled:
                    continue
                cursor = (await session.execute(select(SourceCursor).where(
                    SourceCursor.subscription_id == subscription.id
                ))).scalar_one_or_none()
                if cursor and cursor.last_checked_at:
                    if (cursor.state or {}).get("has_more"):
                        cooldown = timedelta(minutes=max(1, int(subscription.config.get(
                            "backlog_cooldown_minutes", 5
                        ))))
                    else:
                        cooldown = timedelta(hours=max(1, int(subscription.config.get(
                            "interval_hours", self.default_interval_hours
                        ))))
                    if cursor.last_checked_at > now - cooldown:
                        continue
                source = await session.get(Source, subscription.source_id)
                if source is None:
                    continue
                try:
                    adapter = self.adapter_factory(source)
                    llm_client = await self.llm_factory()
                    await run_monitor_subscription(
                        session, subscription, adapter, llm_client, now=now
                    )
                    await session.commit()
                    due_ids.append(subscription.id)
                except Exception:
                    await session.rollback()
                    continue
                finally:
                    if adapter:
                        await adapter.close()
                    if llm_client:
                        await llm_client.close()
        return due_ids
