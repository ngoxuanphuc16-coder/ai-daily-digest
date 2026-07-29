"""Summarize articles with Google Gemini, with an always-available fallback.

The LLM path asks `gemini-2.5-flash` for structured JSON (Vietnamese prose).
When the key is missing, the SDK is absent, or the API rate-limits us, every
article still gets a summary from the extractive fallback — a digest that
arrives slightly dumber beats a digest that never arrives.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .fetcher import Article

LOGGER = logging.getLogger(__name__)

MAX_SCORE = 5
MIN_SCORE = 1
TAKEAWAY_COUNT = 3
MAX_TAGS = 4

#: Consecutive hard failures before we stop calling the API for this run.
CIRCUIT_BREAKER_THRESHOLD = 3

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
_RETRYABLE_MARKERS = (
    "429",
    "500",
    "502",
    "503",
    "504",
    "rate limit",
    "resource_exhausted",
    "quota",
    "unavailable",
    "deadline",
    "timeout",
    "overloaded",
    "internal error",
)

SYSTEM_INSTRUCTION = (
    "Bạn là biên tập viên công nghệ cao cấp, chuyên theo dõi tin tức AI toàn cầu "
    "cho độc giả kỹ thuật người Việt. Bạn viết ngắn gọn, chính xác, không phóng đại. "
    "Luôn trả lời bằng tiếng Việt tự nhiên (giữ nguyên thuật ngữ tiếng Anh đã phổ biến "
    "như model, agent, benchmark, open-source). Chỉ dựa vào thông tin được cung cấp; "
    "tuyệt đối không bịa số liệu hay chi tiết không có trong nguồn."
)

RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "tldr": {
            "type": "STRING",
            "description": "Tóm tắt 1-2 câu bằng tiếng Việt.",
        },
        "key_takeaways": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": "Đúng 3 gạch đầu dòng chi tiết bằng tiếng Việt.",
        },
        "importance_score": {
            "type": "INTEGER",
            "description": "Mức quan trọng từ 1 (nhỏ) đến 5 (đột phá).",
        },
        "tags": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": "2-4 thẻ dạng #LLM, #Agent, #Vision.",
        },
    },
    "required": ["tldr", "key_takeaways", "importance_score", "tags"],
}

#: Keyword → tag, used by the fallback summarizer and to backfill thin LLM tags.
_TAG_RULES = (
    ("#LLM", ("llm", "language model", "gpt", "claude", "gemini", "llama", "mistral", "token")),
    ("#AgenticAI", ("agent", "agentic", "tool use", "autonomous", "workflow", "mcp")),
    ("#Vision", ("vision", "image", "video", "diffusion", "multimodal", "segmentation")),
    ("#Robotics", ("robot", "embodied", "manipulation", "locomotion", "drone")),
    ("#Hardware", ("chip", "gpu", "tpu", "accelerator", "datacenter", "silicon", "wafer")),
    ("#Safety", ("safety", "alignment", "red team", "jailbreak", "guardrail", "interpretab")),
    ("#OpenSource", ("open source", "open-source", "open weights", "apache", "checkpoint")),
    ("#Research", ("paper", "arxiv", "benchmark", "sota", "state-of-the-art", "we propose")),
    ("#Business", ("funding", "raise", "acquisition", "partnership", "revenue", "ipo", "billion")),
    ("#Policy", ("regulation", "policy", "eu ai act", "lawsuit", "compliance", "governance")),
)

#: Signals that push the heuristic importance score up.
_HIGH_SIGNALS = (
    "launch", "launches", "introducing", "announc", "release", "releases", "unveil",
    "state-of-the-art", "breakthrough", "acquisition", "acquires", "partnership",
    "general availability", "now available", "raises", "open-source", "open source",
)


class SummarizerUnavailable(RuntimeError):
    """Raised when the Gemini client cannot be constructed at all."""


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------
@dataclass
class Summary:
    tldr: str
    key_takeaways: List[str] = field(default_factory=list)
    importance_score: int = 3
    tags: List[str] = field(default_factory=list)
    model: str = ""
    #: True when produced by the extractive fallback rather than Gemini.
    fallback: bool = False
    note: str = ""

    @property
    def stars(self) -> str:
        filled = max(MIN_SCORE, min(MAX_SCORE, self.importance_score))
        return "★" * filled + "☆" * (MAX_SCORE - filled)


@dataclass
class SummarizedArticle:
    article: Article
    summary: Summary

    @property
    def score(self) -> int:
        return self.summary.importance_score


# --------------------------------------------------------------------------
# normalization
# --------------------------------------------------------------------------
def _clamp_score(value: Any) -> int:
    try:
        score = int(round(float(value)))
    except (TypeError, ValueError):
        return 3
    return max(MIN_SCORE, min(MAX_SCORE, score))


def _normalize_tag(raw: Any) -> str:
    text = re.sub(r"[^0-9A-Za-z+/-]", "", str(raw or "").strip().lstrip("#"))
    if not text:
        return ""
    return "#" + text[:24]


def _normalize_tags(raw: Any, fallback_text: str = "") -> List[str]:
    values = raw if isinstance(raw, (list, tuple)) else [raw]
    tags: List[str] = []
    for item in values:
        tag = _normalize_tag(item)
        if tag and tag.lower() not in {t.lower() for t in tags}:
            tags.append(tag)
    if not tags and fallback_text:
        tags = derive_tags(fallback_text)
    return tags[:MAX_TAGS] or ["#AI"]


def _normalize_takeaways(raw: Any, filler: str) -> List[str]:
    """Clean bullets, drop repeats, and pad only as a last resort."""
    values = raw if isinstance(raw, (list, tuple)) else [raw]

    items: List[str] = []
    seen = set()
    for value in values:
        text = re.sub(r"^\s*[-*•]\s*", "", str(value or "").strip())
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(text)

    if filler and len(items) < TAKEAWAY_COUNT and filler.lower() not in seen:
        items.append(filler)
    return items[:TAKEAWAY_COUNT]


def derive_tags(text: str) -> List[str]:
    """Keyword-driven tags — deterministic, no API needed."""
    lowered = (text or "").lower()
    tags = [tag for tag, keywords in _TAG_RULES if any(k in lowered for k in keywords)]
    return tags[:MAX_TAGS] or ["#AI"]


# --------------------------------------------------------------------------
# fallback summarizer
# --------------------------------------------------------------------------
def _sentences(text: str) -> List[str]:
    clean = re.sub(r"\s+", " ", text or "").strip()
    if not clean:
        return []
    return [s.strip() for s in _SENTENCE_SPLIT.split(clean) if len(s.strip()) > 20]


def _heuristic_score(article: Article) -> int:
    haystack = "{} {}".format(article.title, article.summary).lower()
    score = 3
    if any(signal in haystack for signal in _HIGH_SIGNALS):
        score += 1
    if article.category == "Frontier Labs":
        score += 1
    if article.category == "Research" and "sota" not in haystack:
        score -= 1
    if len(article.summary) < 120:
        score -= 1
    return max(MIN_SCORE, min(MAX_SCORE, score))


def heuristic_summary(article: Article, note: str = "") -> Summary:
    """Extractive summary used whenever Gemini is unavailable.

    Output stays in the source language (usually English) — translating without
    a model would mean inventing text, which is worse than an honest excerpt.
    """
    sentences = _sentences(article.summary)
    tldr = sentences[0] if sentences else (article.summary.strip() or article.title.strip())
    if len(tldr) > 280:
        tldr = tldr[:280].rsplit(" ", 1)[0] + "…"

    tags = derive_tags("{} {}".format(article.title, article.summary))

    # Sentences from the source come first; distinct context lines top it up.
    # Repeating one filler three times would look like a rendering bug.
    takeaways = list(sentences[1:1 + TAKEAWAY_COUNT])
    for line in (
        "Nguồn: {} · đăng {}".format(article.source_name, article.published_label),
        "Chủ đề: {}".format(" ".join(tags)),
        "Chưa có tóm tắt AI — đọc bài gốc trên {}.".format(article.domain or article.source_name),
    ):
        if len(takeaways) >= TAKEAWAY_COUNT:
            break
        takeaways.append(line)

    return Summary(
        tldr=tldr,
        key_takeaways=_normalize_takeaways(takeaways, ""),
        importance_score=_heuristic_score(article),
        tags=tags,
        model="fallback:extractive",
        fallback=True,
        note=note or "Tóm tắt trích xuất (không dùng LLM).",
    )


# --------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------
def _build_prompt(article: Article) -> str:
    body = article.summary.strip() or "(Nguồn không cung cấp phần mô tả.)"
    return (
        "Phân tích bài viết AI/công nghệ sau và trả về JSON đúng schema.\n\n"
        "Tiêu đề: {title}\n"
        "Nguồn: {source} ({category})\n"
        "Thời gian đăng: {published}\n"
        "URL: {url}\n"
        "Nội dung trích:\n\"\"\"\n{body}\n\"\"\"\n\n"
        "Yêu cầu:\n"
        "- tldr: 1-2 câu tiếng Việt, nêu đúng điều mới nhất bài này công bố.\n"
        "- key_takeaways: đúng 3 gạch đầu dòng tiếng Việt về tác động kỹ thuật "
        "và tác động kinh doanh. Cụ thể, tránh nói chung chung.\n"
        "- importance_score: 1 = tin nhỏ/thường lệ, 3 = đáng chú ý, "
        "5 = thay đổi cuộc chơi cho toàn ngành.\n"
        "- tags: 2-4 thẻ ngắn bắt đầu bằng '#', ví dụ #LLM, #Agent, #Vision, "
        "#Robotics, #Hardware, #OpenSource.\n"
        "- Nếu phần trích quá ngắn, chỉ suy luận từ tiêu đề; không bịa thêm."
    ).format(
        title=article.title,
        source=article.source_name,
        category=article.category,
        published=article.published_label,
        url=article.url,
        body=body[:4000],
    )


def _is_retryable(error: BaseException) -> bool:
    message = "{} {}".format(type(error).__name__, error).lower()
    return any(marker in message for marker in _RETRYABLE_MARKERS)


def _parse_payload(text: str, article: Article, model: str) -> Summary:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty response from model")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        match = _JSON_OBJECT.search(raw)
        if not match:
            raise ValueError("response was not JSON: {}".format(raw[:160]))
        payload = json.loads(match.group(0))

    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object, got {}".format(type(payload).__name__))

    tldr = str(payload.get("tldr") or "").strip()
    if not tldr:
        raise ValueError("response is missing `tldr`")

    filler = "Xem chi tiết trong bài gốc trên {}.".format(article.source_name)
    return Summary(
        tldr=tldr,
        key_takeaways=_normalize_takeaways(payload.get("key_takeaways"), filler),
        importance_score=_clamp_score(payload.get("importance_score")),
        tags=_normalize_tags(
            payload.get("tags"), "{} {}".format(article.title, article.summary)
        ),
        model=model,
        fallback=False,
    )


class GeminiSummarizer:
    """Thin wrapper over `google-genai` with retries and a circuit breaker."""

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.5-flash",
        max_retries: int = 3,
        retry_base_delay: float = 2.0,
    ) -> None:
        self.api_key = api_key or ""
        self.model = model
        self.max_retries = max(1, max_retries)
        self.retry_base_delay = retry_base_delay

        self._client: Any = None
        self._types: Any = None
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._disabled_reason = ""

    # -- lifecycle ---------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def disabled_reason(self) -> str:
        return self._disabled_reason

    def _ensure_client(self) -> Any:
        """Import and construct lazily so the package stays optional."""
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is not None:
                return self._client
            if not self.api_key:
                raise SummarizerUnavailable("GEMINI_API_KEY is not set")
            try:
                from google import genai
                from google.genai import types
            except ImportError as exc:
                raise SummarizerUnavailable(
                    "google-genai is not installed — run `pip install -r requirements.txt`"
                ) from exc
            try:
                self._client = genai.Client(api_key=self.api_key)
            except Exception as exc:  # noqa: BLE001 - surfaced as a clean fallback
                raise SummarizerUnavailable("could not create Gemini client: {}".format(exc)) from exc
            self._types = types
            return self._client

    def _trip_breaker(self, reason: str) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if (
                self._consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD
                and not self._disabled_reason
            ):
                self._disabled_reason = reason
                LOGGER.warning(
                    "Disabling Gemini for this run after %d consecutive failures: %s",
                    self._consecutive_failures,
                    reason,
                )

    def _reset_breaker(self) -> None:
        with self._lock:
            self._consecutive_failures = 0

    # -- main entry point --------------------------------------------------
    def summarize(self, article: Article) -> Summary:
        """Never raises: a failed call degrades to the extractive summary."""
        if self._disabled_reason:
            return heuristic_summary(article, "Gemini tạm ngưng: {}".format(self._disabled_reason))

        try:
            client = self._ensure_client()
        except SummarizerUnavailable as exc:
            self._disabled_reason = str(exc)
            LOGGER.warning("Gemini unavailable: %s", exc)
            return heuristic_summary(article, "Gemini không khả dụng: {}".format(exc))

        config = self._types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=RESPONSE_SCHEMA,
            temperature=0.3,
            max_output_tokens=1024,
        )

        last_error: Optional[BaseException] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = client.models.generate_content(
                    model=self.model,
                    contents=_build_prompt(article),
                    config=config,
                )
                summary = _parse_payload(getattr(response, "text", ""), article, self.model)
                self._reset_breaker()
                return summary
            except Exception as exc:  # noqa: BLE001 - all failures degrade the same way
                last_error = exc
                retryable = _is_retryable(exc)
                LOGGER.warning(
                    "Gemini attempt %d/%d failed for %r: %s",
                    attempt,
                    self.max_retries,
                    article.title[:60],
                    exc,
                )
                if not retryable or attempt == self.max_retries:
                    break
                time.sleep(self.retry_base_delay * (2 ** (attempt - 1)))

        reason = "{}".format(last_error)[:160]
        self._trip_breaker(reason)
        return heuristic_summary(article, "Gemini lỗi: {}".format(reason))


# --------------------------------------------------------------------------
# batch driver
# --------------------------------------------------------------------------
def summarize_articles(
    articles: Sequence[Article],
    api_key: str = "",
    model: str = "gemini-2.5-flash",
    use_llm: bool = True,
    workers: int = 4,
    max_retries: int = 3,
) -> List[SummarizedArticle]:
    """Summarize every article, preserving input order.

    Falls back article-by-article, so a partial outage still yields a usable
    digest with the working summaries intact.
    """
    if not articles:
        return []

    if not use_llm or not api_key:
        note = (
            "Đã tắt LLM (--no-llm)."
            if not use_llm
            else "Thiếu GEMINI_API_KEY — dùng tóm tắt trích xuất."
        )
        if use_llm:
            LOGGER.warning("GEMINI_API_KEY is not set; using the extractive fallback")
        return [SummarizedArticle(a, heuristic_summary(a, note)) for a in articles]

    summarizer = GeminiSummarizer(api_key=api_key, model=model, max_retries=max_retries)
    worker_count = max(1, min(workers, len(articles)))

    LOGGER.info(
        "Summarizing %d article(s) with %s (%d worker(s))", len(articles), model, worker_count
    )
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        summaries = list(pool.map(summarizer.summarize, articles))

    fallbacks = sum(1 for s in summaries if s.fallback)
    if fallbacks:
        LOGGER.warning("%d/%d article(s) used the fallback summarizer", fallbacks, len(summaries))

    return [SummarizedArticle(a, s) for a, s in zip(articles, summaries)]
