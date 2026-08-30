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

Windows 也可以双击根目录的 `launch_desktop.pyw`。桌面窗口会在内部启动仅绑定
`127.0.0.1` 随机端口的本地服务，不会打开浏览器，也不会向局域网暴露端口；关闭
窗口时，本地服务和 Monitor Scheduler 会一同正常退出。

## Literature Monitor v0.1

开发调试时仍可单独启动 Web UI：

```bash
uvicorn src.web.app:app --reload
```

打开 `http://127.0.0.1:8000` 可管理 Crossref / PubMed 期刊与主题订阅，设置启停和检查频率，并在自然周收件箱执行 Keep、Ignore、Queue L2。默认使用 `config.yaml` 的数据库路径；没有该文件时使用 `data/literature.db`。

将 `config.yaml` 中的 `monitor.enabled` 设为 `true` 后，应用内调度器会按每个订阅的频率自动运行；无需打开页面或点击检查，但运行 `uvicorn` 的进程需要保持在线。系统会把新发现送入现有的 Work identity、Snapshot 和 L1 流程。PubMed 会补充 Abstract 与 MeSH；没有配置 LLM API key 时仍会完成发现和 L0/E1 入库，但不会生成新的 L1 卡片。“立即检查”保留用于首次配置和排障。
