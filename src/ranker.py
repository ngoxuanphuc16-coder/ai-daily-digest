"""Hybrid relevance ranker — BM25-style keyword scoring plus structural signals.

Sorts articles by relevance to AI model releases BEFORE the global cap is
applied, so the Gemini quota is spent on the articles that matter most.

The "hybrid" comes from combining two retrieval signals:
  1. Sparse (BM25-inspired): term frequency against a curated vocabulary of
     model-release keywords, weighted by specificity.
  2. Structural/authority: source reputation for model news, title-pattern
     detection for announcements, and recency.

No embedding model or external API is needed — this runs entirely on CPU with
zero network calls, so it never competes with the Gemini quota.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import List, Sequence, Tuple

from .fetcher import Article

# --------------------------------------------------------------------------
# BM25 vocabulary — (term, weight)
# Higher weight = more specific signal that the article is about a model release.
# --------------------------------------------------------------------------
_MODEL_NAMES: List[Tuple[str, float]] = [
    # OpenAI
    ("gpt-4", 3.0), ("gpt-5", 3.0), ("gpt-4o", 3.0), ("gpt-4.1", 3.0),
    ("o1", 2.5), ("o3", 2.5), ("o4-mini", 3.0),
    ("chatgpt", 2.0), ("dall-e", 2.5), ("sora", 2.5), ("codex", 2.5),
    # Anthropic
    ("claude", 3.0), ("opus", 2.5), ("sonnet", 2.5), ("haiku", 2.5),
    ("claude 4", 3.5), ("claude 5", 3.5), ("claude-opus", 3.0),
    ("claude-sonnet", 3.0), ("claude-haiku", 3.0),
    # Google
    ("gemini", 3.0), ("gemini 2", 3.0), ("gemini 3", 3.0),
    ("gemini pro", 2.5), ("gemini ultra", 2.5), ("gemini flash", 2.5),
    ("gemma", 2.5), ("bard", 2.0), ("deepmind", 2.0),
    # Meta
    ("llama", 2.5), ("llama 4", 3.0), ("llama 3", 2.5),
    # Other major models
    ("mistral", 2.5), ("mixtral", 2.5), ("phi-4", 2.5), ("phi-5", 2.5),
    ("grok", 2.5), ("command r", 2.5), ("stable diffusion", 2.0),
    ("midjourney", 2.0), ("flux", 2.0),
]

_RELEASE_TERMS: List[Tuple[str, float]] = [
    # Strong launch signals
    ("introduces", 2.0), ("introducing", 2.0), ("announced", 1.8),
    ("announcing", 1.8), ("announcement", 1.8), ("launches", 2.0),
    ("launched", 2.0), ("launching", 2.0), ("release", 1.8),
    ("released", 1.8), ("releasing", 1.8), ("unveils", 2.0),
    ("unveiling", 2.0), ("debut", 2.0),
    ("now available", 2.0), ("generally available", 2.0),
    ("general availability", 2.0), ("rolling out", 1.5),
    ("public preview", 1.5), ("public beta", 1.5),
    # Model-specific terms
    ("new model", 2.5), ("new ai model", 3.0), ("next-generation", 2.0),
    ("foundation model", 2.0), ("language model", 1.8),
    ("multimodal model", 2.0), ("reasoning model", 2.0),
    ("frontier model", 2.0),
    ("benchmark", 1.5), ("state-of-the-art", 1.8), ("sota", 1.5),
    ("beats", 1.5), ("surpasses", 1.5), ("outperforms", 1.5),
    # Capability terms
    ("context window", 1.5), ("token", 1.0), ("parameter", 1.2),
    ("fine-tuning", 1.2), ("fine-tune", 1.2),
    ("api", 1.0), ("sdk", 1.0), ("pricing", 1.2),
    # AI-general (lower weight — too broad to be strong signals)
    ("artificial intelligence", 0.5), ("machine learning", 0.5),
    ("deep learning", 0.5), ("neural network", 0.5),
    ("large language model", 1.5), ("llm", 1.2),
    ("agent", 0.8), ("agentic", 1.0),
]

# Sources known for first-party model announcements.
_AUTHORITY_SCORES = {
    "openai": 1.0,
    "anthropic": 1.0,
    "deepmind": 0.9,
    "google-ai-blog": 0.9,
    "meta-ai": 0.8,
    "microsoft-ai": 0.7,
    "the-verge-ai": 0.5,
    "techcrunch-ai": 0.5,
    "huggingface": 0.6,
    "mit-tech-review": 0.4,
    "arxiv-cs-ai": 0.3,
}

# Regex patterns that strongly indicate a model announcement headline.
_ANNOUNCEMENT_PATTERNS = [
    re.compile(
        r"\b(?:introduces?|introducing|launches?|launching|unveils?|announcing|announced)\b"
        r".*\b(?:model|ai|gpt|claude|gemini|llama|api)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:gpt|claude|gemini|llama|opus|sonnet|haiku|o[134]|phi|mistral|grok|gemma)"
        r"[\s-]*\d",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:new|next[- ]gen(?:eration)?|latest)\s+(?:ai\s+)?model\b",
        re.IGNORECASE,
    ),
]

# Company names in the user's request — articles mentioning these get a boost.
_TARGET_COMPANIES = re.compile(
    r"\b(?:openai|open\s+ai|anthropic|google|deepmind)\b", re.IGNORECASE
)


@dataclass
class ScoredArticle:
    article: Article
    relevance: float
    bm25_score: float
    authority_score: float
    pattern_score: float
    recency_score: float


# --------------------------------------------------------------------------
# BM25-inspired scorer
# --------------------------------------------------------------------------
def _bm25_score(text: str) -> float:
    """Weighted term-frequency score against the model-release vocabulary.

    Not a true BM25 (no IDF across a corpus — we score each article in
    isolation), but the TF saturation curve `tf / (tf + k1)` prevents a
    single repeated term from dominating.
    """
    lowered = text.lower()
    k1 = 1.2
    total = 0.0

    for term, weight in _MODEL_NAMES + _RELEASE_TERMS:
        tf = lowered.count(term.lower())
        if tf > 0:
            saturated = tf / (tf + k1)
            total += weight * saturated

    return total


def _authority_score(source_id: str) -> float:
    return _AUTHORITY_SCORES.get(source_id, 0.3)


def _pattern_score(title: str, summary: str) -> float:
    text = "{} {}".format(title, summary)
    score = 0.0
    for pattern in _ANNOUNCEMENT_PATTERNS:
        if pattern.search(text):
            score += 1.0
    if _TARGET_COMPANIES.search(text):
        score += 0.5
    return min(score, 3.0)


def _recency_score(article: Article, newest_ts: float, oldest_ts: float) -> float:
    """Linear recency: 1.0 for the newest article, 0.0 for the oldest."""
    span = newest_ts - oldest_ts
    if span <= 0:
        return 1.0
    ts = article.published.timestamp()
    return max(0.0, min(1.0, (ts - oldest_ts) / span))


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------
def rank_articles(
    articles: Sequence[Article],
    *,
    bm25_weight: float = 0.40,
    authority_weight: float = 0.20,
    pattern_weight: float = 0.25,
    recency_weight: float = 0.15,
) -> List[ScoredArticle]:
    """Score and sort articles by relevance to AI model releases.

    Returns all articles (nothing is dropped — that is the caller's job via
    the global cap), sorted most-relevant-first.

    Weight defaults sum to 1.0 but do not need to — the ranking is ordinal.
    """
    if not articles:
        return []

    timestamps = [a.published.timestamp() for a in articles]
    newest = max(timestamps)
    oldest = min(timestamps)

    scored: List[ScoredArticle] = []
    max_bm25 = 0.0
    max_pattern = 0.0

    raw: List[tuple] = []
    for article in articles:
        text = "{} {} {}".format(article.title, article.summary, article.category)
        bm25 = _bm25_score(text)
        authority = _authority_score(article.source_id)
        pattern = _pattern_score(article.title, article.summary)
        recency = _recency_score(article, newest, oldest)
        max_bm25 = max(max_bm25, bm25)
        max_pattern = max(max_pattern, pattern)
        raw.append((article, bm25, authority, pattern, recency))

    for article, bm25, authority, pattern, recency in raw:
        norm_bm25 = (bm25 / max_bm25) if max_bm25 > 0 else 0.0
        norm_pattern = (pattern / max_pattern) if max_pattern > 0 else 0.0

        relevance = (
            bm25_weight * norm_bm25
            + authority_weight * authority
            + pattern_weight * norm_pattern
            + recency_weight * recency
        )

        scored.append(
            ScoredArticle(
                article=article,
                relevance=relevance,
                bm25_score=norm_bm25,
                authority_score=authority,
                pattern_score=norm_pattern,
                recency_score=recency,
            )
        )

    scored.sort(key=lambda s: s.relevance, reverse=True)
    return scored
