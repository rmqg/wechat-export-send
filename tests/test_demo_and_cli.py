from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from wechat_export_send import cli, demo, doctor
from wechat_export_send.config import ConfigError, Instance, _merge, DEFAULTS, IdMapper, conversation_kind

ROOT = Path(__file__).resolve().parents[1]


def test_demo_passes(tmp_path):
    result = demo.run(tmp_path / "demo")
    assert result["ok"], result["checks"]
    assert result["first_run"]["new_lines"] == 16


def test_demo_refuses_non_empty_output(tmp_path):
    (tmp_path / "x").write_text("x")
    with pytest.raises(SystemExit):
        demo.run(tmp_path)


def test_demo_without_any_dependency(tmp_path):
    """刚解压、什么都没装时也要能跑：-S 去掉 site-packages，不给依赖路径。"""
    code = ("import sys;sys.path.insert(0,%r);"
            "from wechat_export_send import demo;r=demo.run(__import__('pathlib').Path(%r));"
            "import json;print(json.dumps({'ok':r['ok'],'fast':r['fast_aes']}))") % (str(ROOT), str(tmp_path / "bare"))
    result = subprocess.run([sys.executable, "-S", "-c", code], capture_output=True, text=True, env={"PATH": os.environ["PATH"]}, timeout=300)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"ok": True, "fast": False}


def test_config_rejects_unknown_key():
    with pytest.raises(ConfigError):
        _merge(DEFAULTS, {"nope": 1})
    with pytest.raises(ConfigError):
        _merge(DEFAULTS, {"export": {"nope": 1}})


