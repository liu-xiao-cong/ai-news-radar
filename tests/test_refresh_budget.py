"""Regression coverage for bounded, partial-success news refreshes."""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from scripts import update_news as news


NOW = datetime(2026, 9, 30, 6, tzinfo=timezone.utc)
FETCHERS = (
    "fetch_official_ai_updates", "fetch_curated_ai_media", "fetch_ai_breakfast",
    "fetch_follow_builders", "fetch_techurls", "fetch_buzzing", "fetch_iris",
    "fetch_bestblogs", "fetch_tophub", "fetch_zeli", "fetch_hacker_news_algolia",
    "fetch_ai_hubtoday", "fetch_aibase", "fetch_aihot", "fetch_newsnow",
)
POSIX_TIMER = pytest.mark.skipif(
    not hasattr(signal, "setitimer"), reason="RefreshBudget requires POSIX timers"
)


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def unexpected_request(*args, **kwargs):
        pytest.fail("Regression tests must not access the network")

    monkeypatch.setattr(news.requests.sessions.Session, "request", unexpected_request)


@pytest.fixture
def restore_alarm():
    previous = signal.getsignal(signal.SIGALRM)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def item(source):
    return news.RawItem(
        site_id=source, site_name=source, source=source,
        title=f"OpenAI model release from {source}",
        url=f"https://example.com/{source}", published_at=NOW, meta={},
    )


def short_budget(seconds=5):
    class ShortBudget(news.RefreshBudget):
        def run(self, label, fn, *, seconds=45):
            return super().run(label, fn, seconds=min(seconds, 0.03))

    return ShortBudget(seconds)


@POSIX_TIMER
def test_hard_timer_escapes_broad_exception_handler(restore_alarm):
    budget = news.RefreshBudget(5)
    swallowed = []

    def blocking_fetcher():
        try:
            time.sleep(0.5)
        except Exception:
            swallowed.append(True)
        return "unexpected success"

    started = time.monotonic()
    with pytest.raises(news.StageTimeout, match="slow-source"):
        budget.run("slow-source", blocking_fetcher, seconds=0.03)

    assert time.monotonic() - started < 0.4
    assert swallowed == []
    assert budget.timed_out_stages == ["slow-source"]
    assert not issubclass(news.StageTimeout, Exception)


@POSIX_TIMER
@pytest.mark.parametrize("outcome", ["success", "exception", "timeout"])
def test_alarm_handler_and_timer_are_cleaned_up(outcome, restore_alarm):
    def previous_handler(*args):
        raise AssertionError("Previous alarm should not fire during this test")

    signal.signal(signal.SIGALRM, previous_handler)
    budget = news.RefreshBudget(5)

    def operation():
        if outcome == "exception":
            raise ValueError("fetch failed")
        if outcome == "timeout":
            time.sleep(0.5)
        return 42

    if outcome == "success":
        assert budget.run("source", operation, seconds=0.03) == 42
    else:
        expected = ValueError if outcome == "exception" else news.StageTimeout
        with pytest.raises(expected):
            budget.run("source", operation, seconds=0.03)

    assert signal.getsignal(signal.SIGALRM) is previous_handler
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


@POSIX_TIMER
def test_exhausted_shared_budget_does_not_invoke_next_stage(monkeypatch, restore_alarm):
    clock = [100.0]
    monkeypatch.setattr(news.time, "monotonic", lambda: clock[0])
    budget = news.RefreshBudget(1)
    calls = []

    def first_stage():
        calls.append("first")
        clock[0] += 2
        return "saved"

    assert budget.run("first", first_stage) == "saved"
    with pytest.raises(news.StageTimeout, match="next"):
        budget.run("next", lambda: calls.append("next"))

    assert calls == ["first"]
    assert budget.timed_out_stages == ["next"]


