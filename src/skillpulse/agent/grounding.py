"""Grounding check (SPEC section 9 and success criterion S4).

A number in a draft answer must be provable from the tool result it cites. Two rules make
this stricter than "the number appears somewhere in the JSON":

* **Citations must exist.** A citation that names no real ``result_id`` fails closed: a
  fabricated citation cannot silently fall back to "search every result".
* **Only typed share/rate fields prove a percent.** The evidence keeps the field it came
  from, so a count of ``1`` never proves ``100%``; only a field named ``share``/``rate``/
  ``ratio`` (and only its ratio or its percent form) can.

Structured :class:`Claim` objects (``result_id`` + field path + value + unit) are the
preferred path: each claim is validated against the exact field it names, and a draft is
checked against its validated claims rather than against any number anywhere in a row.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from skillpulse.tools import ToolResult

# 1,234.5 / 12,5 % — keep the raw token for the defect log.
_NUMBER = re.compile(r"\d[\d.,]*%?")
_ROUNDING_TOLERANCE = 0.005
_PERCENT_FIELD_NAMES = frozenset(
    {"share", "rate", "ratio", "percentage", "percent", "pct"}
)


@dataclass(frozen=True)
class Claim:
    """A structured assertion: a value read from a named field of a tool result."""

    result_id: str
    field: str
    value: float
    unit: str = "count"  # "count" | "share" | "percent"


@dataclass(frozen=True)
class ValidatedClaim:
    """A claim whose result id and field path both resolved to the stated value."""

    result_id: str
    field: str
    value: float
    unit: str


@dataclass(frozen=True)
class GroundingReport:
    """The result of checking one draft against its cited tool results."""

    grounded: bool
    unsupported: list[str] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)
    invalid_citations: list[str] = field(default_factory=list)
    invalid_claims: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Evidence:
    """One numeric field with the field name that proves whether it can be a percent."""

    value: float
    allows_percent: bool
    path: str


def _parse_number(token: str) -> tuple[float, bool]:
    """Return ``(value, is_percent)``; tolerate ``1,234`` and ``12,5``."""
    is_percent = token.endswith("%")
    body = token[:-1] if is_percent else token
    if "," in body and "." in body:
        body = body.replace(",", "")
    elif body.count(",") == 1 and len(body.split(",")[1]) != 3:
        body = body.replace(",", ".")
    else:
        body = body.replace(",", "")
    try:
        return float(body), is_percent
    except ValueError:
        return float("nan"), is_percent


def _leaf_name(path: str) -> str:
    return path.rsplit(".", 1)[-1].lower() if path else ""


def _walk(value: Any, path: str, out: list[_Evidence]) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, int | float):
        out.append(
            _Evidence(
                value=float(value),
                allows_percent=_leaf_name(path) in _PERCENT_FIELD_NAMES,
                path=path,
            )
        )
    elif isinstance(value, dict):
        for key, item in value.items():
            _walk(item, f"{path}.{key}" if path else str(key), out)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _walk(item, f"{path}.{index}", out)


def evidence_values(results: list[ToolResult]) -> list[_Evidence]:
    """Every numeric field (with its field name) from the given results."""
    out: list[_Evidence] = []
    for result in results:
        _walk(result.values, "values", out)
        _walk(result.rows, "rows", out)
    return out


def _resolve_field(result: ToolResult, path: str) -> Any:
    node: Any = result.model_dump(mode="json")
    for part in path.split("."):
        if isinstance(node, dict):
            if part not in node:
                return None
            node = node[part]
        elif isinstance(node, list):
            try:
                node = node[int(part)]
            except (IndexError, ValueError):
                return None
        else:
            return None
    return node


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def validate_claims(
    claims: list[Claim],
    results_by_id: dict[str, ToolResult],
    cited_ids: set[str],
) -> tuple[list[ValidatedClaim], list[str]]:
    """Validate claims against the exact field they name; return (valid, invalid paths)."""
    validated: list[ValidatedClaim] = []
    invalid: list[str] = []
    for claim in claims:
        result = results_by_id.get(claim.result_id)
        if result is None or (cited_ids and claim.result_id not in cited_ids):
            invalid.append(f"{claim.result_id}:{claim.field}")
            continue
        resolved = _resolve_field(result, claim.field)
        if not _is_number(resolved):
            invalid.append(f"{claim.result_id}:{claim.field}")
            continue
        if claim.unit == "percent":
            if _leaf_name(claim.field) not in _PERCENT_FIELD_NAMES or abs(
                claim.value / 100 - float(resolved)
            ) > _ROUNDING_TOLERANCE:
                invalid.append(f"{claim.result_id}:{claim.field}")
                continue
        elif abs(claim.value - float(resolved)) > _ROUNDING_TOLERANCE:
            invalid.append(f"{claim.result_id}:{claim.field}")
            continue
        validated.append(
            ValidatedClaim(
                result_id=claim.result_id,
                field=claim.field,
                value=float(resolved),
                unit=claim.unit,
            )
        )
    return validated, invalid


def render_claims(claims: list[ValidatedClaim]) -> str:
    """Render validated claims as plain text; every number comes from a named field."""
    lines = ["Grounded claims from verified tool fields:"]
    for claim in claims:
        if claim.unit == "percent":
            rendered = f"{claim.value:.4g}%"
        elif claim.unit in {"share", "ratio"}:
            rendered = f"{claim.value * 100:.4g}% (share, ratio {claim.value:.6g})"
        else:
            rendered = f"{claim.value:.6g} ({claim.unit})"
        lines.append(f"- {claim.field} = {rendered} [{claim.result_id}]")
    return "\n".join(lines)


def _matches_percent(value: float, evidence: list[_Evidence]) -> bool:
    for item in evidence:
        if item.allows_percent and abs(value / 100 - item.value) <= _ROUNDING_TOLERANCE:
            return True
    return False


def _matches_plain(value: float, evidence: list[_Evidence]) -> bool:
    return any(abs(value - item.value) <= _ROUNDING_TOLERANCE for item in evidence)


def _claim_matches(value: float, is_percent: bool, claims: list[ValidatedClaim]) -> bool:
    for claim in claims:
        if is_percent:
            target = claim.value * 100 if claim.unit in {"share", "percent"} else None
            if target is None:
                continue
            if abs(value - target) <= max(0.5, abs(target) * 1e-6):
                return True
        elif abs(value - claim.value) <= _ROUNDING_TOLERANCE:
            return True
    return False


def check_grounding(
    answer: str,
    results: list[ToolResult],
    citations: list[str] | None = None,
    *,
    claims: list[Claim] | None = None,
    allowed_numbers: list[float] | None = None,
) -> GroundingReport:
    """Check every number in ``answer`` against its cited results (or validated claims).

    Fails closed when a citation names no known result. Percent tokens are only supported
    by share/rate fields; a plain count can never prove a percentage.
    """
    citations = list(citations or [])
    results_by_id = {result.result_id: result for result in results}
    invalid_citations = [c for c in citations if c not in results_by_id]

    tokens = [
        (match.group(0), *_parse_number(match.group(0)))
        for match in _NUMBER.finditer(answer)
    ]
    checked = [token for token, _, _ in tokens]
    allowed = [float(value) for value in allowed_numbers or []]

    if citations:
        cited = [results_by_id[c] for c in citations if c in results_by_id]
        if not cited:
            # Every citation was fabricated: fail closed rather than search everything.
            return GroundingReport(
                grounded=False,
                unsupported=checked,
                checked=checked,
                invalid_citations=invalid_citations,
            )
        cited_ids = {c for c in citations if c in results_by_id}
    else:
        cited = results
        cited_ids = set()

    claim_list = list(claims or [])
    validated, invalid_claims = (
        validate_claims(claim_list, results_by_id, cited_ids) if claim_list else ([], [])
    )

    unsupported: list[str] = []
    for token, value, is_percent in tokens:
        if value != value:  # NaN
            continue
        if any(abs(value - extra) <= _ROUNDING_TOLERANCE for extra in allowed):
            continue
        if claim_list:
            if validated and _claim_matches(value, is_percent, validated):
                continue
            unsupported.append(token)
            continue
        evidence = evidence_values(cited)
        if is_percent:
            if _matches_percent(value, evidence):
                continue
        elif _matches_plain(value, evidence):
            continue
        unsupported.append(token)

    grounded = not unsupported and not invalid_citations and not invalid_claims
    return GroundingReport(
        grounded=grounded,
        unsupported=unsupported,
        checked=checked,
        invalid_citations=invalid_citations,
        invalid_claims=invalid_claims,
    )
