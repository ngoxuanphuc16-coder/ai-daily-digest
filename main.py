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
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Sequence

from src.config import ICT, ConfigError, OUTPUT_DIR, Settings, load_sources
from src.emailer import (
    Digest,
    EmailError,
    build_digest,
    render_html,
    render_text,
    send_digest,
    send_test_email,
    write_html,
)
from src.fetcher import collect
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
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="warnings and errors only")
    return parser


def _default_output_path(generated_at: datetime) -> Path:
    return OUTPUT_DIR / "digest-{}.html".format(generated_at.astimezone(ICT).strftime("%Y-%m-%d"))


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

    # ---- pipeline --------------------------------------------------------
    try:
        digest = run_pipeline(args, settings)
    except ConfigError as exc:
        LOGGER.error("%s", exc)
        return EXIT_CONFIG
    except Exception as exc:  # noqa: BLE001 - top-level guard for a scheduled job
        LOGGER.exception("Pipeline failed: %s", exc)
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
        return EXIT_OK

    # ---- send ------------------------------------------------------------
    if digest.is_empty and not settings.send_when_empty:
        LOGGER.warning("No articles found; skipping delivery (set SEND_WHEN_EMPTY=true to override)")
        return EXIT_OK

    try:
        html = send_digest(digest, settings, settings.template_dir)
    except (ConfigError, EmailError) as exc:
        LOGGER.error("%s", exc)
        return EXIT_CONFIG if isinstance(exc, ConfigError) else EXIT_ERROR

    # Keep a copy so the GitHub Actions run can upload it as an artifact.
    try:
        write_html(html, args.output or _default_output_path(digest.generated_at))
    except OSError as exc:
        LOGGER.warning("Digest sent, but the local copy could not be written: %s", exc)

    print(
        "Digest sent — {} article(s) to {} recipient(s).".format(
            len(digest.items), len(settings.receiver_emails)
        )
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
