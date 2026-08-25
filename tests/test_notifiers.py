import json
import urllib.parse

from paper_collector.notifiers import ServerChanNotifier


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return json.dumps({"code": 0, "message": "", "data": {"errno": 0, "pushid": "1"}}).encode()


def test_serverchan_posts_markdown(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = urllib.parse.parse_qs(request.data.decode())
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    ServerChanNotifier("SCT123test").send("今日论文", "[下载](https://example.test/a.pdf)")
    assert captured["url"].endswith("/SCT123test.send")
    assert captured["body"]["title"] == ["今日论文"]
    assert "下载" in captured["body"]["desp"][0]
    assert captured["timeout"] == 30
