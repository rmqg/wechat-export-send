"""把一条微信消息解析成 chat-export/v1 的字段。只解析文本，不访问网络、不执行内容。"""
from __future__ import annotations

import html
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zlib

# XML 里这些字段是下载凭据或文件指纹，不进导出
_SENSITIVE_NAMES = {"fullmd5", "thumbfullmd5", "md5", "aeskey", "encryptkey", "filekey", "ticket", "token"}

BASE_TYPES = {1: "text", 3: "image", 34: "voice", 43: "video", 47: "sticker", 49: "app", 10000: "system", 10002: "revoke"}
LINK_APP_TYPES = {"1", "3", "4", "5", "7", "33", "36", "51", "53", "63"}


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        for wbits in (zlib.MAX_WBITS, zlib.MAX_WBITS | 16, -zlib.MAX_WBITS):
            try:
                return zlib.decompress(value, wbits).decode("utf-8", "replace")
            except zlib.error:
                pass
        return value.decode("utf-8", "replace")
    return str(value)


def split_group_sender(value) -> tuple[str, str]:
    """群消息正文可能以「账号:\\n」开头。"""
    text = _text(value)
    match = re.match(r"^([A-Za-z0-9_@.\-]{1,128}):\n(.*)$", text, re.S)
    return (match.group(1), match.group(2)) if match else ("", text)


def safe_field(tag: str, value) -> str:
    name = tag.lower().replace("-", "").replace("_", "")
    if any(sensitive in name for sensitive in _SENSITIVE_NAMES):
        return ""
    text = (value or "").strip()
    if not text:
        return ""
    if name in {"url", "link"}:
        parsed = urllib.parse.urlsplit(text)
        query_names = {key.lower() for key, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)}
        if any(word in parsed.netloc.lower() for word in ("cdn", "qpic")) or any(
                any(token in key for token in ("aeskey", "ticket", "token", "key")) for key in query_names):
            return ""
    return text


def parse_xml(value) -> ET.Element | None:
    text = _text(value)
    start = text.find("<")
    if start < 0:
        return None
    try:
        return ET.fromstring(text[start:])
    except ET.ParseError:
        return None


def _child_text(node: ET.Element | None, tag: str) -> str:
    if node is None:
        return ""
    child = node.find(tag)
    return safe_field(tag, child.text) if child is not None else ""


def _deep_text(node: ET.Element | None, tag: str) -> str:
    if node is None:
        return ""
    child = node.find(".//" + tag)
    return safe_field(tag, child.text) if child is not None else ""


def _record_root(app: ET.Element) -> ET.Element | None:
    item = app.find(".//recorditem")
    if item is None:
        return None
    raw = item.text or ""
    for candidate in (raw, html.unescape(raw)):
        candidate = candidate.strip()
        if candidate.startswith("<"):
            try:
                return ET.fromstring(candidate)
            except ET.ParseError:
                continue
    return item.find("recordinfo")


_RECORD_TYPES = {"1": "text", "2": "image", "3": "voice", "4": "video", "5": "link", "8": "file", "17": "forward", "19": "forward"}