@POSIX_TIMER
def test_remaining_global_budget_limits_stage_timeout(restore_alarm):
    budget = news.RefreshBudget(0.03)
    started = time.monotonic()
    with pytest.raises(news.StageTimeout):
        budget.run("global-limit", lambda: time.sleep(0.5), seconds=10)
    assert time.monotonic() - started < 0.4


@POSIX_TIMER
def test_collect_all_retains_successes_and_continues_after_timeout(monkeypatch, restore_alarm):
    for name in FETCHERS:
        monkeypatch.setattr(news, name, lambda session, now: [])
    before, after = item("official_ai"), item("aibreakfast")
    monkeypatch.setattr(news, "fetch_official_ai_updates", lambda session, now: [before])
    monkeypatch.setattr(news, "fetch_curated_ai_media", lambda session, now: time.sleep(0.5))
    monkeypatch.setattr(news, "fetch_ai_breakfast", lambda session, now: [after])
    budget = short_budget()

    items, statuses = news.collect_all(object(), NOW, budget=budget)

    assert items == [before, after]
    by_source = {status["site_id"]: status for status in statuses}
    assert len(statuses) == len(FETCHERS)
    assert by_source["official_ai"]["ok"]
    assert by_source["aibreakfast"]["ok"]
    assert by_source["aibreakfast"]["item_count"] == 1
    assert not by_source["curated_media"]["ok"]
    assert "budget exhausted" in by_source["curated_media"]["error"]
    assert budget.timed_out_stages == ["curated_media"]


@POSIX_TIMER
def test_official_source_retains_items_fetched_before_timeout(monkeypatch, restore_alarm):
    feeds = [{"title": "First"}, {"title": "Slow"}, {"title": "Never reached"}]
    monkeypatch.setattr(news, "OFFICIAL_AI_FEEDS", feeds)
    saved = item("official_ai")
    calls = []

    def fetch_feed(session, feed, now):
        calls.append(feed["title"])
        if feed["title"] == "Slow":
            time.sleep(0.5)
        return [saved]

    monkeypatch.setattr(news, "fetch_feed_as_official_items", fetch_feed)
    budget = short_budget()
    result = budget.run("official_ai", lambda: news.fetch_official_ai_updates(object(), NOW))

    assert result == [saved]
    assert calls == ["First", "Slow"]
    assert budget.timed_out_stages == ["official_ai"]


def rss_response(title):
    content = f"""<rss version="2.0"><channel><title>Test feed</title><item>
        <title>{title}</title><link>https://example.com/{title}</link>
        <pubDate>Wed, 30 Sep 2026 05:00:00 GMT</pubDate>
        </item></channel></rss>""".encode()
    return SimpleNamespace(content=content, raise_for_status=lambda: None)


@POSIX_TIMER
@pytest.mark.parametrize("exhaust_global_budget", [False, True])
def test_opml_preserves_partial_results_without_waiting_for_threads(
    monkeypatch, tmp_path, restore_alarm, exhaust_global_budget,
):
    opml = tmp_path / "public-test.opml"
    opml.write_text('''<opml version="2.0"><body>
        <outline title="First" xmlUrl="https://example.com/first.xml"/>
        <outline title="Slow" xmlUrl="https://example.com/slow.xml"/>
        <outline title="Last" xmlUrl="https://example.com/last.xml"/>
        </body></opml>''', encoding="utf-8")
    monkeypatch.setattr(news, "feedparser", None)
    budget = short_budget()
    calls = []

    def get(url, **kwargs):
        assert threading.current_thread() is threading.main_thread()
        calls.append(url)
        if url.endswith("first.xml"):
            if exhaust_global_budget:
                budget.deadline = time.monotonic() - 1
            return rss_response("First")
        if url.endswith("slow.xml"):
            time.sleep(0.5)
        return rss_response("Last")

    def no_executor(*args, **kwargs):
        pytest.fail("Bounded RSS must not create an executor with blocking shutdown")

    monkeypatch.setattr(news.requests, "get", get)
    monkeypatch.setattr(news, "ThreadPoolExecutor", no_executor)
    items, summary, statuses = news.fetch_opml_rss(NOW, opml, budget=budget)

    expected = ["First"] if exhaust_global_budget else ["First", "Last"]
    assert [entry.title for entry in items] == expected
    assert len(calls) == (1 if exhaust_global_budget else 3)
    assert summary["item_count"] == len(expected)
    assert summary["ok"]
    assert summary["failed_feed_count"] == (2 if exhaust_global_budget else 1)
    by_feed = {status["feed_title"]: status for status in statuses}
    assert by_feed["First"]["ok"]
    assert not by_feed["Slow"]["ok"]
    assert "budget exhausted" in by_feed["Slow"]["error"]
    assert by_feed["Last"]["ok"] is (not exhaust_global_budget)


