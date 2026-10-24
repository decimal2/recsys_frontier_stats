"""Validate and sort by the first arXiv submission date (UTC), not revision dates."""

import re
from datetime import date


def parse_published_date(value):
    """Return a date for strict YYYY-MM-DD metadata, or None for an unknown date."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("published_date must be YYYY-MM-DD or empty")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Invalid published_date: " + value) from error


def sort_papers_by_date(papers, order="newest"):
    """Return a stable date-sorted list, with unknown dates last in both orders."""
    if order not in {"newest", "oldest"}:
        raise ValueError("Date order must be 'newest' or 'oldest'")
    direction = -1 if order == "newest" else 1

    def key(paper):
        published = parse_published_date(paper.get("published_date"))
        return (published is None, direction * published.toordinal() if published else 0)

    return sorted(papers, key=key)
