"""Deterministic alias baseline (SPEC section 8.2).

No LLM, free, and safe to run in CI. Word-boundary alias matching plus two small
language-aware rules that keep the baseline honest on real posts:

* **Negation** — "no Kubernetes", "ohne Docker", "pas de Kafka" must not be extracted.
* **Nice-to-have** — a skill listed as a bonus is extracted with ``required=False``
  rather than as a hard requirement.

Text is normalised with NFKC + casefold, so accented and non-Latin posts match the same
ASCII aliases. The baseline extracts skills and the REMOTE flag only; role, country and
seniority extraction is the LLM's job (SPEC section 8.2), which is why those fields stay
empty here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from skillpulse.extraction.schema import PostExtraction, SkillMatch
from skillpulse.extraction.taxonomy import Taxonomy, normalize_term

NEGATION_CUES = frozenset(
    {
        "no",
        "not",
        "never",
        "without",
        "dont",
        "doesnt",
        "isnt",
        "cant",
        "wont",
        "nicht",
        "kein",
        "keine",
        "keinen",
        "ohne",
        "pas",
        "sin",
    }
)
NICE_TO_HAVE_CUES = (
    "nice to have",
    "nice-to-have",
    "a plus",
    "is a plus",
    "bonus",
    "preferred",
    "preferably",
    "desirable",
    "advantage",
    "ideally",
    "souhaitable",
    "wunschenswert",
    "wünschenswert",
    "von vorteil",
)
HARD_CUES = (
    "required",
    "must",
    "essential",
    "mandatory",
    "requis",
    "exige",
    "erforderlich",
    "zwingend",
)
REMOTE_CUES = (
    "remote",
    "remote-first",
    "fully remote",
    "work from home",
    "wfh",
    "telecommute",
    "télétravail",
    "teletravail",
    "home office",
    "homeoffice",
    "teletrabajo",
    "remoto",
    "リモート",
    "テレワーク",
)
ONSITE_CUES = ("hybrid", "on-site", "onsite", "in office", "vor ort")
# A trailing "not needed" / "pas nécessaire" negates the skill it follows.
NEGATED_TRAILERS = (
    "not needed",
    "not required",
    "not necessary",
    "not essential",
    "nicht erforderlich",
    "pas necessaire",
    "pas nécessaire",
    "kein",
)

# Split clauses on strong separators, plus a full stop only at a sentence end, so
# "Node.js" and "C++" stay in one token while two sentences do not.
_CLAUSE_SPLIT = re.compile(r"[,;\n•|]+|\.(?=\s|$)")
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_NEGATION_WINDOW = 48


def _tokens(value: str) -> list[str]:
    return [token for token in _WORD.findall(normalize_term(value)) if token]


def _is_negated(clause: str, start: int, end: int) -> bool:
    """True when a negation cue sits just before or after the match in the same clause."""
    before = _tokens(clause[max(0, start - _NEGATION_WINDOW) : start])
    if any(token in NEGATION_CUES for token in before[-4:]):
        return True
    tail = normalize_term(clause[end : end + _NEGATION_WINDOW])
    return any(trailer in tail for trailer in NEGATED_TRAILERS)


def _required_in_clause(clause: str) -> bool:
    """A skill is required unless its clause is explicitly a nice-to-have one."""
    lowered = normalize_term(clause)
    nice = any(cue in lowered for cue in NICE_TO_HAVE_CUES)
    hard = any(cue in lowered for cue in HARD_CUES)
    return hard or not nice


def detect_remote(text: str) -> bool | None:
    """Return the REMOTE flag from the post, or ``None`` when it is not stated."""
    lowered = normalize_term(text)
    remote = any(cue in lowered for cue in REMOTE_CUES)
    onsite = any(cue in lowered for cue in ONSITE_CUES)
    if not remote:
        return False if onsite else None
    # "No remote" / "not remote" / "no remote work" is an explicit no.
    remote_tokens = _tokens(lowered)
    for index, token in enumerate(remote_tokens):
        if token not in {"remote", "homeoffice", "telecommute"}:
            continue
        if any(cue in remote_tokens[max(0, index - 3) : index] for cue in NEGATION_CUES):
            return False
    return not onsite


class BaselineExtractor:
    """Word-boundary alias matcher with negation and required-flag rules."""

    def __init__(self, taxonomy: Taxonomy) -> None:
        self.taxonomy = taxonomy
        # Boundaries treat any Unicode letter as a word character, so a short alias like
        # "r" or "ci" cannot match inside "für" or "appréciés".
        pattern = "|".join(
            f"(?<![^\\W\\d_]){re.escape(normalize_term(term))}(?![^\\W\\d_])"
            for term, _ in taxonomy.matching_terms()
        )
        self._pattern = re.compile(pattern) if pattern else None
        self._term_to_id = {
            normalize_term(term): skill_id for term, skill_id in taxonomy.matching_terms()
        }

    def extract(self, text: str) -> PostExtraction:
        """Extract skills and the remote flag from one post's cleaned text."""
        normalized = normalize_term(text)
        if not normalized:
            return self._empty()

        required: dict[str, bool] = {}
        if self._pattern is not None:
            for clause in _CLAUSE_SPLIT.split(normalized):
                if not clause.strip():
                    continue
                clause_required = _required_in_clause(clause)
                for match in self._pattern.finditer(clause):
                    if _is_negated(clause, match.start(), match.end()):
                        continue
                    skill_id = self._term_to_id.get(match.group(0))
                    if skill_id is None:  # pragma: no cover - index mirrors the pattern
                        continue
                    required[skill_id] = required.get(skill_id, False) or clause_required

        skills = [
            SkillMatch(id=skill_id, required=required[skill_id]) for skill_id in sorted(required)
        ]
        return PostExtraction(
            skills=skills,
            remote=detect_remote(normalized),
            extractor="baseline",
            taxonomy_version=self.taxonomy.version,
        )

    def _empty(self) -> PostExtraction:
        return PostExtraction(extractor="baseline", taxonomy_version=self.taxonomy.version)


def extract_skills(taxonomy: Taxonomy, texts: Iterable[str]) -> list[list[str]]:
    """Convenience helper: extract skill ids for several texts."""
    extractor = BaselineExtractor(taxonomy)
    return [extractor.extract(text).skill_ids() for text in texts]
