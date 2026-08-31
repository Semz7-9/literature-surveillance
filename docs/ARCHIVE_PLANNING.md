# Batch A · Archive Planning

## 目标

只输入 Topic，可选填写 Focus，系统自动形成：

```text
Operator Context + Shared Background
→ structured ScopeDraft
→ provider-neutral SearchPlan
→ AIProposal
→ at most 5 ReviewItems
→ PAUSED / SEARCHING
```

本批不执行 PubMed/OpenAlex/Semantic Scholar 检索，也不生成 citation graph。

## 权限模型

`AIProposal` 与 `HumanDecision` 是不同记录：

- 自动产生的 ScopeDraft/SearchPlan 标为 provisional + auto-applied；
- 只有歧义或高影响问题进入 ReviewItem；
- 操作者决定 append-only 保存 decision、rationale 与 reviewer metadata；
- 处理 ReviewItem 不覆盖 proposal payload。

因此系统可以高度自动化，同时不会把机器建议伪装成人工结论。

## Operator Context

OperatorProfile 是不可变版本，包含研究兴趣、当前项目、概念偏好、方法论原则、
术语偏好和笔记约定。OperatorLens 保存可跨专题复用的概念、方法、术语或启发式视角。

ArchiveOperatorContext 引用建档当时的 Profile 版本与 Lens IDs。以后更新 Profile 不会
反向改写旧 Archive 的规划输入。

## ScopeDraft 与 SearchPlan

ScopeDraft 是机器建议，不等同于 Expert Mode 中人工维护的正式 ArchiveScope。它保存
core scope、included/excluded domains、temporal/object/method scope、ambiguities 和
reasoning summary。

SearchPlan 只描述 concepts、historical vocabulary、hard/soft exclusions 和 source targets。
它不包含 `[Title/Abstract]` 等数据库语法。Batch B 将用确定性 compiler 分别生成各来源
查询。

## Generation Audit Ledger

每次模型或确定性规划尝试记录 provider/model/role、prompt/text hash、cache key、
cache hit/miss、parse status、retry count、token count（可取得时）、error category、
input refs 与 output hash。Ledger 不保存 API key、秘密或全文。

配置模型失败时，失败 GenerationRun 保留，工作流再使用 deterministic baseline 生成可用
初稿。

## Benchmark

固定用例位于 `benchmarks/archive_cases/targeted_covalent_inhibitors.json`。

```powershell
.\.venv\Scripts\python.exe scripts\accept_archive_planning.py `
  --database data\archive_planning_acceptance.db --reset
```

当前检查 planning 结构、已知术语、计划来源、ReviewItem 上限、建议/决策分离以及关闭
重开后的持久化。Landmark recall 已在 Batch B1 使用真实双源 Corpus 验收；community recall、
FalseBranchRate 和 ClaimFaithfulness 仍分别留给 Batch B2/C，不提前虚构。
