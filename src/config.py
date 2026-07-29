"""Configuration: environment variables (.env) plus config/sources.yaml.

Targets Python 3.10+, but deliberately stays 3.9-runtime-safe (no PEP 604
unions at runtime, no `match`) so the pipeline can be smoke-tested on older
interpreters.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from dotenv import load_dotenv

LOGGER = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCES_PATH = PROJECT_ROOT / "config" / "sources.yaml"
TEMPLATE_DIR = PROJECT_ROOT / "templates"
OUTPUT_DIR = PROJECT_ROOT / "output"

#: Indochina Time. A fixed offset rather than zoneinfo — Windows ships without
#: the IANA database and ICT has no DST, so the offset is exact year-round.
ICT = timezone(timedelta(hours=7), "ICT")

_TRUTHY = {"1", "true", "yes", "y", "on"}
_FALSY = {"0", "false", "no", "n", "off"}


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


# --------------------------------------------------------------------------
# env helpers
# --------------------------------------------------------------------------
def _env_str(name: str, default: str = "") -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip()


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        LOGGER.warning("%s=%r is not an integer; using default %d", name, raw, default)
        return default
    if value < minimum:
        LOGGER.warning("%s=%d is below minimum %d; clamping", name, value, minimum)
        return minimum
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    LOGGER.warning("%s=%r is not a boolean; using default %s", name, raw, default)
    return default


def _env_email_list(name: str) -> List[str]:
    """Split a recipient list on commas or semicolons, dropping blanks."""
    raw = _env_str(name)
    if not raw:
        return []
    parts = raw.replace(";", ",").split(",")
    return [p.strip() for p in parts if p.strip()]


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------
@dataclass
class Settings:
    """Runtime settings assembled from the environment."""

    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    smtp_server: str = "smtp.gmail.com"
    smtp_port: int = 465
    sender_email: str = ""
    sender_password: str = ""
    sender_name: str = "AI Daily Digest"
    receiver_emails: List[str] = field(default_factory=list)

    lookback_hours: int = 24
    max_articles: int = 18
    summary_workers: int = 4
    summary_max_retries: int = 3
    #: Gemini free tier allows 5 generate_content calls per minute per model.
    gemini_rpm: int = 5
    send_when_empty: bool = False

    sources_path: Path = DEFAULT_SOURCES_PATH
    template_dir: Path = TEMPLATE_DIR
    output_dir: Path = OUTPUT_DIR

    @classmethod
    def from_env(cls, env_file: Optional[Path] = None) -> "Settings":
        """Load `.env` (if present) and build settings.

        Real environment variables always win over `.env` — that is what makes
        the same code path work in GitHub Actions, where secrets arrive as env
        vars and no `.env` file exists.
        """
        target = Path(env_file) if env_file else PROJECT_ROOT / ".env"
        if target.is_file():
            load_dotenv(target, override=False)
            LOGGER.debug("Loaded environment file %s", target)
        elif env_file is not None:
            raise ConfigError("Environment file not found: {}".format(target))

        return cls(
            gemini_api_key=_env_str("GEMINI_API_KEY"),
            gemini_model=_env_str("GEMINI_MODEL", "gemini-2.5-flash"),
            smtp_server=_env_str("SMTP_SERVER", "smtp.gmail.com"),
            smtp_port=_env_int("SMTP_PORT", 465),
            sender_email=_env_str("SENDER_EMAIL"),
            sender_password=os.getenv("SENDER_PASSWORD", ""),
            sender_name=_env_str("SENDER_NAME", "AI Daily Digest"),
            receiver_emails=_env_email_list("RECEIVER_EMAIL"),
            lookback_hours=_env_int("LOOKBACK_HOURS", 24),
            max_articles=_env_int("MAX_ARTICLES", 18),
            summary_workers=_env_int("SUMMARY_WORKERS", 4),
            summary_max_retries=_env_int("SUMMARY_MAX_RETRIES", 3),
            gemini_rpm=_env_int("GEMINI_RPM", 5),
            send_when_empty=_env_bool("SEND_WHEN_EMPTY", False),
        )

    # -- validation --------------------------------------------------------
    @property
    def has_gemini(self) -> bool:
        return bool(self.gemini_api_key)

    def missing_smtp_vars(self) -> List[str]:
        missing = []
        if not self.smtp_server:
            missing.append("SMTP_SERVER")
        if not self.sender_email:
            missing.append("SENDER_EMAIL")
        if not self.sender_password:
            missing.append("SENDER_PASSWORD")
        if not self.receiver_emails:
            missing.append("RECEIVER_EMAIL")
        return missing

    def require_smtp(self) -> None:
        """Raise a message that names exactly what to fix."""
        missing = self.missing_smtp_vars()
        if missing:
            raise ConfigError(
                "Missing SMTP configuration: {}. Copy .env.example to .env and "
                "fill these in, or export them as environment variables.".format(
                    ", ".join(missing)
                )
            )

    def redacted(self) -> Dict[str, Any]:
        """Loggable view of the settings — never exposes secrets."""
        return {
            "gemini_model": self.gemini_model,
            "gemini_api_key": "set" if self.gemini_api_key else "missing",
            "smtp_server": self.smtp_server,
            "smtp_port": self.smtp_port,
            "sender_email": self.sender_email or "missing",
            "sender_password": "set" if self.sender_password else "missing",
            "receivers": len(self.receiver_emails),
            "lookback_hours": self.lookback_hours,
            "max_articles": self.max_articles,
        }


# --------------------------------------------------------------------------
# sources.yaml
# --------------------------------------------------------------------------
@dataclass
class FetchDefaults:
    lookback_hours: int = 24
    max_articles_per_source: int = 8
    request_timeout: int = 20
    user_agent: str = "ai-daily-digest/1.0"


@dataclass
class Source:
    id: str
    name: str
    url: str
    category: str = "Other"
    type: str = "rss"
    homepage: str = ""
    enabled: bool = True
    max_articles: Optional[int] = None
    fallback: Optional[Dict[str, Any]] = None

    # HTML-scraper selectors (only used when type == "html")
    item_selector: str = ""
    title_selector: str = ""
    link_selector: str = "a"
    summary_selector: str = ""
    date_selector: str = ""


@dataclass
class SourcesConfig:
    defaults: FetchDefaults
    sources: List[Source]

    @property
    def enabled_sources(self) -> List[Source]:
        return [s for s in self.sources if s.enabled]


def _source_from_mapping(raw: Dict[str, Any], index: int) -> Source:
    if not isinstance(raw, dict):
        raise ConfigError("sources[{}] must be a mapping, got {}".format(index, type(raw).__name__))

    url = str(raw.get("url", "")).strip()
    if not url:
        raise ConfigError("sources[{}] is missing a `url`".format(index))

    source_id = str(raw.get("id") or "source-{}".format(index)).strip()
    fallback = raw.get("fallback")
    if fallback is not None and not isinstance(fallback, dict):
        LOGGER.warning("Source %s has a non-mapping `fallback`; ignoring it", source_id)
        fallback = None

    return Source(
        id=source_id,
        name=str(raw.get("name") or source_id).strip(),
        url=url,
        category=str(raw.get("category") or "Other").strip(),
        type=str(raw.get("type") or "rss").strip().lower(),
        homepage=str(raw.get("homepage") or "").strip(),
        enabled=bool(raw.get("enabled", True)),
        max_articles=raw.get("max_articles_per_source"),
        fallback=fallback,
        item_selector=str(raw.get("item_selector") or "").strip(),
        title_selector=str(raw.get("title_selector") or "").strip(),
        link_selector=str(raw.get("link_selector") or "a").strip(),
        summary_selector=str(raw.get("summary_selector") or "").strip(),
        date_selector=str(raw.get("date_selector") or "").strip(),
    )


def load_sources(path: Optional[Path] = None) -> SourcesConfig:
    """Parse `config/sources.yaml` into typed objects."""
    target = Path(path) if path else DEFAULT_SOURCES_PATH
    if not target.is_file():
        raise ConfigError("Sources file not found: {}".format(target))

    try:
        with target.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        raise ConfigError("Could not parse {}: {}".format(target, exc)) from exc

    if not isinstance(payload, dict):
        raise ConfigError("{} must contain a top-level mapping".format(target))

    raw_defaults = payload.get("defaults") or {}
    defaults = FetchDefaults(
        lookback_hours=int(raw_defaults.get("lookback_hours", 24)),
        max_articles_per_source=int(raw_defaults.get("max_articles_per_source", 8)),
        request_timeout=int(raw_defaults.get("request_timeout", 20)),
        user_agent=str(raw_defaults.get("user_agent") or "ai-daily-digest/1.0"),
    )

    raw_sources = payload.get("sources") or []
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ConfigError("{} defines no sources".format(target))

    sources: List[Source] = []
    seen_ids = set()
    for index, raw in enumerate(raw_sources):
        source = _source_from_mapping(raw, index)
        if source.id in seen_ids:
            raise ConfigError("Duplicate source id {!r} in {}".format(source.id, target))
        seen_ids.add(source.id)
        sources.append(source)

    LOGGER.debug("Loaded %d sources (%d enabled)", len(sources), sum(s.enabled for s in sources))
    return SourcesConfig(defaults=defaults, sources=sources)
