"""サイト独自のレース ID（docs/race-id.md）。"""
from .venues import VENUE_TO_SLUG


def make_race_id(date: str, venue: str, race_no: int) -> str:
    return f"{date}-{VENUE_TO_SLUG[venue]}-{race_no:02d}"
