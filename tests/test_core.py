import core


def test_load_config_fills_nested_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "CONFIG_FILE", tmp_path / "config.json")
    (tmp_path / "config.json").write_text('{"filters": {"max_warm": 950}, "platforms": {"immoscout": {"daily_cap": 5}}}', encoding="utf-8")
    cfg = core.load_config()
    assert cfg["filters"] == {"city": "Bonn", "max_warm": 950, "min_rooms": 2.0, "min_lease_months": 12}
    assert cfg["platforms"]["immoscout"]["daily_cap"] == 5
    assert cfg["platforms"]["immoscout"]["min_gap_sec"] == 240
    assert cfg["mode"] == "confirm"


def test_identity():
    assert core.REPO == "NotoBoto/auto-wohnung-search" and core.VERSION == "0.1.0"
