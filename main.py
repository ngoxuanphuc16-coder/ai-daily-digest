#!/usr/bin/env python3
"""AI Daily Digest — fetch, summarize with Gemini, and email a daily digest.

Usage:
    python main.py                 # fetch -> summarize -> send
    python main.py --dry-run       # fetch -> summarize -> print + write HTML
    python main.py --test-email    # verify SMTP credentials only
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Sequence  # noqa: F401 - Sequence used in signatures

from src.config import ICT, ConfigError, OUTPUT_DIR, Settings, load_sources
from src.emailer import (
    Digest,
    EmailError,
    build_digest,
    render_html,
    render_text,
    send_alert,
    send_digest,
    send_test_email,
    write_html,
)
from src.fetcher import FetchResult, collect
from src.state import ict_date, load_state, record_alert, record_delivery
from src.summarizer import summarize_articles

LOGGER = logging.getLogger("ai_daily_digest")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2


def _force_utf8_stdio() -> None:
    """Windows consoles default to a legacy codepage; the digest is Vietnamese."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _setup_logging(verbose: bool, quiet: bool) -> None:
    level = logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    # These libraries are chatty at DEBUG and add nothing useful here.
    for noisy in ("urllib3", "google_genai", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-daily-digest",
        description="Collect, summarize and email the day's AI news.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python main.py --dry-run\n"
            "  python main.py --dry-run --no-llm --limit 5\n"
            "  python main.py --test-email\n"
            "  python main.py --hours 48\n"
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and summarize, print to the terminal and write HTML, but send nothing",
    )
    mode.add_argument(
        "--test-email",
        action="store_true",
        help="send a short test email to verify SMTP credentials, then exit",
    )

    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="skip Gemini entirely and use the extractive fallback summarizer",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="cap the number of articles summarized (default: MAX_ARTICLES from .env)",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=None,
        metavar="H",
        help="lookback window in hours (default: LOOKBACK_HOURS, normally 24)",
    )
    parser.add_argument(
        "--sources",
        type=Path,
        default=None,
        metavar="PATH",
        help="alternate sources.yaml",
    )
    parser.add_argument(
        "--env",
        type=Path,
        default=None,
        metavar="PATH",
        help="alternate .env file",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        metavar="PATH",
        help="where to write the rendered HTML (default: output/digest-YYYY-MM-DD.html)",
    )
    parser.add_argument(
        "--once-per-day",
        action="store_true",
        help=(
            "skip sending if a digest was already delivered for today's ICT date. "
            "Lets the workflow fire several morning slots to survive a dropped "
            "cron without ever sending twice"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="send even if today's digest was already delivered (overrides --once-per-day)",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=None,
        metavar="PATH",
        help="alternate delivery-state file (default: state/last-delivery.json)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="warnings and errors only")
    return parser


def _default_output_path(generated_at: datetime) -> Path:
    return OUTPUT_DIR / "digest-{}.html".format(generated_at.astimezone(ICT).strftime("%Y-%m-%d"))


def report_outcome(sent: bool, headline: str, digest: Optional[Digest] = None) -> None:
    """State plainly whether an email went out, on stdout and in the job summary.

    A green check with no email in the inbox is indistinguishable from a green
    check with one, which makes "did it work?" unanswerable without reading
    raw logs. GitHub renders $GITHUB_STEP_SUMMARY on the run page itself.
    """
    marker = "EMAIL SENT" if sent else "NO EMAIL SENT"
    print("\n{}: {}".format(marker, headline))

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    lines = [
        "## {} {}".format("✅" if sent else "⚠️", marker),
        "",
        headline,
        "",
    ]
    if digest is not None:
        lines += [
            "| | |",
            "|---|---|",
            "| Articles | {} |".format(digest.stats.get("total", 0)),
            "| Sources OK | {}/{} |".format(
                digest.stats.get("sources_ok", 0), digest.stats.get("sources_total", 0)
            ),
            "| Gemini summaries | {} |".format(digest.stats.get("llm_summaries", 0)),
            "| Extractive fallbacks | {} |".format(digest.stats.get("fallback_summaries", 0)),
            "",
            "### Sources",
            "",
            "| Source | Status | Articles | Detail |",
            "|---|---|---|---|",
        ]
        for result in digest.fetch_results:
            lines.append(
                "| {} | {} | {} | {} |".format(
                    result.source_name, result.status, result.count, result.detail or ""
                )
            )
    try:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        LOGGER.warning("Could not write the job summary: %s", exc)


def print_digest(digest: Digest) -> None:
    """Human-readable dump for --dry-run."""
    print()
    print("=" * 72)
    print("AI DAILY DIGEST — {}".format(digest.date_label))
    print("=" * 72)
    print(
        "{} bài · {}/{} nguồn OK · {} Gemini / {} trích xuất".format(
            digest.stats.get("total", 0),
            digest.stats.get("sources_ok", 0),
            digest.stats.get("sources_total", 0),
            digest.stats.get("llm_summaries", 0),
            digest.stats.get("fallback_summaries", 0),
        )
    )
    print()

    print("Nguồn:")
    for result in digest.fetch_results:
        marker = {"ok": "  OK   ", "empty": "  ---  ", "error": "  FAIL "}.get(result.status, "  ?    ")
        detail = " — {}".format(result.detail) if result.detail else ""
        print("{}{:<28} {:>2} bài{}".format(marker, result.source_name, result.count, detail))
    print()

    if digest.is_empty:
        print("Không có bài viết mới trong khung thời gian đã chọn.")
        return

    print(render_text(digest))
    print()


def maybe_alert(
    args: argparse.Namespace,
    settings: Settings,
    reason: str,
    fetch_results: Sequence[FetchResult],
) -> None:
    """Email a failure notice when the digest is overdue, not merely late.

    The four morning slots make a lost day unlikely, not impossible. Without
    this, "all four failed" and "everything is fine" look identical from the
    inbox — which is the silence this whole design exists to prevent.

    Rate-limited to one alert per ICT day, and skipped while the digest is only
    hours late, so an ordinary quiet morning does not page anyone.
    """
    if args.dry_run or not args.once_per_day:
        return
    if settings.missing_smtp_vars():
        return

    state = load_state(args.state)
    if not state.is_stale():
        LOGGER.info("Digest not delivered this run, but not yet overdue — no alert.")
        return
    if state.alerted_today():
        LOGGER.info("Alert already sent today; not repeating.")
        return

    elapsed = state.hours_since_delivery()
    lines = [
        "Bản tin AI hằng ngày chưa gửi được.",
        "",
        "Lý do: {}".format(reason),
        (
            "Lần gửi thành công gần nhất: {} ({:.0f} giờ trước).".format(
                state.last_sent_at, elapsed
            )
            if elapsed is not None
            else "Chưa từng gửi thành công lần nào."
        ),
        "",
    ]
    if fetch_results:
        lines.append("Trạng thái từng nguồn:")
        for result in fetch_results:
            lines.append(
                "  - {:<28} {:<6} {} bài  {}".format(
                    result.source_name, result.status, result.count, result.detail or ""
                )
            )
        lines.append("")
    lines += [
        "Hệ thống sẽ tự thử lại ở các mốc còn lại trong sáng nay, và sáng mai.",
        "Email này chỉ gửi một lần mỗi ngày.",
    ]

    try:
        send_alert(settings, "⚠️ AI Daily Digest — chưa gửi được bản tin", lines)
        record_alert(args.state)
    except (ConfigError, EmailError) as exc:
        # If SMTP is what is broken, nothing in this process can reach the user.
        LOGGER.error("Could not send the alert email either: %s", exc)


def run_pipeline(args: argparse.Namespace, settings: Settings) -> Digest:
    sources_config = load_sources(args.sources or settings.sources_path)
    enabled = sources_config.enabled_sources
    LOGGER.info("Fetching from %d source(s)", len(enabled))

    report = collect(
        sources=enabled,
        defaults=sources_config.defaults,
        lookback_hours=args.hours if args.hours is not None else settings.lookback_hours,
        limit=args.limit if args.limit is not None else settings.max_articles,
    )
    LOGGER.info("Collected %d unique article(s)", len(report.articles))

    use_llm = not args.no_llm
    summarized = summarize_articles(
        report.articles,
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
        use_llm=use_llm,
        workers=settings.summary_workers,
        max_retries=settings.summary_max_retries,
        requests_per_minute=settings.gemini_rpm,
    )

    model_label = settings.gemini_model if (use_llm and settings.has_gemini) else ""
    return build_digest(summarized, report.results, model=model_label)


def main(argv: Optional[Sequence[str]] = None) -> int:
    _force_utf8_stdio()
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose, args.quiet)

    try:
        settings = Settings.from_env(args.env)
    except ConfigError as exc:
        LOGGER.error("%s", exc)
        return EXIT_CONFIG

    LOGGER.debug("Settings: %s", settings.redacted())

    # ---- --test-email ----------------------------------------------------
    if args.test_email:
        try:
            send_test_email(settings)
        except (ConfigError, EmailError) as exc:
            LOGGER.error("%s", exc)
            return EXIT_CONFIG if isinstance(exc, ConfigError) else EXIT_ERROR
        print("Test email sent to {}".format(", ".join(settings.receiver_emails)))
        return EXIT_OK

    # ---- fail fast on missing SMTP before spending API calls -------------
    if not args.dry_run:
        try:
            settings.require_smtp()
        except ConfigError as exc:
            LOGGER.error("%s", exc)
            LOGGER.error("Tip: run with --dry-run to preview the digest without sending it.")
            return EXIT_CONFIG

    # ---- already delivered today? ----------------------------------------
    # Checked BEFORE fetching or summarizing: the redundant morning slots that
    # exist to survive a dropped cron must cost nothing — no Gemini quota, no
    # requests to publishers. A no-op run finishes in seconds.
    if args.once_per_day and not args.dry_run and not args.force:
        state = load_state(args.state)
        if state.last_sent_date == ict_date():
            report_outcome(
                False,
                "Today's digest was already delivered at {} ({} article(s)), so this "
                "run is a no-op. Extra morning slots exist to cover a dropped cron; "
                "use --force to send anyway.".format(state.last_sent_at, state.articles),
            )
            return EXIT_OK

    # ---- pipeline --------------------------------------------------------
    try:
        digest = run_pipeline(args, settings)
    except ConfigError as exc:
        LOGGER.error("%s", exc)
        return EXIT_CONFIG
    except Exception as exc:  # noqa: BLE001 - top-level guard for a scheduled job
        LOGGER.exception("Pipeline failed: %s", exc)
        maybe_alert(
            args,
            settings,
            "Không dựng được bản tin: {}: {}".format(type(exc).__name__, exc),
            [],
        )
        return EXIT_ERROR

    # ---- --dry-run -------------------------------------------------------
    if args.dry_run:
        print_digest(digest)
        try:
            html = render_html(digest, settings.template_dir)
            target = write_html(html, args.output or _default_output_path(digest.generated_at))
        except (EmailError, OSError) as exc:
            LOGGER.error("Could not write the HTML preview: %s", exc)
            return EXIT_ERROR
        print("HTML preview written to {}".format(target.resolve()))
        report_outcome(
            False,
            "Ran with --dry-run, so delivery was skipped by request. "
            "Remove the flag (or untick 'dry run' in Run workflow) to send.",
            digest,
        )
        return EXIT_OK

    # ---- send ------------------------------------------------------------
    if digest.is_empty and not settings.send_when_empty:
        maybe_alert(
            args,
            settings,
            "Không thu được bài viết nào trong {}h qua.".format(
                args.hours if args.hours is not None else settings.lookback_hours
            ),
            digest.fetch_results,
        )
        LOGGER.warning("No articles found; skipping delivery (set SEND_WHEN_EMPTY=true to override)")
        report_outcome(
            False,
            "No articles were found in the last {}h, so there was nothing to send. "
            "Set SEND_WHEN_EMPTY=true to receive an empty digest anyway.".format(
                args.hours if args.hours is not None else settings.lookback_hours
            ),
            digest,
        )
        return EXIT_OK

    try:
        html = send_digest(digest, settings, settings.template_dir)
    except (ConfigError, EmailError) as exc:
        LOGGER.error("%s", exc)
        # No alert here on purpose: delivery itself just failed, so an alert
        # over the same channel would fail too. GitHub's own workflow-failure
        # notification is the escalation path for this case.
        report_outcome(False, "Delivery failed: {}".format(exc), digest)
        return EXIT_CONFIG if isinstance(exc, ConfigError) else EXIT_ERROR

    # Record only after a confirmed send, so a failed attempt leaves the day
    # unmarked and the next scheduled slot retries it.
    if args.once_per_day:
        record_delivery(
            articles=len(digest.items),
            run_id=os.environ.get("GITHUB_RUN_ID", ""),
            path=args.state,
        )

    # Keep a copy so the GitHub Actions run can upload it as an artifact.
    try:
        write_html(html, args.output or _default_output_path(digest.generated_at))
    except OSError as exc:
        LOGGER.warning("Digest sent, but the local copy could not be written: %s", exc)

    report_outcome(
        True,
        "Delivered {} article(s) to {}.".format(
            len(digest.items), ", ".join(settings.receiver_emails)
        ),
        digest,
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