@pytest.mark.parametrize("failed_translation", [None, "still English"])
def test_translation_cap_counts_failed_attempts(monkeypatch, failed_translation):
    titles = [f"OpenAI releases new model number {number}" for number in range(6)]
    items = [{"title": title, "url": f"https://example.com/{number}"}
             for number, title in enumerate(titles)]
    calls = []

    def translate(session, title):
        calls.append(title)
        return failed_translation

    monkeypatch.setattr(news, "translate_to_zh_cn", translate)
    ai_items, all_items, cache = news.add_bilingual_fields(
        items, items, object(), {}, max_new_translations=2,
    )

    assert calls == titles[:2]
    assert len(ai_items) == len(all_items) == len(items)
    assert all(entry["title_en"] == title for entry, title in zip(ai_items, titles))
    assert all(entry["title_zh"] is None for entry in ai_items)
    assert cache == {}


def configure_offline_main(monkeypatch, output_dir, raw_items):
    for name in os.environ:
        if name.startswith(("SOCIALDATA_", "TIKHUB_", "X_API_", "EMAIL_DIGEST_")):
            monkeypatch.delenv(name)
    for name in ("EMAIL_DIGEST_ENABLED", "X_API_ENABLED", "SOCIALDATA_ENABLED", "TIKHUB_ENABLED"):
        monkeypatch.setenv(name, "0")
    monkeypatch.setattr(sys, "argv", ["update_news.py", "--output-dir", str(output_dir)])
    monkeypatch.setattr(news, "utc_now", lambda: NOW)
    monkeypatch.setattr(news, "create_session", lambda: object())
    monkeypatch.setattr(news, "collect_all", lambda session, now, budget: (list(raw_items), []))
    monkeypatch.setattr(news, "fetch_waytoagi_recent_7d", lambda *args: {"updates_7d": [], "count_7d": 0})
    budget = short_budget()
    monkeypatch.setattr(news, "RefreshBudget", lambda seconds: budget)
    return budget


def enable_paid_adapter(monkeypatch, source):
    prefix = source.upper()
    monkeypatch.setenv(f"{prefix}_ENABLED", "1")
    monkeypatch.setenv(f"{prefix}_API_KEY", "test-only-fake-key")
    monkeypatch.setenv(f"{prefix}_FORCE_RUN", "0")
    monkeypatch.setenv(f"{prefix}_RUN_UTC_HOUR", str(NOW.hour))
    monkeypatch.setenv(f"{prefix}_RUN_UTC_MINUTE_MAX", "59")
    monkeypatch.setenv(f"{prefix}_RUN_INTERVAL_HOURS", "24")
    monkeypatch.setenv("SOCIALDATA_LIST_ENABLED", "0")