def test_instance_create_is_private_and_separate(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WES_CLIENT_DIR", str(tmp_path / "client"))
    assert cli.main(["instance", "create", "alpha"]) == 0
    assert cli.main(["instance", "create", "beta"]) == 0
    assert cli.main(["instance", "create", "Bad/Name"]) == 2
    alpha, beta = Instance.load("alpha"), Instance.load("beta")
    assert alpha.container != beta.container and alpha.export_root != beta.export_root
    for sub in (alpha.dir, alpha.dir / "keys", alpha.dir / "gui"):
        assert stat.S_IMODE(sub.stat().st_mode) == 0o700
    env = (alpha.dir / "instance.env").read_text(encoding="utf-8")
    assert "VNC_BIND=127.0.0.1" in env
    ports = [line for line in (alpha.dir / "instance.env").read_text().splitlines() + (beta.dir / "instance.env").read_text().splitlines() if line.startswith("VNC_WEB_PORT")]
    assert len(set(ports)) == 2


def test_db_storage_never_guesses_between_accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("WES_HOME", str(tmp_path))
    directory = tmp_path / "main"
    (directory / "gui" / "xwechat_files" / "one" / "db_storage").mkdir(parents=True)
    (directory / "gui" / "xwechat_files" / "two" / "db_storage").mkdir(parents=True)
    (directory / "config.json").write_text(json.dumps({"instance": "main"}), encoding="utf-8")
    with pytest.raises(ConfigError):
        Instance.load("main").db_storage()
    (directory / "config.json").write_text(json.dumps({"instance": "main", "account_dir": "two"}), encoding="utf-8")
    assert Instance.load("main").db_storage().parent.name == "two"


def test_id_salt_is_stable(tmp_path, monkeypatch):
    monkeypatch.setenv("WES_HOME", str(tmp_path))
    (tmp_path / "main").mkdir()
    (tmp_path / "main" / "config.json").write_text('{"instance":"main"}', encoding="utf-8")
    instance = Instance.load("main")
    assert instance.id_salt() == Instance.load("main").id_salt()
    assert stat.S_IMODE((instance.state / "id-salt").stat().st_mode) == 0o600


def test_id_mapper_and_kind():
    mapper = IdMapper(b"s")
    assert mapper.conversation("1@chatroom", "group").startswith("g_")
    assert mapper.conversation("1@chatroom", "group") == mapper.conversation("1@chatroom", "group")
    assert IdMapper(b"other").conversation("1@chatroom", "group") != mapper.conversation("1@chatroom", "group")
    assert IdMapper(b"s", "raw").conversation("1@chatroom", "group") == "1_chatroom"
    assert conversation_kind("1@chatroom") == "group" and conversation_kind("gh_x") == "channel"
    assert conversation_kind("filehelper") == "other" and conversation_kind("someone") == "private"


def _make_deb(path: Path, binary: bytes, version: str = "9.9.9", extra_data: dict | None = None) -> None:
    import io
    import lzma
    import tarfile

    def tar_xz(files: dict) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for name, data in files.items():
                info = tarfile.TarInfo(name if name.startswith("..") else "./" + name)
                info.size = len(data)
                info.mode = 0o755
                archive.addfile(info, io.BytesIO(data))
        return lzma.compress(buffer.getvalue())

    def member(name: str, data: bytes) -> bytes:
        header = f"{name:<16}{0:<12}{0:<6}{0:<6}{100644:<8}{len(data):<10}`\n".encode()
        return header + data + (b"\n" if len(data) % 2 else b"")

    content = b"!<arch>\n" + member("debian-binary", b"2.0\n")
    content += member("control.tar.xz", tar_xz({"control": f"Package: wechat\nVersion: {version}\n".encode()}))
    content += member("data.tar.xz", tar_xz({"opt/wechat/wechat": binary, **(extra_data or {})}))
    path.write_bytes(content)


def test_client_install_extracts_without_running_scripts(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WES_CLIENT_DIR", str(tmp_path / "client"))
    deb = tmp_path / "w.deb"
    _make_deb(deb, b"#!/bin/sh\necho synthetic\n")
    assert cli.main(["client", "install", str(deb)]) == 0
    current = json.loads((tmp_path / "client" / "current.json").read_text(encoding="utf-8"))
    assert current["version"] == "9.9.9"
    assert (Path(current["root"]) / "opt/wechat/wechat").is_file()
    assert json.loads(capsys.readouterr().out)["tested"] is False


def test_client_install_rejects_path_traversal(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WES_CLIENT_DIR", str(tmp_path / "client"))
    deb = tmp_path / "evil.deb"
    _make_deb(deb, b"x", extra_data={"../../evil": b"pwned"})
    assert cli.main(["client", "install", str(deb)]) == 2
    assert not (tmp_path / "evil").exists()
    assert not list((tmp_path / "client").glob(".staging-*"))


def test_safe_extract_without_filter_support(tmp_path, monkeypatch):
    """模拟 Python 3.11.4 以前：没有 data_filter 时走自己的检查。"""
    import io
    import tarfile
    monkeypatch.delattr(tarfile, "data_filter", raising=False)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo("../evil")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    buffer.seek(0)
    with tarfile.open(fileobj=buffer) as archive, pytest.raises(ConfigError):
        cli._safe_extract(archive, tmp_path / "out")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        link = tarfile.TarInfo("link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)
    buffer.seek(0)
    with tarfile.open(fileobj=buffer) as archive, pytest.raises(ConfigError):
        cli._safe_extract(archive, tmp_path / "out")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo("opt/ok")
        info.size = 2
        archive.addfile(info, io.BytesIO(b"ok"))
    buffer.seek(0)
    (tmp_path / "out").mkdir()
    with tarfile.open(fileobj=buffer) as archive:
        cli._safe_extract(archive, tmp_path / "out")
    assert (tmp_path / "out" / "opt" / "ok").read_bytes() == b"ok"


def test_client_install_rejects_other_files(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WES_CLIENT_DIR", str(tmp_path / "client"))
    bad = tmp_path / "bad.deb"
    bad.write_bytes(b"not a deb")
    assert cli.main(["client", "install", str(bad)]) == 2


def test_doctor_reports_and_never_crashes_without_docker(tmp_path, monkeypatch):
    monkeypatch.setenv("WES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", "/nonexistent")
    items = doctor.run()
    names = {item["check"] for item in items}
    assert {"python", "cpu", "docker"} <= names
    assert not any(item["status"] == doctor.FAIL for item in items)
    assert all(item["fix"] for item in items if item["status"] == doctor.WARN)


def test_doctor_flags_loose_permissions(tmp_path, monkeypatch):
    monkeypatch.setenv("WES_HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/nonexistent")
    directory = tmp_path / "main"
    (directory / "keys").mkdir(parents=True)
    (directory / "config.json").write_text('{"instance":"main"}', encoding="utf-8")
    os.chmod(directory / "keys", 0o755)
    os.chmod(directory, 0o700)
    failed = [item for item in doctor.run("main") if item["status"] == doctor.FAIL]
    assert any("keys" in item["check"] and "chmod 700" in item["fix"] for item in failed)


def test_contract_is_present():
    text = (ROOT / "docs" / "CONTRACT.md").read_text(encoding="utf-8")
    assert "chat-export/v1" in text and "chat-send/v1" in text


def test_export_command_end_to_end(tmp_path, monkeypatch, capsys):
    """真实实例的命令路径：config.json + 密钥文件 + export + conversations + doctor。"""
    from wechat_export_send import synthetic
    monkeypatch.setenv("WES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WES_CLIENT_DIR", str(tmp_path / "client"))
    monkeypatch.setenv("PATH", "/nonexistent")
    assert cli.main(["instance", "create", "main"]) == 0
    directory = tmp_path / "home" / "main"
    synthetic.build_account(directory)
    capsys.readouterr()
    assert cli.main(["export", "--instance", "main"]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["new_lines"] == 16 and stats["errors"] == []
    assert cli.main(["export", "--instance", "main"]) == 0
    assert json.loads(capsys.readouterr().out)["new_lines"] == 0
    assert cli.main(["conversations", "--instance", "main"]) == 0
    out = capsys.readouterr().out
    assert "合成示例群（虚构）" in out and "\tgroup\t14\t" in out
    assert not list((directory / "state").glob("export-*"))  # 解密副本都删掉了
    items = {item["check"]: item for item in doctor.run("main")}
    assert items["[main] 数据库密钥"]["status"] == doctor.OK
    assert items["[main] 导出清单"]["status"] == doctor.OK


def test_export_without_keys_explains(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WES_HOME", str(tmp_path))
    directory = tmp_path / "main"
    (directory / "gui" / "xwechat_files" / "a" / "db_storage").mkdir(parents=True)
    (directory / "keys").mkdir()
    (directory / "keys" / "all_keys.json").write_text('{"contact/contact.db":{"enc_key":"00"}}', encoding="utf-8")
    (directory / "config.json").write_text('{"instance":"main"}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="keys capture"):
        cli.main(["export", "--instance", "main"])


def _attached(tmp_path, monkeypatch, capsys, **options):
    from wechat_export_send import synthetic
    monkeypatch.setenv("WES_HOME", str(tmp_path / "home"))
    existing = tmp_path / "existing"
    synthetic.build_account(existing)
    arguments = ["instance", "attach", "second", "--container", "other-desktop", "--gui", str(existing / "gui"),
                 "--keys", str(existing / "keys" / "all_keys.json")]
    for key, value in options.items():
        arguments += ["--" + key.replace("_", "-"), value]
    assert cli.main(arguments) == 0
    capsys.readouterr()
    return existing, Instance.load("second")


def test_attach_uses_existing_data_read_only_and_refuses_lifecycle(tmp_path, monkeypatch, capsys):
    existing, instance = _attached(tmp_path, monkeypatch, capsys, docker_host="unix:///run/o.sock", network="host", display=":100")
    assert instance.gui == (existing / "gui").resolve()
    assert instance.container == "other-desktop" and instance.worker_network() == "host"
    assert instance.docker() == ["docker", "-H", "unix:///run/o.sock"]
    assert stat.S_IMODE(instance.keys_file.stat().st_mode) == 0o600
    assert instance.keys_file.read_bytes() == (existing / "keys" / "all_keys.json").read_bytes()
    assert json.loads(instance.calibration_file().read_text(encoding="utf-8"))["display"] == ":100"
    assert instance.db_storage().parent.parent.parent == instance.gui
    # 容器不归这里管：不启停
    assert cli.main(["instance", "up", "second"]) == 2
    assert cli.main(["instance", "down", "second"]) == 2
    assert "不启停" in capsys.readouterr().err
    # 重复对接要报错，不覆盖
    assert cli.main(["instance", "attach", "second", "--container", "x", "--gui", str(existing / "gui"), "--keys", str(existing / "keys" / "all_keys.json")]) == 2


def test_attach_export_then_verify_counts_match(tmp_path, monkeypatch, capsys):
    from wechat_export_send import exporter, synthetic, verify
    existing, instance = _attached(tmp_path, monkeypatch, capsys)
    before = {path: path.read_bytes() for path in (existing / "gui").rglob("*") if path.is_file()}
    stats = exporter.export_instance(instance)
    assert stats["new_lines"] == 16
    assert {path: path.read_bytes() for path in (existing / "gui").rglob("*") if path.is_file()} == before
    assert instance.export_root.is_relative_to(instance.dir)
    result = verify.verify_instance(instance)
    assert result["ok"], result
    assert result["db_rows"] == result["exported_unique"] == 16 and result["mismatch_count"] == 0
    assert result["types"]["image"] == 4 and result["media"]["image:ready"] == 1  # 没给图片密钥，V2 的那张如实是 pending
    assert result["media"]["image:pending"] == 3
    # 只有计数：结果里不能出现消息内容和账号
    blob = json.dumps(result, ensure_ascii=False)
    for private in ("周六", synthetic.ALICE, synthetic.GROUP, "合成示例群"):
        assert private not in blob


def test_verify_reports_missing_lines(tmp_path, monkeypatch, capsys):
    from wechat_export_send import exporter, verify
    _, instance = _attached(tmp_path, monkeypatch, capsys)
    exporter.export_instance(instance)
    manifest = json.loads((instance.export_root / "manifest.json").read_text(encoding="utf-8"))
    path = instance.export_root / next(item for item in manifest["conversations"] if item["kind"] == "group")["file"]
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    result = verify.verify_instance(instance)
    assert not result["ok"] and result["mismatch_count"] == 1 and result["mismatches"][0]["db_rows"] == result["mismatches"][0]["exported"] + 1


def test_container_runner_command_is_locked_down(tmp_path, monkeypatch, capsys):
    existing, instance = _attached(tmp_path, monkeypatch, capsys, runner="container", puid="1200", pgid="1201")
    seen = {}

    def fake_call(command):
        seen["command"] = command
        return 0

    monkeypatch.setattr(cli.subprocess, "call", fake_call)
    assert cli.main(["export", "--instance", "second"]) == 0
    joined = " ".join(seen["command"])
    for piece in ("--network none", "--read-only", "--cap-drop ALL", "no-new-privileges:true", "--memory 1024m",
                  "--user 1200:1201", f"src={instance.gui},dst={instance.gui},readonly", "--in-process"):
        assert piece in joined, piece
    assert "SYS_PTRACE" not in joined


def test_export_flushes_large_batches(tmp_path, monkeypatch, capsys):
    from wechat_export_send import exporter, verify
    monkeypatch.setattr(exporter, "FLUSH_LINES", 3)
    _, instance = _attached(tmp_path, monkeypatch, capsys)
    assert exporter.export_instance(instance)["new_lines"] == 16
    assert verify.verify_instance(instance)["ok"]
