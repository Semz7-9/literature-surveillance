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
