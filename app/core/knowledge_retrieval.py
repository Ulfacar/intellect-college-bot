"""Increment 6: top-k lexical retrieval over the PUBLISHED managed knowledge base.

Feeds `app/core/pilot_prompt.py` (which formats the result into the system prompt) and
`app/core/pilot_validator.py` (which cross-checks sensitive facts in the reply against
what was actually retrieved). Reuses `faq_kb.list_published_candidates()` — the SAME
published+enabled+non-expired, non-archived set the deterministic FAQ layer uses (see
`app/core/faq_kb.py`); draft/archived/expired entries are never constructed as
candidates in the first place, so this module cannot leak them even by accident.

Deliberately NOT the same algorithm as `app/core/faq_matcher.py` (that one looks for a
single confident deterministic answer; this one ranks the best K candidates as context
for the LLM). No embeddings, no vector DB, no second LLM call — normalized token overlap
(Jaccard-style) blended with `difflib.SequenceMatcher.ratio()`, both stdlib-only, matching
the project's existing "no new dependency" convention (see `faq_matcher.py` docstring).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher

from app.core.faq_kb import get_faq_kb_store
from app.core.faq_matcher import MatchCandidate, normalize_text

TOP_K = 5


@dataclass(frozen=True)
class RetrievedKnowledge:
    """Everything the prompt/validator need about one retrieved candidate."""

    entry_id: int
    category: str
    answer_ru: str
    answer_ky: str | None
    handoff_only: bool
    score: float
    valid_from: datetime | None = None
    valid_until: datetime | None = None


def _texts_for_scoring(candidate: MatchCandidate) -> list[str]:
    texts = [candidate.canonical_question]
    texts.extend(v.text for v in candidate.variants if v.text)
    return texts


def _score_text(query_tokens: set[str], query_norm: str, text: str) -> float:
    norm = normalize_text(text)
    if not norm:
        return 0.0
    tokens = set(norm.split())
    if not tokens or not query_tokens:
        overlap = 0.0
    else:
        overlap = len(query_tokens & tokens) / len(query_tokens | tokens)
    fuzzy = SequenceMatcher(None, query_norm, norm).ratio()
    # Blend: token overlap catches reordered/partial phrasing, fuzzy ratio catches
    # near-identical short questions — average keeps either signal from dominating.
    return (overlap + fuzzy) / 2.0


def score_candidate(query_norm: str, query_tokens: set[str], candidate: MatchCandidate) -> float:
    best = 0.0
    for text in _texts_for_scoring(candidate):
        best = max(best, _score_text(query_tokens, query_norm, text))
    return best


def _to_retrieved(candidate: MatchCandidate, score: float) -> RetrievedKnowledge:
    return RetrievedKnowledge(
        entry_id=candidate.faq_entry_id,
        category=candidate.category,
        answer_ru=candidate.answer_ru,
        answer_ky=candidate.answer_ky,
        handoff_only=candidate.handoff_only,
        score=score,
    )


async def retrieve_knowledge(query: str, *, k: int = TOP_K, min_score: float = 0.25) -> list[RetrievedKnowledge]:
    """Top-k published candidates scored against `query`. Empty/no-signal query or an
    empty knowledge base -> `[]` (the model then must ask a safe clarifying question or
    hand off, never invent — see `app/core/pilot_prompt.py`)."""
    query_norm = normalize_text(query)
    if not query_norm:
        return []
    query_tokens = set(query_norm.split())

    store = get_faq_kb_store()
    candidates = await store.list_published_candidates()
    if not candidates:
        return []

    scored = [(score_candidate(query_norm, query_tokens, c), c) for c in candidates]
    scored = [(s, c) for s, c in scored if s >= min_score]
    scored.sort(key=lambda pair: (-pair[0], -pair[1].priority, pair[1].faq_entry_id))
    return [_to_retrieved(c, s) for s, c in scored[:k]]
