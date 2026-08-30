# Archive Automation Foundation 验收

## 产品边界

默认流程从“操作者填写检索参数”重置为：

```text
Topic + optional focus
→ ArchiveBuildRun
→ provisional Scope
→ shared Background resolution
→ high confidence auto-attach
→ medium confidence operator review
```

旧的 Scope、私有 Background、Concept Set、Term 状态、Boolean Query 与真实 PubMed
检索能力没有删除，统一收进 `Advanced / Expert Mode`。

## 持久化状态

`ArchiveBuildRun` 保存输入版本、当前 state、运行 status、开始/结束时间和错误。
`ArchiveBuildStep` 为 SCOPING、BACKGROUND、LEXICON、SEARCHING、ASSEMBLING 分别保存：

- attempt；
- started_at / finished_at；
- status；
- input_version；
- output_artifact；
- error。

失败步骤以新 attempt 重试，已完成步骤不会重复执行。本批完成 SCOPING 与 BACKGROUND
后明确以 `PAUSED / LEXICON` 保存，不伪装成完整 Archive 已 READY。

## Shared Background Library

背景正文由 `BackgroundProfile → BackgroundNode` 共享，Archive 只保存
`ArchiveBackgroundLink`。高置信节点自动挂接，中置信节点作为 candidate 进入页面待审，
低置信节点不载入。

内容层严格分离：

```text
Canonical Core
Operator Contributions (immutable raw text)
AI Contributions (provider/model/role/input_refs/output)
Sources
```

AI workflow 只请求 `SOURCE_SYNTHESIS`、`REASONING`、`CRITIQUE` role，供应商由
`RoleBasedModelRouter` 注册，不写入 Archive Builder 的控制流。

## 固定验收

```powershell
.\.venv\Scripts\python.exe scripts\accept_archive_automation_foundation.py `
  --database data\archive_automation_acceptance.db --reset
```

输入仅为 `Targeted Covalent Inhibitor Design`。验收检查：

- provisional Scope 已保存；
- BuildRun 停在可恢复的 `PAUSED / LEXICON`；
- SCOPING、BACKGROUND 完成，其余步骤保持 PENDING；
- 三个高置信 BackgroundNode 自动挂接；
- Chemoproteomics 作为中置信问题进入 Review Queue；
- Profile / Node 是共享对象而非 Archive 正文副本；
- 关闭并重新创建 App 后状态仍可读取。

## 明确未做

- 自动完整 Scope；
- 自动 Concept Lexicon / MeSH；
- 自动 Query Family；
- citation chasing；
- Archive Assembly 与完整 Review Queue；
- Monitor → Archive Delta；
- 多 Agent 编排。