@POSIX_TIMER
@pytest.mark.parametrize("source", ["socialdata", "tikhub"])
def test_main_empty_refresh_preserves_snapshots_and_persists_paid_timeout_gate(
    monkeypatch, tmp_path, restore_alarm, source,
):
    snapshots = {
        "archive.json": '{"items": [], "generated_at": "previous snapshot"}',
        "latest-24h.json": '{"items": ["previous AI items"]}',
        "latest-24h-all.json": '{"items_all": ["previous all-mode items"]}',
        "source-status.json": '{"generated_at": "previous source status"}',
        "daily-brief.json": '{"items": ["previous daily brief"]}',
        "stories-merged.json": '{"stories": ["previous stories"]}',
        "merge-log.json": '{"events": ["previous events"]}',
        "waytoagi-7d.json": '{"updates_7d": ["previous updates"]}',
        "title-zh-cache.json": '{"Old title": "已有中文标题"}',
        "history/index.json": '{"dates": ["previous date"]}',
    }
    for filename, contents in snapshots.items():
        path = tmp_path / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    budget = configure_offline_main(monkeypatch, tmp_path, [])
    enable_paid_adapter(monkeypatch, source)
    calls = []

    def stalled_paid_request(*args, **kwargs):
        calls.append(source)
        time.sleep(0.5)
        pytest.fail("The paid request must be interrupted")

    monkeypatch.setattr(news, f"fetch_{source}_search", stalled_paid_request)
    with pytest.raises(RuntimeError, match="previous snapshot unchanged"):
        news.main()

    state_path = tmp_path / news.PAID_SOURCE_STATE_FILE
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["sources"][source]["last_run_at"] == news.iso(NOW)
    assert state["sources"][source]["last_ok"] is False
    assert state["sources"][source]["last_item_count"] == 0
    assert source in budget.timed_out_stages
    assert calls == [source]

    # A later refresh reloads the persisted cost gate and must not bill again.
    monkeypatch.setattr(news, "utc_now", lambda: NOW + timedelta(minutes=5))
    with pytest.raises(RuntimeError, match="previous snapshot unchanged"):
        news.main()
    assert calls == [source]
    for filename, contents in snapshots.items():
        assert (tmp_path / filename).read_text(encoding="utf-8") == contents
    assert {str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*") if path.is_file()} == (
        set(snapshots) | {news.PAID_SOURCE_STATE_FILE}
    )


@POSIX_TIMER
def test_main_translation_timeout_writes_complete_schema_using_completed_cache(
    monkeypatch, tmp_path, restore_alarm,
):
    first, second = item("official_ai"), item("official_ai")
    first.title = "OpenAI releases a new language model for reasoning"
    first.url = "https://openai.com/news/reasoning"
    second.title = "Anthropic launches Claude Code security scanning for developers"
    second.url = "https://anthropic.com/news/code-security"
    budget = configure_offline_main(monkeypatch, tmp_path, [first, second])
    translated_title = "OpenAI 发布全新推理语言模型"
    calls = []

    def translate(session, title):
        calls.append(title)
        if title == first.title:
            return translated_title
        time.sleep(0.5)
        pytest.fail("The second translation must be interrupted")

    monkeypatch.setattr(news, "translate_to_zh_cn", translate)
    assert news.main() == 0
    assert calls == [first.title, second.title]
    assert budget.timed_out_stages == ["translations"]

    latest = json.loads((tmp_path / "latest-24h.json").read_text(encoding="utf-8"))
    all_mode = json.loads((tmp_path / "latest-24h-all.json").read_text(encoding="utf-8"))
    history = json.loads((tmp_path / "history" / "2026-09-30.json").read_text(encoding="utf-8"))
    for records in (latest["items"], latest["items_ai"], all_mode["items_all"],
                    all_mode["items_all_raw"], history["items"]):
        assert len(records) == 2
        by_title = {record["title_original"]: record for record in records}
        assert by_title[first.title]["title_zh"] == translated_title
        assert by_title[first.title]["title_en"] == first.title
        assert by_title[first.title]["title_bilingual"] == f"{translated_title} / {first.title}"
        assert by_title[second.title]["title_en"] == second.title
        assert by_title[second.title]["title_zh"] is None
        assert by_title[second.title]["title_bilingual"] == second.title
    cache = json.loads((tmp_path / "title-zh-cache.json").read_text(encoding="utf-8"))
    assert cache == {first.title: translated_title}
    status = json.loads((tmp_path / "source-status.json").read_text(encoding="utf-8"))
    assert status["refresh_partial"] is True
    assert status["timed_out_stages"] == ["translations"]


