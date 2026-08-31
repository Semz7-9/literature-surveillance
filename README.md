# 学术文献监控与档案系统

一个面向个人长期使用的学术文献监控、分级阅读与专题知识归档系统。

## 核心定位

解决两个核心问题：

1. **某个感兴趣领域过去是怎么发展起来的？** (Archive Builder)
2. **我关注的领域最近又出现了什么？** (Literature Monitor)

## 设计原则

- **Evidence-driven**: 所有信息必须标明证据等级，不知道就是合法答案
- **Provenance everywhere**: 包括 Work identity resolution 在内的所有判断都要有来源
- **Human-in-the-loop**: 系统负责发现和组织，人负责兴趣判断和最终解释
- **Structured first**: 能用软件硬约束的不要只写进 prompt

## 三维知识模型

```
Evidence Level (我们看到了什么材料)
  × 
Publication Status (论文的出版状态)
  ×
Claim Status (基于它的断言是否有效)
```

## 开发路线

### Phase 0: 验证单篇论文处理 (当前)
- DOI/metadata 获取
- Work identity resolution
- Abstract → L1 structured output
- Validator + SQLite + Markdown

### Phase 1: 日常文献追踪
- Daily monitor
- Reading queue (L0 → L1 → L2 → L3)
- Web UI
- Obsidian integration

### Phase 2: 专题学术档案
- Concept lexicon
- Multi-source search strategy
- Citation graph expansion
- Archive incremental update

## 技术栈

- **Runtime**: Python 3.11+
- **Data**: SQLite (WAL mode) + Markdown
- **LLM**: API-agnostic (OpenAI/Anthropic/etc.)
- **Workflow**: 抽象接口 + 本地实现（Phase 1 后可选 Hatchet/Temporal）
- **Frontend**: Web UI (基础) + Obsidian (可选)

## 项目结构

```
.
├── src/
│   ├── core/           # 核心数据模型
│   ├── adapters/       # 外部数据源适配器
│   ├── skills/         # LLM 任务规程
│   ├── workflows/      # 固定流程
│   ├── validators/     # 输出校验
│   └── scripts/        # 确定性计算
├── skills/             # Skill 定义
├── background/         # 学科背景档案
├── archives/           # 专题档案（Markdown）
├── data/              # SQLite 数据库
└── tests/
```

## License

MIT

## 本地桌面应用

安装项目后直接启动桌面窗口：

```bash
pip install -e .
literature-surveillance
```

Windows 推荐双击根目录的 `启动文献助手.cmd`；它会固定使用项目的 `.venv`，启动失败时
保留错误信息。也可以双击 `launch_desktop.pyw`，该入口会自动转交给 `.venv`，并在失败时
弹窗提示、把详情写入 `data/desktop-startup.log`。桌面窗口会在内部启动仅绑定
`127.0.0.1` 随机端口的本地服务，不会打开浏览器，也不会向局域网暴露端口；关闭
窗口时，本地服务和 Monitor Scheduler 会一同正常退出。

## Literature Monitor v0.1

开发调试时仍可单独启动 Web UI：

```bash
uvicorn src.web.app:app --reload
```

打开 `http://127.0.0.1:8000` 可管理 Crossref / PubMed 期刊与主题订阅，设置启停和检查频率，并在自然周收件箱执行 Keep、Ignore、Queue L2。默认使用 `config.yaml` 的数据库路径；没有该文件时使用 `data/literature.db`。

将 `config.yaml` 中的 `monitor.enabled` 设为 `true` 后，应用内调度器会按每个订阅的频率自动运行；无需打开页面或点击检查，但运行 `uvicorn` 的进程需要保持在线。系统会把新发现送入现有的 Work identity、Snapshot 和 L1 流程。PubMed 会补充 Abstract 与 MeSH；没有配置 LLM API key 时仍会完成发现和 L0/E1 入库，但不会生成新的 L1 卡片。“立即检查”保留用于首次配置和排障。

## Auto Archive Builder

默认创建专题只需要 `topic`，可选填写 `focus`。系统会持久化
`ArchiveBuildRun`，自动形成 provisional Scope，并从 Shared Background Library
挂接高置信背景节点；中置信节点进入 Review Queue。关闭桌面程序再打开后，步骤状态、
输入版本、输出产物与错误仍然存在。

```text
Topic + optional focus
→ persisted BuildRun
→ provisional Scope
→ BackgroundResolver
→ shared Background links
→ operator review
```

Foundation 已由 Batch A · Archive Planning 接续：系统现在生成结构化 ScopeDraft、
provider-neutral SearchPlan 与最多 5 个高影响 ReviewItem，然后明确停在 `SEARCHING`
等待 Batch B。机器建议与人工决定分别保存，模型调用只在 Audit Ledger 中保存 hash 和
运行元数据，不保存 API key 或全文。

```text
Operator Context + Shared Background
→ ScopeDraft → Semantic SearchPlan → AIProposal
→ exception-driven ReviewItem → HumanDecision (separate, append-only)
→ PAUSED / SEARCHING
```

固定 Planning benchmark 验收：

```bash
python scripts/accept_archive_planning.py \
  --database data/archive_planning_acceptance.db --reset
```

Foundation 验收仍可独立运行：

```bash
python scripts/accept_archive_automation_foundation.py \
  --database data/archive_automation_acceptance.db --reset
```

详细边界见 `docs/ARCHIVE_PLANNING.md` 和
`docs/ARCHIVE_AUTOMATION_FOUNDATION.md`。

## Topic Archive v0.1（Expert Mode）

“专题档案”支持完整的本地纵向工作流：

```text
创建 Archive → 保存版本化 Scope → 挂接 Background
→ 建立带状态和来源的 Concept Sets / Terms
→ 生成版本化 PubMed Boolean Queries
→ 执行真实检索 → Work Core 去重
→ Archive Corpus → 基本 Timeline → Revision Log
```

这些手工能力现在位于 `Advanced / Expert Mode`，作为可审计编辑和排障工具保留。
Archive Search 使用隐藏的一次性订阅复用现有 ingestion/identity 核心，但不会混入
普通 Monitor Inbox。可运行隔离的真实 PubMed 验收：

```bash
python scripts/accept_archive_v01.py --database data/archive_acceptance.db --reset
```

## Monitor 时间压缩验收

不用真实等待一周，可以将历史 API 数据按虚拟日期逐日回放：

```bash
python scripts/simulate_monitor_period.py \
  --from 2026-08-18 --to 2026-08-24 \
  --provider crossref --name acceptance-jmedchem --issn 1520-4804 \
  --database data/acceptance_crossref.db --reset \
  --restart-each-day --repeat-each-day 2 --fail-on-day 4
```

脚本使用隔离数据库，支持每天重启、同日重复调度、指定日期 provider 故障，
并检查重复 DOI、MERGED Work 引用、悬挂运行与未清 backlog。完整验收记录见
`docs/MONITOR_ACCEPTANCE.md`。
