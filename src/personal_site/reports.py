from __future__ import annotations

import datetime as dt
import logging

from flask import render_template

from .stats_queries import nutrition_stats, sleep_stats, workout_stats
from .tz import today_pacific

logger = logging.getLogger(__name__)


def generate_weekly_report(session) -> str:
    """Generate an HTML weekly report covering the last 7 days."""
    end = today_pacific()
    start = end - dt.timedelta(days=6)

    w = workout_stats(session, start, end)
    s = sleep_stats(session, start, end)
    n = nutrition_stats(session, start, end)

    return render_template(
        "email/weekly_report.html",
        start=start.isoformat(),
        end=end.isoformat(),
        workouts=w,
        sleep=s,
        nutrition=n,
    )


def send_report(html_body: str, to_email: str) -> None:
    """Stub: log the report instead of sending email.

    Wire up SMTP / SendGrid / etc. here when email provider is chosen.
    """
    logger.info(
        "Weekly report generated for %s (%d chars). "
        "Email transport not configured — skipping send.",
        to_email,
        len(html_body),
    )
