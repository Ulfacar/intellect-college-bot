"""Increment 5: pure deterministic matcher for the managed multilingual FAQ / knowledge
base (`faq_kb_*` tables, see `app/core/faq_kb.py`).

Deliberately independent of any storage/DB code — no imports from `app.core.faq_kb`,
`app.integrations.crm.db`, or SQLAlchemy. Callers (the store layer, the pipeline
integration in `app/core/telegram_commands.py`, the admin playground) resolve the
current PUBLISHED (or, for playground preview, live-draft) candidate set and pass it in
as a plain list of `MatchCandidate` — this module only does text comparison.

Algorithm (documented, no new dependency, NO embeddings/LLM):
    normalize input
    -> exact match against `canonical_question` (any candidate)
    -> exact match against a `variant.text` (any candidate)
    -> normalized equality/substring (query is/contains/is-contained-by a candidate text)
    -> conservative fuzzy: stdlib `difflib.SequenceMatcher.ratio()`, threshold >= 0.92

At every stage, if more than one DIFFERENT `faq_entry_id` ties for the best result,
the tie is broken by `priority` (higher wins); if `priority` is ALSO tied, the match is
refused (`matched=False`, `reason="ambiguous"`) — this layer NEVER guesses/picks
randomly between two competing facts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

# Conservative fuzzy threshold — documented, stdlib-only (see module docstring).
FUZZY_THRESHOLD = 0.92

# Kyrgyz-specific letters (not used in Russian) — deterministic language signal.
_KY_LETTERS = ("ө", "ү", "ң")


@dataclass(frozen=True)
class VariantText:
    """One matchable text for a candidate — either the canonical question
    (`variant_id=None`) or a structured question-variant row."""

    variant_id: int | None
    text: str
    language: str | None = None


@dataclass(frozen=True)
class MatchCandidate:
    """Everything the matcher needs about one entry's currently-effective content.

    For the real bot path this is resolved from the latest PUBLISHED (or restored)
    `faq_kb_versions` snapshot of a `published`+`enabled`+non-expired entry (see
    `app/core/faq_kb.py::list_published_candidates`). For admin "preview draft" it is
    resolved from the entry's live (possibly unpublished) row instead — the matcher
    itself has no notion of draft/published, it only compares text.
    """

    faq_entry_id: int
    canonical_question: str
    variants: list[VariantText]
    answer_ru: str
    answer_ky: str | None
    category: str
    priority: int
    handoff_only: bool
    faq_version_id: int | None = None


@dataclass(frozen=True)
class MatchingResult:
    matched: bool
    faq_entry_id: int | None = None
    match_type: str | None = None          # canonical | variant | normalized | fuzzy
    score: float | None = None
    matched_variant_id: int | None = None
    language: str = "ru"
    answer: str | None = None
    handoff_only: bool = False
    missing_answer_ky: bool = False
    reason: str | None = None              # set when matched is False (or informational)
    faq_version_id: int | None = None


def normalize_text(text: str) -> str:
    """Lowercase, normalize Russian ё, strip punctuation, collapse whitespace.

    Kyrgyz-specific letters (ө/ү/ң) are Unicode word characters and survive
    `\\w`-based stripping untouched — normalization never damages the language signal.
    """
    text = (text or "").lower().replace("ё", "е")
    text = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def detect_language(text: str, *, stored_language: str | None = None) -> str:
    """Deterministic ru/ky detection — NO LLM.

    1. Kyrgyz-specific letters (ө/ү/ң) anywhere in the text -> "ky".
    2. Otherwise, if the caller supplies a known stored language ("ru"/"ky") -> use it.
    3. Otherwise fall back to "ru".
    """
    lowered = (text or "").lower()
    if any(ch in lowered for ch in _KY_LETTERS):
        return "ky"
    if stored_language in ("ru", "ky"):
        return stored_language
    return "ru"


def _texts_for(candidate: MatchCandidate) -> list[VariantText]:
    """Canonical question (variant_id=None) followed by all structured variants."""
    return [VariantText(None, candidate.canonical_question)] + list(candidate.variants)


def _resolve_answer(candidate: MatchCandidate, language: str) -> tuple[str, bool]:
    """`(answer_text, missing_answer_ky)` — RU query -> answer_ru; KY query ->
    answer_ky if present, else safe RU fallback with `missing_answer_ky=True`.
    NEVER auto-translates."""
    if language == "ky":
        if candidate.answer_ky:
            return candidate.answer_ky, False
        return candidate.answer_ru, True
    return candidate.answer_ru, False


def _pick_winner(
    scored: list[tuple[MatchCandidate, VariantText, float]],
) -> tuple[MatchCandidate, VariantText, float] | None | str:
    """Given (candidate, variant_text, score) tuples that all reached the same
    matching *tier*, resolve to a single winner using priority as the tie-break.

    Returns:
      - the winning tuple, if exactly one entry wins after tie-breaking;
      - `"ambiguous"` if two+ DIFFERENT entries remain tied even on priority;
      - `None` if `scored` was empty.
    """
    if not scored:
        return None
    # Best score per DISTINCT faq_entry_id (an entry can have multiple texts match).
    best_by_entry: dict[int, tuple[MatchCandidate, VariantText, float]] = {}
    for cand, vtext, score in scored:
        current = best_by_entry.get(cand.faq_entry_id)
        if current is None or score > current[2]:
            best_by_entry[cand.faq_entry_id] = (cand, vtext, score)

    top_score = max(t[2] for t in best_by_entry.values())
    top_entries = [t for t in best_by_entry.values() if t[2] == top_score]
    if len(top_entries) == 1:
        return top_entries[0]

    top_priority = max(t[0].priority for t in top_entries)
    top_priority_entries = [t for t in top_entries if t[0].priority == top_priority]
    if len(top_priority_entries) == 1:
        return top_priority_entries[0]
    return "ambiguous"  # still tied after priority -> refuse, never guess


def match(
    text: str, candidates: list[MatchCandidate], *, language: str,
) -> MatchingResult:
    """Run the deterministic funnel described in the module docstring."""
    normalized_input = normalize_text(text)
    if not normalized_input or not candidates:
        return MatchingResult(matched=False, language=language, reason="no_match")

    # Pre-normalize every candidate text once.
    normalized_texts: list[tuple[MatchCandidate, VariantText, str]] = []
    for cand in candidates:
        for vtext in _texts_for(cand):
            norm = normalize_text(vtext.text)
            if norm:
                normalized_texts.append((cand, vtext, norm))

    # Stage 1: exact canonical_question match.
    exact_canonical = [
        (c, v, 1.0) for c, v, n in normalized_texts
        if v.variant_id is None and n == normalized_input
    ]
    winner = _pick_winner(exact_canonical)
    if winner == "ambiguous":
        return MatchingResult(matched=False, language=language, reason="ambiguous")
    if winner is not None:
        return _build_result(winner, "canonical", language)

    # Stage 2: exact variant match.
    exact_variant = [
        (c, v, 1.0) for c, v, n in normalized_texts
        if v.variant_id is not None and n == normalized_input
    ]
    winner = _pick_winner(exact_variant)
    if winner == "ambiguous":
        return MatchingResult(matched=False, language=language, reason="ambiguous")
    if winner is not None:
        return _build_result(winner, "variant", language)

    # Stage 3: normalized equality/substring (either direction).
    substring_hits = [
        (c, v, 1.0) for c, v, n in normalized_texts
        if n in normalized_input or normalized_input in n
    ]
    winner = _pick_winner(substring_hits)
    if winner == "ambiguous":
        return MatchingResult(matched=False, language=language, reason="ambiguous")
    if winner is not None:
        return _build_result(winner, "normalized", language)

    # Stage 4: conservative fuzzy (stdlib difflib, threshold >= FUZZY_THRESHOLD).
    fuzzy_hits = []
    for c, v, n in normalized_texts:
        score = SequenceMatcher(None, normalized_input, n).ratio()
        if score >= FUZZY_THRESHOLD:
            fuzzy_hits.append((c, v, score))
    winner = _pick_winner(fuzzy_hits)
    if winner == "ambiguous":
        return MatchingResult(matched=False, language=language, reason="ambiguous")
    if winner is not None:
        return _build_result(winner, "fuzzy", language)

    return MatchingResult(matched=False, language=language, reason="no_match")


def _build_result(
    winner: tuple[MatchCandidate, VariantText, float], match_type: str, language: str,
) -> MatchingResult:
    candidate, vtext, score = winner
    answer, missing_ky = _resolve_answer(candidate, language)
    return MatchingResult(
        matched=True,
        faq_entry_id=candidate.faq_entry_id,
        match_type=match_type,
        score=score,
        matched_variant_id=vtext.variant_id,
        language=language,
        answer=answer,
        handoff_only=candidate.handoff_only,
        missing_answer_ky=missing_ky,
        reason=None,
        faq_version_id=candidate.faq_version_id,
    )
