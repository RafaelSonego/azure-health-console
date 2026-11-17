import io

import pytest

from healthconsole.core import status_feed
from healthconsole.core.guarded import ServiceError, WriteBlocked
from tests.conftest import REAL_FETCH_XML


class Response:
    def __init__(self, status, body=b""):
        self.status_code, self.raw = status, io.BytesIO(body)
        self.raw.read = lambda n, decode_content=True, _r=self.raw.read: _r(n)


class Session:
    def __init__(self, response):
        self.response, self.sent = response, []

    def request(self, method, url, **request):
        self.sent.append((method, url, request))
        return self.response


@pytest.mark.parametrize("method,url", [
    ("POST", status_feed.FEED_URL),
    ("GET", "https://evil.example/en-us/status/feed/"),
    ("GET", "https://azure.status.microsoft/en-us/status/other"),
    ("GET", "http://azure.status.microsoft/en-us/status/feed/"),
])
def test_only_the_public_feed_can_be_read(method, url):
    session = Session(Response(200))
    with pytest.raises(WriteBlocked):
        status_feed._FEED.send(session, method, url, 5)
    assert session.sent == []  # refused before anything left the machine


def test_the_feed_is_read_without_redirects_and_capped():
    session = Session(Response(200, b"<rss/>"))
    real = REAL_FETCH_XML
    assert real(session) == b"<rss/>"
    method, url, request = session.sent[0]
    assert (method, url, request["allow_redirects"]) == ("GET", status_feed.FEED_URL, False)
    assert "Authorization" not in request["headers"]
    with pytest.raises(ServiceError, match="answered 301"):
        real(Session(Response(301)))
    with pytest.raises(ServiceError, match="more than"):
        real(Session(Response(200, b"x" * (status_feed.MAX_BYTES + 1))))


def test_parse_strips_markup_keeps_https_links_and_sorts_newest_first():
    xml = (b'<rss><channel>'
           b'<item><title>Old</title><link>https://azure.status.microsoft/x</link><pubDate>Fri, 13 Nov 2026 10:00:00 Z</pubDate></item>'
           b'<item><title>New</title><link>http://plain.example/</link><description>&lt;p&gt;A&amp;amp;B &lt;br/&gt; down&lt;/p&gt;</description>'
           b'<pubDate>Sat, 14 Nov 2026 10:00:00 Z</pubDate></item>'
           b'<item><title></title><pubDate>not a date</pubDate></item></channel></rss>')
    items = status_feed.parse(xml)
    assert [i["title"] for i in items] == ["New", "Old", "Azure status update"]
    assert items[0]["text"] == "A&B down" and items[0]["link"] == status_feed.STATUS_PAGE
    assert items[1]["link"] == "https://azure.status.microsoft/x" and items[2]["published"] == ""
    assert len(status_feed._text("word " * 200)) == status_feed.DETAIL_CHARS


def test_a_bad_feed_is_reported_and_kept_briefly(monkeypatch):
    calls = []
    monkeypatch.setattr(status_feed, "fetch_xml", lambda session=None: calls.append(1) or b"<rss><not closed")
    first = status_feed.read()
    assert (first["items"], first["error"]) == ([], "azure.status.microsoft sent a feed that couldn't be read.")
    assert first["read_at"].endswith("+00:00")
    assert status_feed.read() == first and calls == [1]
    status_feed.read(refresh=True)
    assert calls == [1, 1]
