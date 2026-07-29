"""Build the digest model, render it with Jinja2, and deliver it over SMTP."""

from __future__ import annotations

import logging
import smtplib
import ssl
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import ICT, Settings
from .fetcher import FetchResult
from .summarizer import SummarizedArticle

LOGGER = logging.getLogger(__name__)

MUST_READ_COUNT = 3
TEMPLATE_NAME = "email_template.html"

#: Weekday names indexed by `datetime.weekday()` (Monday == 0).
_VI_WEEKDAYS = (
    "Thứ Hai",
    "Thứ Ba",
    "Thứ Tư",
    "Thứ Năm",
    "Thứ Sáu",
    "Thứ Bảy",
    "Chủ Nhật",
)

#: Ordering for the grouped sections; anything unlisted sorts last.
_CATEGORY_ORDER = (
    "Frontier Labs",
    "Industry & Platforms",
    "Open Source & Community",
    "Media & Analysis",
    "Research",
)


class EmailError(RuntimeError):
    """Raised when the digest cannot be delivered."""


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------
@dataclass
class Group:
    source_id: str
    source_name: str
    category: str
    items: List[SummarizedArticle] = field(default_factory=list)

    @property
    def top_score(self) -> int:
        return max((item.score for item in self.items), default=0)


@dataclass
class Digest:
    generated_at: datetime
    items: List[SummarizedArticle] = field(default_factory=list)
    must_reads: List[SummarizedArticle] = field(default_factory=list)
    groups: List[Group] = field(default_factory=list)
    fetch_results: List[FetchResult] = field(default_factory=list)
    llm_enabled: bool = False
    model: str = ""
    stats: Dict[str, Any] = field(default_factory=dict)

    @property
    def date_label(self) -> str:
        local = self.generated_at.astimezone(ICT)
        return "{}, {}".format(_VI_WEEKDAYS[local.weekday()], local.strftime("%d/%m/%Y"))

    @property
    def generated_label(self) -> str:
        return self.generated_at.astimezone(ICT).strftime("%H:%M %d/%m/%Y") + " ICT"

    @property
    def subject(self) -> str:
        local = self.generated_at.astimezone(ICT)
        if not self.items:
            return "AI Daily Digest — {} (không có tin mới)".format(local.strftime("%d/%m/%Y"))
        headline = self.must_reads[0].article.title if self.must_reads else ""
        if len(headline) > 60:
            headline = headline[:60].rsplit(" ", 1)[0] + "…"
        return "AI Daily Digest — {} · {} tin · {}".format(
            local.strftime("%d/%m/%Y"), len(self.items), headline
        ).rstrip(" ·")

    @property
    def failed_sources(self) -> List[FetchResult]:
        return [r for r in self.fetch_results if r.status == "error"]

    @property
    def is_empty(self) -> bool:
        return not self.items


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------
def _category_rank(category: str) -> int:
    try:
        return _CATEGORY_ORDER.index(category)
    except ValueError:
        return len(_CATEGORY_ORDER)


def build_digest(
    summarized: Sequence[SummarizedArticle],
    fetch_results: Sequence[FetchResult],
    model: str = "",
    generated_at: Optional[datetime] = None,
) -> Digest:
    """Rank articles, pick the must-reads, and group the rest by publisher."""
    now = generated_at or datetime.now(ICT)

    ranked = sorted(
        summarized,
        key=lambda item: (item.score, item.article.published),
        reverse=True,
    )
    must_reads = ranked[:MUST_READ_COUNT]

    grouped: Dict[str, Group] = {}
    for item in ranked:
        article = item.article
        group = grouped.get(article.source_id)
        if group is None:
            group = Group(article.source_id, article.source_name, article.category)
            grouped[article.source_id] = group
        group.items.append(item)

    groups = sorted(
        grouped.values(),
        key=lambda g: (_category_rank(g.category), -g.top_score, g.source_name.lower()),
    )

    llm_used = sum(1 for item in ranked if not item.summary.fallback)
    stats = {
        "total": len(ranked),
        "sources_ok": sum(1 for r in fetch_results if r.status == "ok"),
        "sources_total": len(fetch_results),
        "sources_failed": sum(1 for r in fetch_results if r.status == "error"),
        "llm_summaries": llm_used,
        "fallback_summaries": len(ranked) - llm_used,
    }

    return Digest(
        generated_at=now,
        items=ranked,
        must_reads=must_reads,
        groups=groups,
        fetch_results=list(fetch_results),
        llm_enabled=llm_used > 0,
        model=model,
        stats=stats,
    )


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
def _environment(template_dir: Path) -> Environment:
    return Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_html(digest: Digest, template_dir: Optional[Path] = None) -> str:
    from .config import TEMPLATE_DIR

    target = Path(template_dir) if template_dir else TEMPLATE_DIR
    if not (target / TEMPLATE_NAME).is_file():
        raise EmailError("Template not found: {}".format(target / TEMPLATE_NAME))
    return _environment(target).get_template(TEMPLATE_NAME).render(digest=digest)


