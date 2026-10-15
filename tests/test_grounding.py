"""Grounding regression tests (S4).

Two bugs motivated these: a fabricated citation must fail closed, and a count of 1 must
never prove 100%. Structured claims are validated against the exact field they name.
"""

from __future__ import annotations

from skillpulse.agent.grounding import Claim, check_grounding, render_claims, validate_claims
from skillpulse.tools import ToolResult


def _result(
    *,
    rows: list[dict[str, object]],
    values: dict[str, object] | None = None,
    result_id: str = "tr-real",
) -> ToolResult:
    return ToolResult(
        result_id=result_id,
        tool="skill_counts",
        args={},
        rows=rows,
        values=values or {},
        row_count=len(rows),
        truncated=False,
        extractor="baseline",
        taxonomy_version="t",
    )


def test_fabricated_citation_fails_closed() -> None:
    real = _result(rows=[{"share": 0.5, "posts_with_skill": 15, "total_posts": 30}])
    report = check_grounding("The share is 50%.", [real], ["tr-invented"])
    assert report.grounded is False
    assert report.invalid_citations == ["tr-invented"]
    assert any(token.startswith("50") for token in report.unsupported)


def test_count_of_one_cannot_prove_a_percent() -> None:
    """share is null (sample < 30); the count 1 must not support "100%"."""
    row = {"posts_with_skill": 1, "total_posts": 1, "share": None}
    report = check_grounding("100% of posts mention it.", [_result(rows=[row])], ["tr-real"])
    assert report.grounded is False
    assert report.unsupported == ["100%"]


def test_legitimate_percent_from_share_field() -> None:
    row = {"posts_with_skill": 31, "total_posts": 100, "share": 0.31}
    report = check_grounding("The share is 31%.", [_result(rows=[row])], ["tr-real"])
    assert report.grounded is True
    assert report.unsupported == []


def test_zero_count_is_supported() -> None:
    row = {"posts_with_skill": 0, "total_posts": 40, "share": 0.0}
    report = check_grounding("0 posts mention it (0%).", [_result(rows=[row])], ["tr-real"])
    assert report.grounded is True


def test_number_from_uncited_result_is_not_evidence() -> None:
    cited = _result(rows=[{"posts_with_skill": 3, "total_posts": 40, "share": 0.075}],
                    result_id="tr-a")
    other = _result(rows=[{"posts_with_skill": 777, "total_posts": 40, "share": 0.9}],
                    result_id="tr-b")
    report = check_grounding("777 posts match.", [cited, other], ["tr-a"])
    assert report.grounded is False
    assert "777" in report.unsupported


def test_structured_claim_validated_and_rendered_from_field() -> None:
    real = _result(rows=[{"share": 0.31, "posts_with_skill": 31, "total_posts": 100}])
    claims = [Claim("tr-real", "rows.0.share", 0.31, "share")]
    report = check_grounding(
        "The share is 31%.", [real], ["tr-real"], claims=claims
    )
    assert report.grounded is True
    validated, invalid = validate_claims(claims, {"tr-real": real}, {"tr-real"})
    assert invalid == []
    assert "31%" in render_claims(validated)


def test_claim_with_wrong_field_path_is_invalid() -> None:
    real = _result(rows=[{"share": 0.31, "posts_with_skill": 31, "total_posts": 100}])
    claims = [Claim("tr-real", "rows.0.does_not_exist", 0.31, "share")]
    report = check_grounding("The share is 31%.", [real], ["tr-real"], claims=claims)
    assert report.grounded is False
    assert report.invalid_claims


def test_claim_cannot_cite_an_uncited_result() -> None:
    real = _result(rows=[{"share": 0.31}], result_id="tr-real")
    other = _result(rows=[{"share": 0.9}], result_id="tr-other")
    claims = [Claim("tr-other", "rows.0.share", 0.9, "share")]
    report = check_grounding(
        "The share is 90%.", [real, other], ["tr-real"], claims=claims
    )
    assert report.grounded is False
    assert report.invalid_claims == ["tr-other:rows.0.share"]
