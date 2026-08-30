# Topic Archive v0.1 验收记录

验收日期：2026-08-30

## 最小纵向链

```text
创建专题档案
→ Scope v1
→ Background
→ 2 个 Concept Sets / Lexicon
→ Search Strategy v1（Q1 / Q2）
→ 真实 PubMed 检索
→ Work Core 去重
→ Archive Corpus
→ Timeline
→ Revision Log
```

## 自动回归

固定 PubMed fixture 对两个 Query 返回同一 DOI，验证结果：

- Work: 1
- Record: 1
- DiscoveryEvent: 2（保留每个 Query 的发现 provenance）
- ArchiveWork: 1
- matched queries: Q1 + Q2
- Archive Revision: 9
- Archive Search 不出现在普通 Monitor Inbox
- Work merge 后 Archive membership 自动合并且不指向 tombstone

## 真实 PubMed 验收

命令：

```bash
python scripts/accept_archive_v01.py --database data/archive_acceptance.db --reset
```

主题：Targeted Covalent Inhibitor Design

- Archives: 1
- Scope versions: 1
- Backgrounds: 1
- Concept Sets: 2
- Search Strategies: 1
- 去重后的 Archive Works: 84
- Duplicate memberships: 0
- Revisions: 7

结果：PASS

## 边界

v0.1 只提供 PubMed 检索、基本 Corpus 和按 publication date 排列的 Timeline。
Citation chasing、Claims、Relations、Terms evolution、Unresolved 和 structural
saturation 属于 Archive Intelligence，未纳入本次验收。
