"""End-to-end CLI tests with the network and Discord stubbed out."""
from datetime import date, timedelta

import pytest

from indyvb import cli
from indyvb.models import Event
from indyvb.publish import ConsolePublisher

TODAY = date.today()


def make_event(source_id="1", **kwargs) -> Event:
    defaults = dict(
        source="cca", source_name="CCA Sports", kind="league",
        source_id=source_id, name=f"League {source_id}",
        url=f"https://example.com/l/{source_id}",
        start_date=TODAY + timedelta(days=7), status="Open",
    )
    defaults.update(kwargs)
    return Event(**defaults)


@pytest.fixture
def stub(monkeypatch):
    """Replace the scrape and the Discord client with controllable stubs."""
    state = {"events": [make_event("1"), make_event("2")], "errors": {}, "sent": []}

    def fake_collect(slugs, fetcher):
        return list(state["events"]), dict(state["errors"])

    class RecordingPublisher(ConsolePublisher):
        def send(self, embeds, content=None):
            state["sent"].append(embeds)

    monkeypatch.setattr(cli, "collect", fake_collect)
    monkeypatch.setattr(cli, "WebhookPublisher",
                        lambda *a, **k: RecordingPublisher())
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/x")
    return state


def run(argv):
    return cli.main(argv)


class TestPostNewOnly:
    def test_first_run_posts_everything_then_second_run_posts_nothing(
            self, stub, tmp_path):
        """The core scheduled-run behaviour: never repeat yourself."""
        args = ["post", "--new-only", "--state", str(tmp_path / "seen.json")]

        assert run(args) == 0
        first = sum(len(b) for b in stub["sent"])
        assert first == 2

        stub["sent"].clear()
        assert run(args) == 0
        assert stub["sent"] == []

    def test_changed_event_is_reposted(self, stub, tmp_path):
        args = ["post", "--new-only", "--state", str(tmp_path / "seen.json")]
        run(args)
        stub["sent"].clear()

        stub["events"] = [make_event("1"), make_event("2", status="Sold Out")]
        run(args)
        assert sum(len(b) for b in stub["sent"]) == 1

    def test_dry_run_does_not_record_state(self, stub, tmp_path):
        """A preview must not consume the events it previewed."""
        state_file = tmp_path / "seen.json"
        args = ["post", "--new-only", "--state", str(state_file)]

        run(args + ["--dry-run"])
        assert not state_file.exists()

        assert run(args) == 0
        assert sum(len(b) for b in stub["sent"]) == 2

    def test_partial_failure_aborts_without_recording(self, stub, tmp_path):
        """If a source broke, its listings must not be marked as announced."""
        state_file = tmp_path / "seen.json"
        stub["errors"] = {"ibeach-leagues": "HTTPError: 500"}

        assert run(["post", "--new-only", "--state", str(state_file)]) == 2
        assert not state_file.exists()
        assert stub["sent"] == []

    def test_allow_partial_overrides_the_abort(self, stub, tmp_path):
        stub["errors"] = {"ibeach-leagues": "HTTPError: 500"}
        code = run(["post", "--new-only", "--allow-partial",
                    "--state", str(tmp_path / "seen.json")])
        assert code == 0
        assert sum(len(b) for b in stub["sent"]) == 2


class TestFilters:
    def test_within_limits_horizon(self, stub, capsys):
        stub["events"] = [
            make_event("soon", start_date=TODAY + timedelta(days=3)),
            make_event("later", start_date=TODAY + timedelta(days=90)),
        ]
        run(["list", "--within", "30"])
        out = capsys.readouterr().out
        assert "League soon" in out
        assert "League later" not in out

    def test_open_only(self, stub, capsys):
        stub["events"] = [
            make_event("open", status="Open"),
            make_event("shut", status="Closed"),
        ]
        run(["list", "--open-only"])
        out = capsys.readouterr().out
        assert "League open" in out
        assert "League shut" not in out

    def test_past_events_hidden_by_default(self, stub, capsys):
        stub["events"] = [make_event("gone", start_date=TODAY - timedelta(days=5))]
        run(["list"])
        assert "League gone" not in capsys.readouterr().out

    def test_kind_filter(self, stub, capsys):
        stub["events"] = [
            make_event("lg", kind="league"),
            make_event("tn", kind="tournament"),
        ]
        run(["list", "--kind", "tournament"])
        out = capsys.readouterr().out
        assert "League tn" in out
        assert "League lg" not in out

    def test_search_matches_location(self, stub, capsys):
        stub["events"] = [
            make_event("a", location="Fishers YMCA"),
            make_event("b", location="Jordan YMCA"),
        ]
        run(["list", "--search", "fishers"])
        out = capsys.readouterr().out
        assert "League a" in out
        assert "League b" not in out


