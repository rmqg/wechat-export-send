import hashlib

from wechat_export_send import media, synthetic


def test_v2_and_xor_roundtrip(tmp_path):
    picture = synthetic._png(40, 40)
    v2 = tmp_path / "a.dat"
    v2.write_bytes(synthetic._v2_dat(picture, "0123456789abcdef", 0x37))
    assert media.decrypt_dat(v2, "0123456789abcdef", 0x37) == (picture, "png")
    assert media.decrypt_dat(v2, "wrongwrongwrong0", 0x37) == (None, None)
    xor = tmp_path / "b.dat"
    xor.write_bytes(synthetic._xor_dat(picture, 0x5A))
    assert media.decrypt_dat(xor) == (picture, "png")


def test_truncated_image_rejected(tmp_path):
    picture = synthetic._png(40, 40)
    broken = tmp_path / "c.dat"
    broken.write_bytes(synthetic._xor_dat(picture[:len(picture) // 2], 0x5A))
    assert media.decrypt_dat(broken) == (None, None)


def test_quality_and_thumbnail_rule(tmp_path):
    picture = synthetic._png(40, 40)
    md5 = hashlib.md5(picture).hexdigest()
    full = tmp_path / f"{md5}.dat"
    thumb = tmp_path / f"{md5}_t.dat"
    full.write_bytes(synthetic._xor_dat(picture, 1))
    thumb.write_bytes(synthetic._xor_dat(synthetic._png(8, 8), 1))
    attributes = {"md5": md5, "length": str(len(picture))}
    assert media.best_cached_image([full, thumb], attributes, None, None, allow_thumbnail=False)["quality"] == "original"
    assert media.best_cached_image([thumb], attributes, None, None, allow_thumbnail=False) is None
    found = media.best_cached_image([thumb], attributes, None, None, allow_thumbnail=True)
    assert found["is_thumbnail"] is True and found["quality"] == "thumbnail"
    other = {"md5": "0" * 32, "length": "1"}
    assert media.best_cached_image([full], other, None, None, allow_thumbnail=False)["quality"] == "client_full"


def test_store_is_content_addressed(tmp_path):
    relative, digest = media.store_content(tmp_path, b"abc", "png")
    assert relative == f"media/{digest[:2]}/{digest}.png" and (tmp_path / relative).read_bytes() == b"abc"
    assert media.store_content(tmp_path, b"abc", "png") == (relative, digest)
    assert (tmp_path / relative).stat().st_mode & 0o077 == 0


def test_index_prefers_conversation_directory(tmp_path):
    base = tmp_path / "msg" / "attach" / hashlib.md5(b"room").hexdigest() / "2026-01" / "Img"
    base.mkdir(parents=True)
    (base / "ABC_t.dat").write_bytes(b"x")
    (base / "abc.dat").write_bytes(b"y")
    assert sorted(path.name for path in media.index_attach_dir(tmp_path, "room")["abc"]) == ["ABC_t.dat", "abc.dat"]
