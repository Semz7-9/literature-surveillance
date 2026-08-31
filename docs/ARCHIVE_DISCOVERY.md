# Batch B1 · Archive Discovery Core

## 已实现的纵向链

```text
SearchPlan + HumanDecision
→ EffectiveSearchPlan
→ PubMedCompiler + OpenAlexCompiler
→ RetrievalRun + RetrievalHit
→ existing Record / Work Resolver
→ ArchiveWork corpus
→ source contribution + query coverage + landmark recall
```

`HumanDecision` 不改写原始 AIProposal。Resolver 根据每个决定携带的检索词，把 INCLUDE
加入检索 concept、EXCLUDE 加入 hard exclusions、BACKGROUND_ONLY 单独保留为背景词，
然后生成一个可执行的 EffectiveSearchPlan 版本。实际检索只读取该投影。

## Query 与来源边界

- LLM 只生成 provider-neutral concepts、历史词和排除项；
- PubMedCompiler 负责 `[Title/Abstract]` 与 Boolean 结构；
- OpenAlexCompiler 负责 OpenAlex full-text search 请求；
- PubMed 使用 relevance 排序，避免归档检索退化为“只拿最近 50 篇”；
- 当前只支持 PubMed + OpenAlex，Semantic Scholar 留到 graph 补全需要被数据证明时再接。

每个来源失败分别记录。一个来源失败时，另一个来源成功形成的 Corpus 仍会提交，
RetrievalRun 标为 `COMPLETED_WITH_ERRORS`，而不是丢掉部分成果。

## 跨来源 identity

RetrievalHit 是薄观察层，不是第二套 Discovery Core。原始元数据进入已有 Record 摄入、
SourceSnapshot 和 WorkIdentityResolver。DOI、PMID 与 OpenAlex ID 均可成为 WorkIdentifier；
ArchiveWork 只引用 canonical Work，因此两个来源命中同一 DOI/PMID 不会制造两个档案成员。

## 真实 benchmark

ground truth 位于 `benchmarks/archive_cases/targeted_covalent_inhibitors.json`，包含：

- 10 篇 DOI 标识的 landmark works；
- 7 篇 known reviews；
- 11 个 known terminology；
- landmark recall 目标 `>= 0.80`。

运行：

```powershell
.\.venv\Scripts\python.exe scripts\accept_archive_discovery.py `
  --database data\archive_discovery_acceptance.db --reset `
  --max-results-per-query 50
```

2026-08-31 的固定验收结果：

```text
PubMed hits       196
OpenAlex hits     200
Canonical Works  290
Landmark recall  8 / 10 = 0.80
```

原始 hit 数会随远端索引变化；验收的核心不是“数字越大越好”，而是 canonical corpus 非空、
双源贡献可见且 landmark recall 不低于 ground truth 阈值。

## 明确未做

Batch B1 不包含 citation edges、centrality、community detection、repair loop 或 Archive
knowledge skeleton。这些依次属于 B2、B3、B4。下一步直接使用当前 290-Work Corpus 做
Citation Landscape，而不是继续扩展规划层状态。
