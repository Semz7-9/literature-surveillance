"""
L1 Literature Card Validator

硬约束检查，防止非法输出进入数据库
"""

from .contract import L1Input, L1Output, validate_evidence_permission


class ValidationError(Exception):
    """验证失败"""

    def __init__(self, violations: list[str]):
        self.violations = violations
        super().__init__(f"Validation failed: {', '.join(violations)}")


def validate_l1_output(input_data: L1Input, output: L1Output) -> None:
    """
    验证 L1 输出是否符合约束

    Raises:
        ValidationError: 如果验证失败
    """
    violations = []

    # 1. Evidence Level 权限检查
    is_valid, evidence_violations = validate_evidence_permission(
        input_data.evidence_level, output
    )
    if not is_valid:
        violations.extend(evidence_violations)

    # 2. 字段长度检查（Pydantic 已经做了，但这里再次确认）
    if len(output.one_sentence) > 200:
        violations.append(f"one_sentence too long: {len(output.one_sentence)} > 200")

    if len(output.tags) > 5:
        violations.append(f"Too many tags: {len(output.tags)} > 5")

    if len(output.research_object) > 150:
        violations.append(
            f"research_object too long: {len(output.research_object)} > 150"
        )

    if len(output.major_method) > 150:
        violations.append(f"major_method too long: {len(output.major_method)} > 150")

    if len(output.author_reported_result) > 200:
        violations.append(
            f"author_reported_result too long: {len(output.author_reported_result)} > 200"
        )

    # 3. 禁止的内容检查
    # 不应该出现 "详细方法"、"局限性" 等全文级内容的标志词
    forbidden_phrases = [
        "detailed protocol",
        "step-by-step",
        "study limitation",
        "limitations of this",
        "limitations of our",
        "we acknowledge limitations",
        "future work",
        "we conclude",
        "citation",
        "compared to previous",
    ]

    full_text = (
        output.one_sentence
        + " "
        + output.research_object
        + " "
        + output.major_method
        + " "
        + output.author_reported_result
    ).lower()

    for phrase in forbidden_phrases:
        if phrase in full_text:
            violations.append(f"L1 should not contain full-text details: '{phrase}' found")

    # 4. 必须有实质内容
    if not output.one_sentence.strip():
        violations.append("one_sentence cannot be empty")

    if not output.research_object.strip():
        violations.append("research_object cannot be empty")

    if not output.major_method.strip():
        violations.append("major_method cannot be empty")

    if not output.author_reported_result.strip():
        violations.append("author_reported_result cannot be empty")

    if len(output.tags) == 0:
        violations.append("Must have at least 1 tag")

    # 5. visual_status 只能是 public 或 unavailable
    if output.visual_status not in ["public", "unavailable"]:
        violations.append(
            f"visual_status must be 'public' or 'unavailable', got '{output.visual_status}'"
        )

    if violations:
        raise ValidationError(violations)