@POSIX_TIMER
@pytest.mark.parametrize("platform", ["douyin", "xiaohongshu"])
def test_tikhub_timeout_retains_partial_items_diagnostics_and_attempted_status(
    monkeypatch, restore_alarm, platform,
):
    enable_paid_adapter(monkeypatch, "tikhub")
    monkeypatch.setenv("TIKHUB_QUERY", "OpenAI,Claude")
    monkeypatch.setenv("TIKHUB_PLATFORMS", platform)
    monkeypatch.setenv("TIKHUB_MAX_RESULTS", "4")
    monkeypatch.setenv("TIKHUB_DAILY_ITEM_LIMIT", "4")
    saved = item(f"tikhub_{platform}")
    calls = []

    def request(*args, **kwargs):
        calls.append(args[0])
        if len(calls) > 1:
            time.sleep(0.5)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {})

    monkeypatch.setattr(news, f"parse_tikhub_{platform}_items", lambda *args, **kwargs: [saved])
    session = SimpleNamespace(get=request, post=request)
    state = {"schema_version": 1, "sources": {}}
    budget = short_budget()
    items, status = budget.optional(
        "tikhub", lambda: news.maybe_fetch_tikhub_updates(session, NOW, state),
        news.tikhub_status_base(NOW, state), empty=[],
    )

    assert items == [saved]
    assert status["attempted"] is True
    assert status["ok"] is False
    assert status["item_count"] == 1
    assert status["diagnostics"]["timed_out"] is True
    assert status["diagnostics"]["mapped_item_count"] == 1
    assert status["diagnostics"]["successful_request_count"] == 1
    assert len(calls) == 2
    news.update_paid_source_state(state, "tikhub", status, NOW)
    assert news.tikhub_should_run_now(NOW + timedelta(minutes=5), state) == (
        False, "before_tikhub_run_interval",
    )


