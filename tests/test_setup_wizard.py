import json
import stat
import subprocess
import sys

import pytest

from wechat_export_send import ROOT, setup_wizard


def _answers(tmp_path, text):
    path = tmp_path / "install.env"
    path.write_text(text, encoding="utf-8")
    return setup_wizard.read_answers(str(path))


def test_answers_file_is_parsed_not_executed(tmp_path):
    answers = _answers(tmp_path, "# 注释\nWES_MODE=real\nWES_EXPORT_INCLUDE='项目群,家人群'\n")
    assert answers == {"WES_MODE": "real", "WES_EXPORT_INCLUDE": "项目群,家人群"}
    with pytest.raises(SystemExit):
        _answers(tmp_path, "rm -rf /\n")


def test_demo_mode_changes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WES_HOME", str(tmp_path / "instances"))
    assert setup_wizard.wizard({"WES_MODE": "demo"}, False) == 0
    assert "wechat-export-send demo" in capsys.readouterr().out
    assert not (tmp_path / "instances").exists()


def test_real_mode_stops_with_fixes_when_docker_is_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WES_HOME", str(tmp_path / "instances"))
    monkeypatch.setattr(setup_wizard, "_docker_ready", lambda: (False, "装 Docker 的命令"))
    assert setup_wizard.wizard({"WES_MODE": "real"}, False) == 1
    out = capsys.readouterr().out
    assert "装 Docker 的命令" in out and "重新运行 ./install.sh" in out
    assert not (tmp_path / "instances").exists()


def test_real_mode_creates_instance_with_include(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WES_HOME", str(tmp_path / "instances"))
    monkeypatch.setenv("WES_CLIENT_DIR", str(tmp_path / "client"))
    monkeypatch.setattr(setup_wizard, "_docker_ready", lambda: (True, ""))
    monkeypatch.setattr(setup_wizard, "_deps_ready", lambda: True)
    monkeypatch.setattr(setup_wizard.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(setup_wizard, "_image_exists", lambda tag: False)
    answers = {"WES_MODE": "real", "WES_INSTANCE": "work", "WES_EXPORT_INCLUDE": "项目群，家人群",
               "WES_BUILD_IMAGES": "no", "WES_CLIENT_DEB": "", "WES_START": "no"}
    assert setup_wizard.wizard(answers, False) == 0
    config = tmp_path / "instances" / "work" / "config.json"
    assert json.loads(config.read_text(encoding="utf-8"))["export"]["include"] == ["项目群", "家人群"]
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    out = capsys.readouterr().out
    assert "./install.sh --images" in out and "client install" in out and "vnc.html" in out


def test_check_only_uses_only_the_standard_library():
    out = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]);"
                          "from wechat_export_send.setup_wizard import check_main; raise SystemExit(check_main())", str(ROOT)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "检查环境" in out.stdout
