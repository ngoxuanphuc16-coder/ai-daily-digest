"""Delivery state: "have I already sent today's digest?".

GitHub's `schedule` event is best-effort — it drops the first firing of a new
cron and sheds load at contended slots. The workflow therefore fires several
times each morning, and this module is what stops that from turning into
several emails: the first run that succeeds records the date, and later runs
for the same date exit before spending any Gemini quota.

Deliberately fails OPEN. If the state file is missing, corrupt, or
unreadable, we treat the digest as unsent and deliver it. A duplicate email is
a mild annoyance; silence is the failure the user actually complained about.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import ICT, PROJECT_ROOT

LOGGER = logging.getLogger(__name__)

DEFAULT_STATE_PATH = PROJECT_ROOT / "state" / "last-delivery.json"


#: A digest is expected daily, so a gap beyond this means something is wrong
#: rather than merely late. 26h gives the four morning slots room to retry.
STALE_AFTER_HOURS = 26.0


@dataclass
class DeliveryState:
    #: ICT calendar date of the last successful send, "YYYY-MM-DD".
    last_sent_date: str = ""
    last_sent_at: str = ""
    articles: int = 0
    #: GitHub run id, so a state commit can be traced back to its run.
    run_id: str = ""
    #: ICT date of the last "I could not deliver" alert, to cap it at one/day.
    last_alert_date: str = ""

    @property
    def recorded(self) -> bool:
        return bool(self.last_sent_date)

    def hours_since_delivery(self, now: Optional[datetime] = None) -> Optional[float]:
        """Hours since the last successful send, or None if never/unparseable."""
        if not self.last_sent_at:
            return None
        try:
            sent = datetime.fromisoformat(self.last_sent_at)
        except ValueError:
            return None
        if sent.tzinfo is None:
            sent = sent.replace(tzinfo=ICT)
        moment = (now or datetime.now(ICT)).astimezone(ICT)
        return (moment - sent).total_seconds() / 3600.0

    def is_stale(self, now: Optional[datetime] = None) -> bool:
        """True when a digest is overdue — including 'never delivered'."""
        elapsed = self.hours_since_delivery(now)
        if elapsed is None:
            return self.recorded is False
        return elapsed >= STALE_AFTER_HOURS

    def alerted_today(self, now: Optional[datetime] = None) -> bool:
        return self.last_alert_date == ict_date(now)


def ict_date(now: Optional[datetime] = None) -> str:
    """The digest's identity is its ICT calendar date.

    Runs at 23:47 UTC and 00:20 UTC belong to the same Vietnamese morning, so
    keying on the local date — not UTC — is what makes the guard correct.
    """
    moment = now or datetime.now(ICT)
    return moment.astimezone(ICT).strftime("%Y-%m-%d")


def load_state(path: Optional[Path] = None) -> DeliveryState:
    target = Path(path) if path else DEFAULT_STATE_PATH
    if not target.is_file():
        LOGGER.debug("No delivery state at %s", target)
        return DeliveryState()

    try:
        # utf-8-sig, not utf-8: a BOM makes json.load raise, which would fail
        # open and send a duplicate. Windows editors and PowerShell's
        # `Set-Content -Encoding utf8` both emit one. Harmless when absent.
        with target.open("r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Ignoring unreadable delivery state %s: %s", target, exc)
        return DeliveryState()

    if not isinstance(payload, dict):
        LOGGER.warning("Delivery state %s is not an object; ignoring", target)
        return DeliveryState()

    return DeliveryState(
        last_sent_date=str(payload.get("last_sent_date") or ""),
        last_sent_at=str(payload.get("last_sent_at") or ""),
        articles=int(payload.get("articles") or 0),
        run_id=str(payload.get("run_id") or ""),
        last_alert_date=str(payload.get("last_alert_date") or ""),
    )


def save_state(state: DeliveryState, path: Optional[Path] = None) -> Optional[Path]:
    """Persist the state. Never raises — a send already happened."""
    target = Path(path) if path else DEFAULT_STATE_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as handle:
            json.dump(asdict(state), handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    except OSError as exc:
        # Losing the marker means tomorrow's extra slots may send twice. That
        # is strictly better than aborting after the mail is already gone.
        LOGGER.warning("Could not write delivery state %s: %s", target, exc)
        return None
    LOGGER.debug("Recorded delivery for %s in %s", state.last_sent_date, target)
    return target


def already_sent_today(
    path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> bool:
    return load_state(path).last_sent_date == ict_date(now)


def record_delivery(
    articles: int,
    run_id: str = "",
    path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> DeliveryState:
    moment = (now or datetime.now(ICT)).astimezone(ICT)
    previous = load_state(path)
    state = DeliveryState(
        last_sent_date=ict_date(moment),
        last_sent_at=moment.isoformat(timespec="seconds"),
        articles=articles,
        run_id=run_id,
        # Preserved so a delivery does not re-arm today's alert.
        last_alert_date=previous.last_alert_date,
    )
    save_state(state, path)
    return state


def record_alert(
    path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> DeliveryState:
    """Mark that a failure alert went out, capping it at one per ICT day."""
    state = load_state(path)
    state.last_alert_date = ict_date(now)
    save_state(state, path)
    return state
