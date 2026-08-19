"""
L1 Literature Card Validator
"""

from .contract import (
    L1Input,
    L1Output,
    validate_evidence_permission,
    ALLOWED_EVIDENCE_SPAN_KEYS,
)


class ValidationError(Exception):
    def __init__(self, violations: list[str]):
        self.violations = violations
        super().__init__(f"Validation failed: {', '.join(violations)}")


def validate_l1_output(input_data: L1Input, output: L1Output) -> None:
    violations = []

    # 1. Evidence Level permission
    is_valid, evidence_violations = validate_evidence_permission(
        input_data.evidence_level, output
    )
    if not is_valid:
        violations.extend(evidence_violations)

    # 2. Length checks (only for non-None values)
    if output.one_sentence and len(output.one_sentence) > 200:
        violations.append(f"one_sentence too long: {len(output.one_sentence)} > 200")
    if output.research_object and len(output.research_object) > 150:
        violations.append(f"research_object too long: {len(output.research_object)} > 150")
    if output.major_method and len(output.major_method) > 150:
        violations.append(f"major_method too long: {len(output.major_method)} > 150")
    if output.author_reported_result and len(output.author_reported_result) > 200:
        violations.append(f"author_reported_result too long: {len(output.author_reported_result)} > 200")

    # 3. evidence_spans key whitelist — no untracked/unknown fields allowed
    for key in output.evidence_spans:
        if key not in ALLOWED_EVIDENCE_SPAN_KEYS:
            violations.append(
                f"evidence_spans has unknown key '{key}' (allowed: {sorted(ALLOWED_EVIDENCE_SPAN_KEYS)})"
            )

    # 4. Strict XOR per nullable field:
    #    value set     <-> missing_reason is None <-> evidence_spans has an entry
    #    value is None <-> missing_reason is set  <-> evidence_spans has NO entry
    nullable_fields = [
        ("research_object", "research_object_missing_reason"),
        ("major_method", "major_method_missing_reason"),
        ("author_reported_result", "author_reported_result_missing_reason"),
    ]
    for field, reason_field in nullable_fields:
        value = getattr(output, field)
        reason = getattr(output, reason_field)
        has_span = field in output.evidence_spans

        if value is None and reason is None:
            violations.append(f"{field} is null but {reason_field} is also null — must provide a reason")
        if value is not None and reason is not None:
            violations.append(f"{field} is set but {reason_field} is also set — pick exactly one")
        if value is None and has_span:
            violations.append(f"{field} is null but evidence_spans still has an entry for it")
        if value is not None and not has_span:
            violations.append(f"{field} is set but missing from evidence_spans — add a verbatim quote")

    # 5. Evidence span grounding: each span must appear verbatim in the abstract
    if input_data.abstract:
        for field_name, span in output.evidence_spans.items():
            if span and span not in input_data.abstract:
                violations.append(
                    f"evidence_spans['{field_name}'] not found verbatim in abstract: {span[:80]!r}"
                )

    # 6. Forbidden phrases (full-text content leak check)
    forbidden_phrases = [
        "detailed protocol", "step-by-step", "study limitation",
        "limitations of this", "limitations of our", "we acknowledge limitations",
        "future work", "we conclude", "citation", "compared to previous",
    ]
    full_text = " ".join(
        v for v in [output.one_sentence, output.research_object, output.major_method, output.author_reported_result]
        if v
    ).lower()
    for phrase in forbidden_phrases:
        if phrase in full_text:
            violations.append(f"L1 should not contain full-text details: '{phrase}' found")

    # 7. one_sentence required (tags count is now enforced by Pydantic min_length=3)
    if not output.one_sentence or not output.one_sentence.strip():
        violations.append("one_sentence cannot be empty")

    if violations:
        raise ValidationError(violations)
