import json

from wechat_export_send import watchdog
from wechat_export_send.config import Instance

MAIN, LOGIN = (917, 667), (292, 396)


def window(width, height, title="Weixin", viewable=True):
    return {"title": title, "viewable": viewable, "width": width, "height": height}


def test_classify():
    assert watchdog.classify_windows([window(292, 396)], MAIN, LOGIN) == "offline"
    assert watchdog.classify_windows([window(917, 667)], MAIN, LOGIN) == "online"
    assert watchdog.classify_windows([window(915, 667)], MAIN, LOGIN) == "online"  # 窗口漂移不算掉线
    assert watchdog.classify_windows([window(300, 300)], MAIN, LOGIN) == "unavailable"
    assert watchdog.classify_windows([window(917, 667, viewable=False)], MAIN, LOGIN) == "unavailable"
    assert watchdog.classify_windows([window(917, 667, title="其他")], MAIN, LOGIN) == "unavailable"
    assert watchdog.classify_windows([], MAIN, LOGIN) == "unavailable"
    assert watchdog.classify_windows([window(292, 396, title="微信")], MAIN, LOGIN) == "offline"


def make_instance(tmp_path, monkeypatch):
    directory = tmp_path / "home" / "main"
    directory.mkdir(parents=True)
    (directory / "config.json").write_text(json.dumps({"instance": "main", "watchdog": {"bark_url": "http://localhost/x"}}), encoding="utf-8")
    monkeypatch.setenv("WES_HOME", str(tmp_path / "home"))
    return Instance.load("main")


def test_alerts_once_and_recovery(tmp_path, monkeypatch):
    instance = make_instance(tmp_path, monkeypatch)
    sent = []

    def notifier(config, title, body, level):
        sent.append(title)
        return True

    def tick(status):
        return watchdog.tick(instance, observer=lambda _i: status, notifier=notifier)

    assert tick("online")["notified"] is False
    assert tick("offline")["notified"] is True
    assert tick("offline")["notified"] is False
    assert tick("unavailable")["notified"] is False
    assert tick("online")["notified"] is True
    assert tick("offline")["notified"] is True
    assert len(sent) == 3 and "掉线" in sent[0] and "恢复" in sent[1]


def test_failed_push_retries(tmp_path, monkeypatch):
    instance = make_instance(tmp_path, monkeypatch)
    outcome = {"ok": False}
    results = []

    def notifier(config, title, body, level):
        results.append(title)
        return outcome["ok"]

    watchdog.tick(instance, observer=lambda _i: "offline", notifier=notifier)
    outcome["ok"] = True
    assert watchdog.tick(instance, observer=lambda _i: "offline", notifier=notifier)["notified"] is True
    assert len(results) == 2


def test_probe_error_is_unavailable(tmp_path, monkeypatch):
    instance = make_instance(tmp_path, monkeypatch)

    def boom(_instance):
        raise RuntimeError("x")

    assert watchdog.tick(instance, observer=boom, notifier=lambda *a: True)["status"] == "unavailable"
