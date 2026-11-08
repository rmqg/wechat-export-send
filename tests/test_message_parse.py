from wechat_export_send import message_parse as mp, wcdb, synthetic


def test_text_with_group_prefix():
    result = mp.parse_message(1, 0, "abc_1:\n你好")
    assert result["type"] == "text" and result["text"] == "你好" and result["group_sender"] == "abc_1"


def test_prefix_kept_when_not_a_known_account():
    known = {"abc_1"}.__contains__
    assert mp.parse_message(1, 0, "abc_1:\n你好", sender_known=known)["text"] == "你好"
    result = mp.parse_message(1, 0, "note:\n正文", sender_known=known)
    assert result["text"] == "note:\n正文" and result["group_sender"] == ""


def test_media_has_no_placeholder_text():
    for base, name in ((3, "image"), (34, "voice"), (43, "video"), (47, "sticker")):
        result = mp.parse_message(base, 0, "<msg/>")
        assert result["type"] == name and "text" not in result


def test_quote():
    result = mp.parse_message(49, 57, synthetic._quote_xml("回复", 42, "某人", "原文"))
    assert result["type"] == "quote" and result["text"] == "回复"
    assert result["reply_to"] == {"sender_name": "某人", "text": "原文", "id": "42"}


def test_quote_of_xml_content_is_not_dumped():
    xml = "<msg><appmsg><title>t</title><type>57</type><refermsg><svrid>1</svrid><displayname>d</displayname><content>&lt;msg&gt;&lt;appmsg&gt;&lt;title&gt;标题&lt;/title&gt;&lt;/appmsg&gt;&lt;/msg&gt;</content></refermsg></appmsg></msg>"
    assert mp.parse_message(49, 57, xml)["reply_to"]["text"] == "标题"


def test_forward():
    result = mp.parse_message(49, 19, synthetic._forward_xml())
    assert result["type"] == "forward"
    assert [item["text"] for item in result["forward"]] == ["A 区种香草", "B 区种花苗"]
    assert result["forward"][0]["ts"] == 1793100000


def test_link_drops_credential_urls():
    assert mp.parse_message(49, 5, synthetic._link_xml())["link"]["url"] == "https://example.org/garden-guide"
    secret = "<msg><appmsg><title>t</title><url>https://a.example/x?aeskey=1</url><type>5</type></appmsg></msg>"
    assert "url" not in mp.parse_message(49, 5, secret)["link"]
    cdn = "<msg><appmsg><title>t</title><url>https://cdn.example/x</url><type>5</type></appmsg></msg>"
    assert "url" not in mp.parse_message(49, 5, cdn)["link"]


def test_file_does_not_expose_aeskey():
    result = mp.parse_message(49, 6, synthetic._file_xml("a.txt", 12))
    assert result["type"] == "file" and result["file_name"] == "a.txt" and result["file_size"] == 12
    assert "aeskey" not in str(result)


def test_system_and_revoke():
    assert mp.parse_message(10000, 0, "某人 加入了群聊") == {"group_sender": "", "raw_type": "10000", "type": "system", "text": "某人 加入了群聊"}
    xml = '<sysmsg type="revokemsg"><revokemsg><replacemsg>某人 撤回了一条消息</replacemsg></revokemsg></sysmsg>'
    result = mp.parse_message(10002, 0, xml)
    assert result["type"] == "revoke" and result["text"] == "某人 撤回了一条消息"


def test_pat_is_system():
    xml = "<msg><appmsg><title>某人 拍了拍 某人</title><type>62</type></appmsg></msg>"
    result = mp.parse_message(49, 62, xml)
    assert result["type"] == "system" and result["text"] == "某人 拍了拍 某人"


def test_packed_md5_malformed_is_none():
    assert wcdb.packed_resource_md5(None) is None
    assert wcdb.packed_resource_md5(b"") is None
    assert wcdb.packed_resource_md5(b"\xff\xff\xff") is None
    assert wcdb.packed_resource_md5(b"\x1a\x05\x22\x03abc") is None


def test_unparseable_app_is_other_and_keeps_no_xml():
    result = mp.parse_message(49, 0, "<msg><broken")
    assert result["type"] == "other" and "text" not in result


def test_mentions():
    assert mp.parse_mentions("<msgsource><atuserlist>a,notify@all,a</atuserlist></msgsource>") == ["a", "@all"]
    assert mp.parse_mentions("") == []


def test_split_type_and_room_blob():
    assert wcdb.split_msg_type(49 | (57 << 32)) == (49, 57)
    assert wcdb.parse_room_nicknames(synthetic._room_blob({"a": "甲"})) == {"a": "甲"}
    assert wcdb.parse_room_nicknames(b"\xff\xff") == {}
    assert wcdb.parse_room_nicknames(None) == {}


def test_decompress():
    import zlib
    assert wcdb.decompress_content(zlib.compress("你好".encode())) == "你好"
    assert wcdb.decompress_content(None) is None
    assert wcdb.decompress_content("明文") == "明文"
