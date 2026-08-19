"""
L1 Literature Card Validator
"""

from .contract import L1Input, L1Output, validate_evidence_permission


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
    if len(output.tags) > 5:
        violations.append(f"Too many tags: {len(output.tags)} > 5")
    if output.research_object and len(output.research_object) > 150:
        violations.append(f"research_object too long: {len(output.research_object)} > 150")
    if output.major_method and len(output.major_method) > 150:
        violations.append(f"major_method too long: {len(output.major_method)} > 150")
    if output.author_reported_result and len(output.author_reported_result) > 200:
        violations.append(f"author_reported_result too long: {len(output.author_reported_result)} > 200")

    # 3. Nullable consistency: None field must have a missing_reason
    nullable_pairs = [
        ("research_object", "research_object_missing_reason"),
        ("major_method", "major_method_missing_reason"),
        ("author_reported_result", "author_reported_result_missing_reason"),
    ]
    for field, reason_field in nullable_pairs:
        if getattr(output, field) is None and getattr(output, reason_field) is None:
            violations.append(
                f"{field} is null but {reason_field} is also null — must provide a reason"
            )

    # 4. Evidence span grounding: each span must appear verbatim in the abstract
    if input_data.abstract:
        for field_name, span in output.evidence_spans.items():
            if span and span not in input_data.abstract:
                violations.append(
                    f"evidence_spans['{field_name}'] not found verbatim in abstract: "
                    f"{span[:80]!r}"
                )

        # 5. Non-null fields must have evidence spans grounding them
        for field in ("research_object", "major_method", "author_reported_result"):
            if getattr(output, field) is not None and field not in output.evidence_spans:
                violations.append(
                    f"{field} is set but missing from evidence_spans — "
                    "add a verbatim quote from the abstract"
                )

    # 6. Forbidden phrases (full-text content leak check)
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
    full_text = " ".join(
        v for v in [
            output.one_sentence,
            output.research_object,
            output.major_method,
            output.author_reported_result,
        ]
        if v
    ).lower()
    for phrase in forbidden_phrases:
        if phrase in full_text:
            violations.append(f"L1 should not contain full-text details: '{phrase}' found")

    # 7. Required non-null fields
    if not output.one_sentence or not output.one_sentence.strip():
        violations.append("one_sentence cannot be empty")
    if len(output.tags) == 0:
        violations.append("Must have at least 1 tag")

    if violations:
        raise ValidationError(violations)
