# Phase 0 开发计划

## 目标

验证核心假设，不追求完整功能。

## 验收标准

给定 10 个真实 DOI：

1. ✅ 能从 Crossref 获取 metadata
2. ✅ Work identity resolution 准确率 >90%
3. ✅ Abstract → L1 输出符合 schema
4. ✅ Validator 能阻止非法输出
5. ✅ 数据正确存入 SQLite
6. ✅ 能生成可读的 Markdown

## 已完成组件

### 数据模型
- [x] `src/core/models.py` - 完整的三维模型（Evidence/Publication/Claim）
- [x] `src/core/database.py` - SQLite + WAL 配置
- [x] `src/core/config.py` - 配置管理

### Adapters
- [x] `src/adapters/crossref.py` - Crossref API 适配器（含限速）

### Core Logic
- [x] `src/core/work_identity.py` - Work identity resolver（含 provenance）

### Skills
- [x] `skills/l1_literature_card/contract.py` - L1 输入输出 schema
- [x] `skills/l1_literature_card/validator.py` - 硬约束验证
- [x] `skills/l1_literature_card/SKILL.md` - Skill 文档

### Tests
- [x] `tests/test_phase0.py` - Phase 0 验证脚本

## 待完成（关键）

### 1. LLM API 集成
需要创建 `src/llm/client.py`，支持：
- OpenAI API
- Anthropic API
- 统一的 `call_with_schema()` 接口

### 2. 真实 L1 生成
将 `test_phase0.py` 中的 `mock_llm_call()` 替换为真实调用。

### 3. 配置文件
创建 `config.yaml` 并填入真实的：
- Crossref email
- LLM API key

### 4. 人工验证
运行 Phase 0 后，需要人工检查：
- Work identity 是否正确匹配
- L1 的事实准确性
- null 处理是否合理

## 运行步骤

```bash
# 1. 安装依赖
pip install -e .

# 2. 创建配置文件
python -c "from src.core.config import create_default_config; create_default_config()"

# 3. 编辑 config.yaml，填入真实信息
# vim config.yaml

# 4. 运行 Phase 0 验证
python tests/test_phase0.py

# 5. 检查输出
# - data/test_phase0.db (SQLite)
# - archives/test/*.md (Markdown)
```

## 预期问题

1. **Work identity 误匹配** - 需要调整 fuzzy matching 策略
2. **L1 包含全文级信息** - 需要加强 validator 或调整 prompt
3. **Crossref API 限速触发** - 需要观察实际 rate limit
4. **某些 DOI 没有 abstract** - 确认 E0 不能生成 L1

## Phase 0 完成标志

当以下条件满足时，Phase 0 完成：

- [ ] 10 个测试 DOI 全部成功处理
- [ ] Work identity accuracy >90% (人工检查)
- [ ] L1 factual accuracy >90% (人工检查)
- [ ] Validator 成功阻止至少 1 个非法输出
- [ ] SQLite 数据可查询
- [ ] Markdown 可在 Obsidian 中打开

完成后进入 **Phase 1: 日常文献追踪**。

## 已知限制（Phase 0 不解决）

- 没有 Web UI
- 没有定时任务
- 没有 L2/L3
- 没有 Archive Builder
- Work identity 只支持简单匹配
- 只支持 DOI 输入，不支持 PubMed/RSS
