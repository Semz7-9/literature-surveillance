"""
L1 Literature Card validator 的 regression test

不调用真实 LLM——直接手工构造 L1Input/L1Output 驱动 validate_l1_output()，
覆盖 review 里指出的 XOR 逻辑漏洞（P0 item 4）：
- value 与 missing_reason 必须恰好设置一个，不能两个都设/两个都不设
- evidence_spans 必须与"字段是否为 null"完全对应，不能一边为 null 一边留着证据
- evidence_spans 的 key 必须落在白名单里
- evidence_spans 的引用必须是摘要原文的逐字子串
- tags 少于 3 个在 Pydantic 构造阶段就应该失败，不需要走到 validator
"""

import pytest

from skills.l1_literature_card.contract import L1Input, L1Output, MissingReason
from skills.l1_literature_card.validator import validate_l1_output, ValidationError


ABSTRACT = (
    "KRAS mutations drive tumor growth in multiple cancer types. "
    "We used structure-based design to develop covalent inhibitors. "
    "The lead compound showed an IC50 of 8 nM in biochemical assays."
)


def make_input(**overrides) -> L1Input:
    data = dict(
        work_id="W001",
        record_id="R001",
        title="Discovery of covalent inhibitors targeting KRASG12C",
        authors=["Smith J"],
        abstract=ABSTRACT,
        evidence_level="E1",
        snapshot_id=1,
    )
    data.update(overrides)
    return L1Input(**data)


def make_output(**overrides) -> L1Output:
    data = dict(
        one_sentence="Developed covalent inhibitors targeting KRASG12C mutant protein.",
        tags=["covalent inhibitor", "KRAS", "cancer"],
        research_object="KRASG12C mutant protein",
        research_object_missing_reason=None,
        major_method="structure-based design",
        major_method_missing_reason=None,
        author_reported_result="IC50 of 8 nM",
        author_reported_result_missing_reason=None,
        evidence_spans={
            "research_object": "KRAS mutations drive tumor growth",
            "major_method": "We used structure-based design",
            "author_reported_result": "showed an IC50 of 8 nM",
        },
    )
    data.update(overrides)
    return L1Output(**data)


# ---------------------------------------------------------------------------
# Valid baseline case
# ---------------------------------------------------------------------------


def test_valid_output_passes():
    input_data = make_input()
    output = make_output()
    validate_l1_output(input_data, output)  # should not raise


# ---------------------------------------------------------------------------
# XOR: value / missing_reason
# ---------------------------------------------------------------------------


def test_value_none_and_reason_none_is_rejected():
    input_data = make_input()
    output = make_output(
        research_object=None,
        research_object_missing_reason=None,
        evidence_spans={
            "major_method": "We used structure-based design",
            "author_reported_result": "showed an IC50 of 8 nM",
        },
    )
    with pytest.raises(ValidationError) as exc_info:
        validate_l1_output(input_data, output)
    assert any("research_object" in v and "null" in v for v in exc_info.value.violations)


def test_value_set_and_reason_set_is_rejected():
    input_data = make_input()
    output = make_output(
        research_object="KRASG12C mutant protein",
        research_object_missing_reason=MissingReason.NOT_STATED,
    )
    with pytest.raises(ValidationError) as exc_info:
        validate_l1_output(input_data, output)
    assert any(
        "research_object" in v and "also set" in v for v in exc_info.value.violations
    )


# ---------------------------------------------------------------------------
# XOR: value / evidence_spans presence
# ---------------------------------------------------------------------------


def test_value_none_but_evidence_span_present_is_rejected():
    input_data = make_input()
    output = make_output(
        research_object=None,
        research_object_missing_reason=MissingReason.NOT_STATED,
        # deliberately leave the evidence span in place despite the null value
        evidence_spans={
            "research_object": "KRAS mutations drive tumor growth",
            "major_method": "We used structure-based design",
            "author_reported_result": "showed an IC50 of 8 nM",
        },
    )
    with pytest.raises(ValidationError) as exc_info:
        validate_l1_output(input_data, output)
    assert any(
        "research_object" in v and "evidence_spans" in v for v in exc_info.value.violations
    )


def test_value_set_but_no_evidence_span_is_rejected():
    input_data = make_input()
    output = make_output(
        evidence_spans={
            "major_method": "We used structure-based design",
            "author_reported_result": "showed an IC50 of 8 nM",
        }
    )
    with pytest.raises(ValidationError) as exc_info:
        validate_l1_output(input_data, output)
    assert any(
        "research_object" in v and "missing from evidence_spans" in v
        for v in exc_info.value.violations
    )


# ---------------------------------------------------------------------------
# evidence_spans key whitelist
# ---------------------------------------------------------------------------


def test_unknown_evidence_span_key_is_rejected():
    input_data = make_input()
    output = make_output(
        evidence_spans={
            "research_object": "KRAS mutations drive tumor growth",
            "major_method": "We used structure-based design",
            "author_reported_result": "showed an IC50 of 8 nM",
            "bogus_field": "some text",
        }
    )
    with pytest.raises(ValidationError) as exc_info:
        validate_l1_output(input_data, output)
    assert any("unknown key 'bogus_field'" in v for v in exc_info.value.violations)


# ---------------------------------------------------------------------------
# evidence_spans grounding
# ---------------------------------------------------------------------------


def test_evidence_span_not_verbatim_in_abstract_is_rejected():
    input_data = make_input()
    output = make_output(
        evidence_spans={
            "research_object": "this sentence does not appear anywhere in the abstract",
            "major_method": "We used structure-based design",
            "author_reported_result": "showed an IC50 of 8 nM",
        }
    )
    with pytest.raises(ValidationError) as exc_info:
        validate_l1_output(input_data, output)
    assert any("not found verbatim" in v for v in exc_info.value.violations)


# ---------------------------------------------------------------------------
# Pydantic-level: tags count
# ---------------------------------------------------------------------------


def test_fewer_than_three_tags_rejected_at_construction():
    with pytest.raises(Exception):
        make_output(tags=["only", "two"])


def test_more_than_five_tags_rejected_at_construction():
    with pytest.raises(Exception):
        make_output(tags=["a", "b", "c", "d", "e", "f"])
