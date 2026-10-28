"""Explicit sandbox calendar; configured dates are not an official holiday feed."""
from datetime import date, timedelta
from .storage import Document


def is_business_day(session, day):
    doc = session.get(Document, "banking_calendar")
    settings = doc.data if doc else {}
    if day.isoformat() in settings.get("closed_dates", []):
        return False
    if day.isoformat() in settings.get("open_dates", []):
        return True
    return day.weekday() < 5


def settlement_date(session, clock, business_days, cutoff_hour=15):
    doc = session.get(Document, "banking_calendar")
    return settlement_date_from_settings(clock, business_days, doc.data if doc else {}, cutoff_hour)


def settlement_date_from_settings(clock, business_days, settings=None, cutoff_hour=15):
    settings = settings or {}
    def open_day(day):
        if day.isoformat() in settings.get("closed_dates", []):
            return False
        return day.isoformat() in settings.get("open_dates", []) or day.weekday() < 5
    if not business_days:
        return clock.date()  # Seeded cash product explicitly uses calendar-day T+0.
    day = clock.date()
    effective_today = open_day(day)
    while not open_day(day):
        day += timedelta(days=1)
    remaining = business_days + int(effective_today and clock.hour >= cutoff_hour)
    while remaining:
        day += timedelta(days=1)
        if open_day(day):
            remaining -= 1
    return day


def validate_calendar(value):
    for key in ("closed_dates", "open_dates"):
        for day in value.get(key, []):
            date.fromisoformat(day)
    if set(value.get("closed_dates", [])) & set(value.get("open_dates", [])):
        raise ValueError("A calendar date cannot be both open and closed")
