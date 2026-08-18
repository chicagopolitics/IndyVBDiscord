"""Webhook delivery tests, using a stub session so nothing hits the network."""
import pytest

from indyvb.publish import (ConsolePublisher, PublishError, WebhookPublisher,
                            publish_all)

WEBHOOK = "https://discord.com/api/webhooks/123/abc"


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class FakeSession:
    """Replays a queued list of responses and records what was posted."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json})
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Keep retry/backoff paths instant."""
    monkeypatch.setattr("indyvb.publish.time.sleep", lambda *_: None)


def make_publisher(responses, **kwargs):
    return WebhookPublisher(WEBHOOK, session=FakeSession(responses), **kwargs)


class TestWebhookPublisher:
    def test_rejects_empty_url(self):
        with pytest.raises(PublishError, match="No Discord webhook URL"):
            WebhookPublisher("")

    def test_rejects_non_discord_url(self):
        """Catch a pasted-wrong URL before it silently posts nowhere."""
        with pytest.raises(PublishError, match="does not look like"):
            WebhookPublisher("https://example.com/hook")

    def test_sends_embeds(self):
        pub = make_publisher([FakeResponse(204)])
        pub.send([{"title": "one"}], content="hello")
        sent = pub.session.calls[0]["json"]
        assert sent["embeds"] == [{"title": "one"}]
        assert sent["content"] == "hello"

    def test_suppresses_mentions(self):
        """A scraped league name must never be able to ping the server."""
        pub = make_publisher([FakeResponse(204)])
        pub.send([{"title": "@everyone free pizza"}])
        assert pub.session.calls[0]["json"]["allowed_mentions"] == {"parse": []}

    def test_applies_username_override(self):
        pub = make_publisher([FakeResponse(204)], username="VB Bot")
        pub.send([{"title": "x"}])
        assert pub.session.calls[0]["json"]["username"] == "VB Bot"

    def test_retries_on_rate_limit(self):
        pub = make_publisher([
            FakeResponse(429, {"retry_after": 0.01}),
            FakeResponse(204),
        ])
        pub.send([{"title": "x"}])
        assert len(pub.session.calls) == 2

    def test_retries_on_server_error(self):
        pub = make_publisher([FakeResponse(502), FakeResponse(204)])
        pub.send([{"title": "x"}])
        assert len(pub.session.calls) == 2

    def test_raises_on_client_error(self):
        pub = make_publisher([FakeResponse(400, text="bad embed")])
        with pytest.raises(PublishError, match="400"):
            pub.send([{"title": "x"}])

    def test_gives_up_after_persistent_rate_limiting(self):
        pub = make_publisher([FakeResponse(429, {"retry_after": 0.01})] * 6)
        with pytest.raises(PublishError, match="Giving up"):
            pub.send([{"title": "x"}])

    def test_handles_malformed_rate_limit_body(self):
        """A non-JSON 429 must not crash the retry path."""
        pub = make_publisher([FakeResponse(429, text="<html>"), FakeResponse(204)])
        pub.session.responses[0].json = lambda: (_ for _ in ()).throw(ValueError())
        pub.send([{"title": "x"}])
        assert len(pub.session.calls) == 2


class TestPublishAll:
    def test_header_only_on_first_message(self):
        pub = ConsolePublisher()
        sent = publish_all(pub, [[{"title": "a"}], [{"title": "b"}]], header="Digest")
        assert sent == 2
        assert pub.messages[0]["content"] == "Digest"
        assert pub.messages[1]["content"] is None

    def test_counts_all_embeds(self):
        pub = ConsolePublisher()
        assert publish_all(pub, [[{"title": "a"}, {"title": "b"}]]) == 2
