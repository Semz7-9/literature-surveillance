# Literature Monitor v0.1 验收记录

验收日期：2026-08-30

## Gate 1 — Automated regression

- 虚拟时钟推进 7 天
- enabled / disabled / not-due
- backlog 短冷却续跑
- 同日重复运行幂等
- provider 和单订阅故障隔离
- E0 → E1 metadata upgrade
- identity relation 后到恢复
- FAILED / INGESTED / IDENTITY_REVIEW 恢复
- L1 cache 不重复生成
- Scheduler start / stop

结果：完整 pytest suite 通过。

## Gate 2/3 — Historical replay + restart/failure

固定窗口：2026-08-18 至 2026-08-24。每天重新创建 Database 与
MonitorScheduler，第 4 天注入 provider failure，每天调度两次。

### Crossref / JMedChem

- unique works / events: 119 / 119
- duplicate DOI groups: 0
- DiscoveryEvent → MERGED Work: 0
- dangling RUNNING: 0
- remaining backlog: 0
- result: PASS

### PubMed / `"covalent inhibitor"[Title/Abstract]`

- unique works / events: 1 / 1
- duplicate DOI groups: 0
- DiscoveryEvent → MERGED Work: 0
- dangling RUNNING: 0
- remaining backlog: 0
- result: PASS

数据库位于 `data/acceptance_crossref.db` 和 `data/acceptance_pubmed.db`，由
`.gitignore` 排除，不作为产品数据提交。

## Gate 4 — Live wiring

2026-08-29 的短时间真实运行中，应用启动后未点击“立即检查”，Scheduler 自动
运行 4 个订阅并写入 213 个 DiscoveryEvent / Work。页面能够立即读取结果；关闭
桌面窗口后 Uvicorn 与 Scheduler 均正常退出。

## Verdict

四个 Gate 均具备可重复证据。Literature Monitor v0.1 的周期触发、持久状态、
重启恢复、故障隔离、幂等性和真实 provider 接线通过验收。