class TestHealth:
    """`health` inspects each source individually, so it is stubbed at the
    registry rather than at `collect`."""

    @pytest.fixture
    def fake_registry(self, monkeypatch):
        def install(result, allow_empty=False):
            class FakeSource:
                slug = "fake"
                name = "Fake Source"

                def __init__(self):
                    self.allow_empty = allow_empty

                def safe_fetch(self, fetcher):
                    return result

            monkeypatch.setattr(cli.source_registry, "build",
                                lambda slugs: [FakeSource()])
        return install

    def test_reports_healthy(self, fake_registry, capsys):
        fake_registry(([make_event("1")], None))
        assert run(["health"]) == 0
        assert "1/1 sources healthy" in capsys.readouterr().out

    def test_empty_source_is_a_failure(self, fake_registry, capsys):
        """Zero events means the markup probably changed - do not call that OK."""
        fake_registry(([], None))
        assert run(["health"]) == 1
        assert "EMPTY" in capsys.readouterr().out

    def test_empty_is_fine_for_sources_that_allow_it(self, fake_registry, capsys):
        """GroupMe having nothing scheduled is normal, not a broken parser."""
        fake_registry(([], None), allow_empty=True)
        assert run(["health"]) == 0
        out = capsys.readouterr().out
        assert "EMPTY" not in out
        assert "no events currently scheduled" in out

    def test_fetch_error_is_a_failure(self, fake_registry, capsys):
        fake_registry(([], "HTTPError: 503"))
        assert run(["health"]) == 1
        assert "FAIL" in capsys.readouterr().out

    def test_undated_events_warn(self, fake_registry, capsys):
        fake_registry(([make_event("1", start_date=None)], None))
        assert run(["health"]) == 0
        assert "WARN" in capsys.readouterr().out


def test_verbose_works_after_subcommand(stub):
    """-v must be accepted in its natural position, not only before the verb."""
    assert run(["list", "--verbose"]) == 0


def test_unknown_source_is_rejected():
    with pytest.raises(ValueError, match="unknown source"):
        from indyvb import sources
        sources.build(["nope"])


class TestForumPosting:
    """Forum mode: one thread per listing, tags required."""

    @pytest.fixture
    def tag_file(self, tmp_path):
        import json as _json
        path = tmp_path / "forum_tags.json"
        path.write_text(_json.dumps({"tags": {
            "Sand": "1", "Indoor": "2", "Grass": "3", "Open Play": "4",
            "Doubles": "5", "Quads": "6", "Reverse Co-Ed": "7",
            "League": "8", "Tournament": "9",
        }}), encoding="utf-8")
        return str(path)

    @pytest.fixture
    def forum(self, stub, monkeypatch):
        """Capture threads instead of posting them."""
        threads = []

        class RecordingForum:
            def __init__(self, *a, **k):
                pass

            def send_thread(self, thread_name, embeds, applied_tags=None,
                            content=None):
                if getattr(self, "fail_at", None) == len(threads):
                    from indyvb.publish import PublishError
                    raise PublishError("simulated outage")
                threads.append({"name": thread_name, "tags": applied_tags})

        monkeypatch.setattr(cli, "ForumWebhookPublisher", RecordingForum)
        stub["threads"] = threads
        stub["forum_cls"] = RecordingForum
        return stub

    def test_creates_one_thread_per_listing(self, forum, tag_file, tmp_path):
        forum["events"] = [make_event("1"), make_event("2")]
        code = run(["post", "--forum", "--tag-config", tag_file,
                    "--state", str(tmp_path / "seen.json")])
        assert code == 0
        assert len(forum["threads"]) == 2

    def test_applies_derived_tags(self, forum, tag_file, tmp_path):
        forum["events"] = [make_event("1", name="Reverse Coed 4v4", kind="league")]
        run(["post", "--forum", "--tag-config", tag_file,
             "--state", str(tmp_path / "seen.json")])
        applied = forum["threads"][0]["tags"]
        assert "8" in applied          # League
        assert "6" in applied          # Quads
        assert "7" in applied          # Reverse Co-Ed

    def test_aborts_before_posting_if_a_tag_cannot_resolve(
            self, forum, tmp_path):
        """The forum rejects untagged posts, so fail before sending any."""
        import json as _json
        empty = tmp_path / "empty.json"
        empty.write_text(_json.dumps({"tags": {}}), encoding="utf-8")

        code = run(["post", "--forum", "--tag-config", str(empty),
                    "--state", str(tmp_path / "seen.json")])
        assert code == 2
        assert forum["threads"] == []

    def test_missing_tag_config_is_reported(self, forum, tmp_path):
        code = run(["post", "--forum", "--tag-config", str(tmp_path / "nope.json"),
                    "--state", str(tmp_path / "seen.json")])
        assert code == 2
        assert forum["threads"] == []

    def test_partial_failure_records_only_what_posted(
            self, forum, tag_file, tmp_path):
        """A mid-run outage must not orphan or duplicate threads."""
        state = tmp_path / "seen.json"
        forum["events"] = [make_event(str(i)) for i in range(4)]
        forum["forum_cls"].fail_at = 2      # third thread blows up

        code = run(["post", "--forum", "--new-only", "--tag-config", tag_file,
                    "--state", str(state)])
        assert code == 2
        assert len(forum["threads"]) == 2

        # Re-running posts only the two that never made it.
        forum["forum_cls"].fail_at = None
        forum["threads"].clear()
        assert run(["post", "--forum", "--new-only", "--tag-config", tag_file,
                    "--state", str(state)]) == 0
        assert len(forum["threads"]) == 2

    def test_dry_run_sends_nothing(self, forum, tag_file, tmp_path):
        forum["events"] = [make_event("1")]
        run(["post", "--forum", "--dry-run", "--tag-config", tag_file,
             "--state", str(tmp_path / "seen.json")])
        assert forum["threads"] == []
