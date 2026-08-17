# L1 Literature Card

从 Abstract 生成分级阅读卡片的第一层。

## 目标

让用户在 **10-20 秒内** 决定是否有兴趣深入阅读。

## 输入要求

- **必须有 Abstract** (Evidence Level E1+)
- E0 (仅 metadata) 无法生成 L1

## 输出结构

```yaml
one_sentence: 一句话总结（≤200字符）
tags: [3-5个标签]
research_object: 研究对象（蛋白/化合物/细胞系等）
major_method: 主要方法
author_reported_result: 作者报告的主要结果
visual_status: public | unavailable
```

## 约束

### 必须遵守
- 所有信息来自 Abstract
- 不得包含详细方法步骤
- 不得包含局限性、未来工作
- 不得包含引用关系、历史比较

### 禁止词汇
- "detailed protocol"
- "step-by-step"
- "limitation"
- "future work"
- "citation"
- "compared to previous"

## Validator

所有输出必须通过 `validator.py` 的硬约束检查：
- Evidence Level 权限
- 字段长度
- 禁止内容
- 非空检查

验证失败会抛出 `ValidationError`，阻止数据进入数据库。

## Prompt 模板

```
You are extracting key information from a scientific abstract to create a quick-scan literature card.

Input:
- Title: {title}
- Authors: {authors}
- Journal: {journal}
- Abstract: {abstract}

Output a JSON with:
1. one_sentence: A single sentence (max 200 chars) summarizing the core contribution
2. tags: 3-5 tags (research type, field, technique)
3. research_object: Main object of study (protein, compound, cell line, etc.)
4. major_method: Primary methods or platforms used
5. author_reported_result: Main result as reported in abstract
6. visual_status: "public" if figures likely available, "unavailable" if not

Constraints:
- ALL information must come from the abstract
- NO detailed protocols, limitations, future work, or citation context
- Keep it concise - this is for quick scanning, not deep reading

Output valid JSON matching the schema.
```

## 使用示例

```python
from skills.l1_literature_card import L1Input, L1Output, validate_l1_output

# 准备输入
input_data = L1Input(
    work_id="W001",
    record_id="R001",
    title="Discovery of covalent inhibitors...",
    authors=["Smith J", "Chen L"],
    abstract="KRAS mutations are prevalent...",
    evidence_level="E1"
)

# 调用 LLM
output = call_llm_with_schema(input_data, L1Output)

# 验证
validate_l1_output(input_data, output)  # 抛出异常如果验证失败

# 存储
save_to_db(output)
```

## 设计原则

**Evidence-driven**: E0 不能生成 L1，这是硬约束，不是建议。

**Validator 优先于 Prompt**: 不依赖模型 "克制"，而是用代码强制执行边界。

**null 是合法的**: 如果 abstract 没有提到某个信息，应该留空或使用 "not specified"。