def _record_time(node: ET.Element) -> int | None:
    raw = (node.findtext("srcMsgCreateTime") or node.findtext("sourcetime") or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    for layout in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return int(time.mktime(time.strptime(raw, layout)))
        except ValueError:
            continue
    return None


def parse_forward(app: ET.Element) -> list[dict]:
    root = _record_root(app)
    if root is None:
        return []
    items = []
    for node in root.iter("dataitem"):
        kind = _RECORD_TYPES.get(str(node.get("datatype") or node.findtext("datatype") or ""), "other")
        entry = {"sender_name": safe_field("sourcename", node.findtext("sourcename")), "type": kind}
        moment = _record_time(node)
        if moment is not None:
            entry["ts"] = moment
        body = safe_field("datadesc", node.findtext("datadesc")) or safe_field("datatitle", node.findtext("datatitle"))
        if body:
            entry["text"] = body
        items.append(entry)
    return items


def _refer_text(refer: ET.Element) -> str:
    raw = (refer.findtext("content") or "").strip()
    if not raw:
        return ""
    # 被引用的内容可能本身是 XML（图片、链接），这时只取标题，不把 XML 当正文
    nested = parse_xml(html.unescape(raw)) if "<" in raw else None
    if nested is not None:
        return _deep_text(nested, "title")
    return raw


def parse_mentions(source_xml) -> list[str]:
    """source 列的 msgsource/atuserlist：逗号分隔的账号，notify@all 表示 @所有人。返回原始账号。"""
    root = parse_xml(source_xml)
    if root is None:
        return []
    raw = root.findtext(".//atuserlist") or ""
    found = []
    for item in re.split(r"[,;，\s]+", raw.strip()):
        if not item:
            continue
        found.append("@all" if item == "notify@all" else item)
    return list(dict.fromkeys(found))


def parse_image_attrs(body) -> dict[str, str]:
    root = parse_xml(body)
    node = root.find(".//img") if root is not None else None
    return dict(node.attrib) if node is not None else {}


def parse_message(base_type: int, subtype: int, body, packed=None, *, sender_known=None) -> dict:
    """返回不含 id、ts、发送者的消息字段。解析不了的 XML 不会把原文当正文写出去。

    sender_known：判断前缀是不是真的账号。有人发「note:\\n正文」时不能把开头当成发送者吃掉。
    """
    raw = body if body is not None else packed
    sender, content = split_group_sender(raw)
    if sender and sender_known is not None and not sender_known(sender):
        sender, content = "", _text(raw)
    label = BASE_TYPES.get(base_type)
    if label is None:
        label = "system" if base_type >= 10000 else "other"
    result: dict = {"group_sender": sender, "raw_type": str(base_type) if not subtype else f"{base_type}:{subtype}"}
    if label == "text":
        result.update(type="text", text=content)
        return result
    if label in {"image", "voice", "video", "sticker"}:
        result["type"] = label
        return result
    if label in {"system", "revoke"}:
        result["type"] = label
        root = parse_xml(content)
        if root is not None:
            replaced = root.findtext(".//replacemsg") or root.findtext(".//content") or ""
            if root.tag == "sysmsg" and root.get("type") == "revokemsg":
                result["type"] = "revoke"
            if replaced.strip():
                result["text"] = re.sub(r"<[^>]+>", "", html.unescape(replaced)).strip()
        else:
            cleaned = re.sub(r"<[^>]+>", "", content).strip()
            if cleaned:
                result["text"] = cleaned
        return result
    if label == "app":
        return _parse_app(content, result)
    result["type"] = "other"
    return result


def _parse_app(content: str, result: dict) -> dict:
    root = parse_xml(content)
    app = root.find(".//appmsg") if root is not None else None
    if app is None:
        result["type"] = "other"
        return result
    app_type = (app.findtext("type") or "").strip()
    title = _child_text(app, "title")
    description = _child_text(app, "des")
    url = _child_text(app, "url")
    result["app_type"] = app_type
    if app_type == "62":  # 拍一拍：没有真正的发送者，是一条系统提示
        result["type"] = "system"
        if title:
            result["text"] = title
        return result
    if app_type == "57":
        result["type"] = "quote"
        if title:
            result["text"] = title
        refer = app.find("refermsg")
        if refer is not None:
            reply = {"sender_name": safe_field("displayname", refer.findtext("displayname")),
                     "text": _refer_text(refer)}
            server_id = (refer.findtext("svrid") or "").strip()
            if server_id and server_id != "0":
                reply["id"] = server_id
            result["reply_to"] = {key: value for key, value in reply.items() if value}
        return result
    if app_type == "19":
        result["type"] = "forward"
        if title:
            result["text"] = title
        result["forward"] = parse_forward(app)
        return result
    if app_type == "6":
        result["type"] = "file"
        if title:
            result["file_name"] = title
        attach = app.find("appattach")
        total = (attach.findtext("totallen") if attach is not None else "") or ""
        if total.strip().isdigit():
            result["file_size"] = int(total.strip())
        return result
    if app_type == "8":
        result["type"] = "sticker"
        return result
    if app_type in LINK_APP_TYPES or url:
        result["type"] = "link"
        link = {"url": url, "title": title, "description": description}
        result["link"] = {key: value for key, value in link.items() if value}
        return result
    result["type"] = "other"
    if title:
        result["text"] = title
    return result
