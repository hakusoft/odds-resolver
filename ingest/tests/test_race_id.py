import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from ingest.race_id import make_race_id  # noqa: E402
from ingest.venues import venue_from_key  # noqa: E402


def test_make_race_id():
    assert make_race_id("20260726", "帯広ば", 1) == "20260726-ob-01"
    assert make_race_id("20260723", "大井", 11) == "20260723-oi-11"


def test_venue_from_key():
    assert venue_from_key("202607242015060500") == "大井"
    assert venue_from_key("202607233601080300") == "門別"
    assert venue_from_key("202607250304080400") == "帯広ば"  # 場コード 03
    assert venue_from_key("202607249915060500") is None  # 未知コード