@POSIX_TIMER
@pytest.mark.parametrize("timeout_at", ["search_first", "search_next", "list_first", "list_next"])
def test_socialdata_timeout_finalizes_completed_read_accounting(
    monkeypatch, restore_alarm, timeout_at,
):
    enable_paid_adapter(monkeypatch, "socialdata")
    monkeypatch.setenv("SOCIALDATA_LIST_ENABLED", "1")
    monkeypatch.setenv("SOCIALDATA_LIST_ID", "test-list")
    monkeypatch.setenv("SOCIALDATA_MAX_RESULTS", "10")
    monkeypatch.setenv("SOCIALDATA_DAILY_TWEET_LIMIT", "10")
    monkeypatch.setenv("SOCIALDATA_LIST_MAX_RESULTS", "10")
    calls = []

    def tweet(tweet_id, *, stale=False, tweet_type="tweet"):
        return {
            "id_str": tweet_id,
            "full_text": f"OpenAI model release {tweet_id}",
            "created_at": news.iso(NOW - timedelta(days=10) if stale else NOW),
            "type": tweet_type,
            "user": {"screen_name": "test_author", "default_profile_image": False},
        }

    def request(url, **kwargs):
        source = "list" if "/twitter/list/" in url else "search"
        page = "next" if kwargs["params"].get("cursor") else "first"
        calls.append(f"{source}_{page}")
        if f"{source}_{page}" == timeout_at:
            time.sleep(0.5)
            pytest.fail("The paid request must be interrupted")
        tweets = (
            [tweet("search-current"), tweet("search-stale", stale=True)]
            if source == "search"
            else [tweet("list-current"), tweet("list-retweet", tweet_type="retweet")]
        )
        payload = {"tweets": tweets}
        if timeout_at == f"{source}_next":
            payload["next_cursor"] = "next-page"
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)

    session = SimpleNamespace(get=request)
    state = {"schema_version": 1, "sources": {}}
    budget = short_budget()
    items, status = budget.optional(
        "socialdata", lambda: news.maybe_fetch_socialdata_updates(session, NOW, state),
        news.socialdata_status_base(NOW, state), empty=[],
    )

    search_count = 0 if timeout_at == "search_first" else 1
    list_count = 1 if timeout_at == "list_next" else 0
    raw_reads = 2 * (search_count + list_count)
    assert len(items) == status["item_count"] == search_count + list_count
    assert status["attempted"] is True
    assert status["ok"] is False
    assert status["timed_out"] is True
    assert "StageTimeout" in status["error"] or "budget exhausted" in status["error"]
    assert status["list_enabled"] is True
    assert status["search_item_count"] == search_count
    assert status["list_item_count"] == list_count
    assert status["raw_reads"] == raw_reads
    assert status["estimated_cost_usd"] == round(raw_reads * news.SOCIALDATA_TWEET_READ_COST_USD, 4)
    assert status["skipped_stale_count"] == search_count
    assert budget.timed_out_stages == ["socialdata"]
    assert calls == {
        "search_first": ["search_first"],
        "search_next": ["search_first", "search_next"],
        "list_first": ["search_first", "list_first"],
        "list_next": ["search_first", "list_first", "list_next"],
    }[timeout_at]
    news.update_paid_source_state(state, "socialdata", status, NOW)
    assert news.socialdata_should_run_now(NOW + timedelta(minutes=5), state) == (
        False, "before_socialdata_run_interval",
    )


@POSIX_TIMER
@pytest.mark.parametrize("source,failure_stage", [
    ("socialdata", "next_paid_adapter"),
    ("socialdata", "processing"),
    ("tikhub", "processing"),
])
def test_main_checkpoints_paid_gate_before_later_failure(
    monkeypatch, tmp_path, restore_alarm, source, failure_stage,
):
    configure_offline_main(monkeypatch, tmp_path, [item("official_ai")])
    monkeypatch.setattr(sys, "argv", [
        "update_news.py", "--output-dir", str(tmp_path), "--translate-max-new", "0",
    ])
    enable_paid_adapter(monkeypatch, source)
    calls = []

    def paid_request(*args, **kwargs):
        calls.append(source)
        site_id = "socialdata_x" if source == "socialdata" else "tikhub_douyin"
        return [item(site_id)], {"raw_tweet_count": 1}

    def fail_later(*args, **kwargs):
        raise RuntimeError("injected downstream failure")

    monkeypatch.setattr(news, f"fetch_{source}_search", paid_request)
    target = "maybe_fetch_tikhub_updates" if failure_stage == "next_paid_adapter" else "build_history_payloads"
    monkeypatch.setattr(news, target, fail_later)
    snapshot = '{"items": ["previous snapshot"]}'
    (tmp_path / "latest-24h.json").write_text(snapshot, encoding="utf-8")

    for run_time in (NOW, NOW + timedelta(minutes=5)):
        monkeypatch.setattr(news, "utc_now", lambda: run_time)
        with pytest.raises(RuntimeError, match="injected downstream failure"):
            news.main()
        state = json.loads((tmp_path / news.PAID_SOURCE_STATE_FILE).read_text(encoding="utf-8"))
        assert state["sources"][source]["last_run_at"] == news.iso(NOW)
        assert state["sources"][source]["last_ok"] is True
        assert state["sources"][source]["last_item_count"] == 1
        assert (tmp_path / "latest-24h.json").read_text(encoding="utf-8") == snapshot
    assert calls == [source]