def render_text(digest: Digest) -> str:
    """Plain-text alternative — improves deliverability and helps screen readers."""
    lines: List[str] = [
        "AI DAILY DIGEST — {}".format(digest.date_label),
        "Tạo lúc {} · {} bài từ {}/{} nguồn".format(
            digest.generated_label,
            digest.stats.get("total", 0),
            digest.stats.get("sources_ok", 0),
            digest.stats.get("sources_total", 0),
        ),
        "",
    ]

    if digest.is_empty:
        lines.append("Không có bài viết mới trong 24 giờ qua.")
        return "\n".join(lines)

    lines.append("=" * 60)
    lines.append("TOP {} MUST-READ".format(len(digest.must_reads)))
    lines.append("=" * 60)
    for rank, item in enumerate(digest.must_reads, start=1):
        lines.append("")
        lines.append("{}. [{}] {}".format(rank, item.summary.stars, item.article.title))
        lines.append("   {} · {}".format(item.article.source_name, item.article.published_label))
        if item.summary.tldr != item.article.title:
            lines.append("   {}".format(item.summary.tldr))
        for takeaway in item.summary.key_takeaways:
            lines.append("   - {}".format(takeaway))
        lines.append("   {}".format(item.article.url))

    for group in digest.groups:
        lines.append("")
        lines.append("-" * 60)
        lines.append("{} ({})".format(group.source_name.upper(), group.category))
        lines.append("-" * 60)
        for item in group.items:
            lines.append("")
            lines.append("[{}] {}".format(item.summary.stars, item.article.title))
            if item.summary.tldr != item.article.title:
                lines.append("   {}".format(item.summary.tldr))
            for takeaway in item.summary.key_takeaways:
                lines.append("   - {}".format(takeaway))
            lines.append("   {} {}".format(" ".join(item.summary.tags), item.article.url))

    if digest.failed_sources:
        lines.append("")
        lines.append("Nguồn lỗi: {}".format(", ".join(r.source_name for r in digest.failed_sources)))

    return "\n".join(lines)


# --------------------------------------------------------------------------
# delivery
# --------------------------------------------------------------------------
@contextmanager
def _smtp_connection(settings: Settings) -> Iterator[smtplib.SMTP]:
    """Open an authenticated SMTP session.

    Port 465 means implicit TLS (SMTPS); anything else is treated as
    submission with STARTTLS, which covers 587 and most custom relays.
    """
    context = ssl.create_default_context()
    server: Optional[smtplib.SMTP] = None
    try:
        if settings.smtp_port == 465:
            server = smtplib.SMTP_SSL(settings.smtp_server, settings.smtp_port, timeout=30, context=context)
        else:
            server = smtplib.SMTP(settings.smtp_server, settings.smtp_port, timeout=30)
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
        server.login(settings.sender_email, settings.sender_password)
        yield server
    except smtplib.SMTPAuthenticationError as exc:
        raise EmailError(
            "SMTP authentication failed for {}. For Gmail you must use a 16-character "
            "App Password (not your account password) and have 2-Step Verification on. "
            "Server said: {}".format(settings.sender_email, exc)
        ) from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailError(
            "Could not reach SMTP server {}:{} — {}".format(
                settings.smtp_server, settings.smtp_port, exc
            )
        ) from exc
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:  # noqa: BLE001 - teardown must not mask real errors
                pass


def _build_message(
    subject: str, html_body: str, text_body: str, settings: Settings
) -> MIMEMultipart:
    message = MIMEMultipart("alternative")
    message["Subject"] = Header(subject, "utf-8")
    message["From"] = formataddr((str(Header(settings.sender_name, "utf-8")), settings.sender_email))
    message["To"] = ", ".join(settings.receiver_emails)
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain="ai-daily-digest")
    # Plain text first: clients render the last part they understand.
    message.attach(MIMEText(text_body, "plain", "utf-8"))
    message.attach(MIMEText(html_body, "html", "utf-8"))
    return message


def send_digest(digest: Digest, settings: Settings, template_dir: Optional[Path] = None) -> str:
    """Render and send the digest. Returns the rendered HTML."""
    settings.require_smtp()

    html_body = render_html(digest, template_dir)
    message = _build_message(digest.subject, html_body, render_text(digest), settings)

    with _smtp_connection(settings) as server:
        server.sendmail(settings.sender_email, settings.receiver_emails, message.as_string())

    LOGGER.info(
        "Digest sent to %d recipient(s): %s",
        len(settings.receiver_emails),
        ", ".join(settings.receiver_emails),
    )
    return html_body


def send_test_email(settings: Settings) -> None:
    """Verify SMTP credentials end-to-end without running the pipeline."""
    settings.require_smtp()

    now = datetime.now(ICT).strftime("%H:%M %d/%m/%Y")
    text_body = (
        "AI Daily Digest — test email\n\n"
        "SMTP credentials are working.\n"
        "Server: {}:{}\n"
        "From:   {}\n"
        "Sent:   {} ICT\n".format(
            settings.smtp_server, settings.smtp_port, settings.sender_email, now
        )
    )
    html_body = (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;'
        'max-width:520px;margin:0 auto;padding:24px">'
        '<h2 style="margin:0 0 12px">✅ AI Daily Digest</h2>'
        "<p>SMTP credentials are working — the daily digest can be delivered.</p>"
        '<table style="font-size:14px;border-collapse:collapse">'
        "<tr><td><b>Server</b></td><td>&nbsp;{}:{}</td></tr>"
        "<tr><td><b>From</b></td><td>&nbsp;{}</td></tr>"
        "<tr><td><b>Sent</b></td><td>&nbsp;{} ICT</td></tr>"
        "</table></div>".format(
            settings.smtp_server, settings.smtp_port, settings.sender_email, now
        )
    )

    message = _build_message("AI Daily Digest — test email", html_body, text_body, settings)
    with _smtp_connection(settings) as server:
        server.sendmail(settings.sender_email, settings.receiver_emails, message.as_string())

    LOGGER.info("Test email sent to %s", ", ".join(settings.receiver_emails))


def write_html(html: str, path: Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")
    return target
