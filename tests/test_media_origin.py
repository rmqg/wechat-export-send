import http.client
import threading

import pytest

from wechat_export_send import media, media_origin


@pytest.fixture
def server(tmp_path):
    relative, _ = media.store_content(tmp_path, b"\x89PNG-synthetic", "png")
    config = {"assets_dir": tmp_path, "token": "t0ken", "port": 0, "bind": "127.0.0.1"}
    instance = media_origin.LoopbackThreadingHTTPServer(("127.0.0.1", 0), media_origin.ImageOriginHandler)
    instance.origin_config = config
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance.server_address[1], "/" + relative
    instance.shutdown()
    instance.server_close()


def get(port, path, token="t0ken"):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    connection.request("GET", path, headers=headers)
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response.status, body


def test_serves_with_token_only(server):
    port, path = server
    assert get(port, path) == (200, b"\x89PNG-synthetic")
    assert get(port, path, token="wrong")[0] == 403
    assert get(port, path, token=None)[0] == 403


def test_rejects_other_paths(server):
    port, _ = server
    for path in ("/manifest.json", "/conversations/x.jsonl", "/media/../manifest.json", "/media/ab/" + "0" * 64 + ".png"):
        assert get(port, path)[0] == 404
