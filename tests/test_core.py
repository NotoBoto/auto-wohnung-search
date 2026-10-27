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
    assert core.REPO == "NotoBoto/auto-wohnung-search" and core.VERSION == "0.2.0"


def test_save_config_atomic(tmp_path, monkeypatch):
    import json
    import os
    import threading
    path = tmp_path / "config.json"
    monkeypatch.setattr(core, "CONFIG_FILE", path)
    core.save_config({"bot_token": "secret"})
    replaced = []
    real = os.replace
    monkeypatch.setattr(core.os, "replace", lambda a, b: (replaced.append((a, b)), real(a, b)))
    try:
        core.save_config({"bot_token": object()})   # запись сорвалась — старый файл цел
    except TypeError:
        pass
    assert json.loads(path.read_text(encoding="utf-8")) == {"bot_token": "secret"}
    assert replaced == [] and [p.name for p in tmp_path.iterdir()] == ["config.json"]

    def writer(n):
        for i in range(30):
            core.save_config({"writer": n, "i": i, "pad": "x" * 2000})
    threads = [threading.Thread(target=writer, args=(n,)) for n in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert json.loads(path.read_text(encoding="utf-8"))["i"] == 29
    assert len(replaced) == 60 and [p.name for p in tmp_path.iterdir()] == ["config.json"]


def test_load_config_backs_up_corrupt_file(tmp_path, monkeypatch, caplog):
    path = tmp_path / "config.json"
    monkeypatch.setattr(core, "CONFIG_FILE", path)
    broken = '{"bot_token": "123:abc", "owner_chat_id": 4242, "profile": {"first_'
    path.write_text(broken, encoding="utf-8")
    cfg = core.load_config()
    backups = list(tmp_path.glob("config.json.bad-*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == broken
    assert core.config_backup == backups[0] and "повреждён" in caplog.text
    assert cfg["owner_chat_id"] == 4242   # владелец бота не сбрасывается: иначе бот привяжется к чужому
    assert cfg["mode"] == "confirm"
    path.write_text("{}", encoding="utf-8")
    core.load_config()
    assert core.config_backup is None
