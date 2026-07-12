from __future__ import annotations

import json
from datetime import datetime, timezone

from scripts.update_news import build_history_payloads, history_day_key, write_history_payloads


NOW = datetime(2026, 7, 12, 4, 0, tzinfo=timezone.utc)


def record(
    item_id: str,
    title: str,
    published_at: str,
    *,
    url: str | None = None,
    site_id: str = "official_ai",
    site_name: str = "Official AI Updates",
    source: str = "OpenAI",
) -> dict:
    return {
        "id": item_id,
        "site_id": site_id,
        "site_name": site_name,
        "source": source,
        "title": title,
        "url": url or f"https://example.com/{item_id}",
        "published_at": published_at,
        "first_seen_at": published_at,
        "last_seen_at": "2026-07-12T04:00:00Z",
    }


def test_history_day_key_uses_shanghai_calendar_day():
    item = record("boundary", "OpenAI releases GPT-6", "2026-07-10T17:30:00Z")
    assert history_day_key(item) == "2026-07-11"


def test_build_history_payloads_filters_scores_and_deduplicates():
    shared_url = "https://example.com/openai-gpt-6"
    archive = {
        "a": record("a", "OpenAI releases GPT-6 model", "2026-07-10T17:30:00Z", url=shared_url),
        "b": record("b", "OpenAI releases GPT-6 model", "2026-07-10T17:30:00Z", url=shared_url),
        "noise": record(
            "noise",
            "Gardening tips for tomatoes",
            "2026-07-10T19:00:00Z",
            site_id="techurls",
            site_name="TechURLs",
            source="Reddit",
        ),
        "old": record("old", "Anthropic releases Claude update", "2026-06-01T00:00:00Z"),
    }

    payloads = build_history_payloads(
        archive,
        now=NOW,
        archive_days=21,
        generated_at="2026-07-12T04:00:00Z",
    )

    assert [payload["date"] for payload in payloads] == ["2026-07-11"]
    payload = payloads[0]
    assert payload["timezone"] == "Asia/Shanghai"
    assert payload["total_items_raw"] == 3
    assert payload["total_items"] == 1
    assert payload["items_ai"][0]["ai_is_related"] is True
    assert payload["site_stats"] == [
        {
            "site_id": "official_ai",
            "site_name": "Official AI Updates",
            "count": 1,
            "raw_count": 2,
        }
    ]


def test_write_history_payloads_creates_index_and_removes_expired_days(tmp_path):
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    (history_dir / "2026-06-01.json").write_text("{}", encoding="utf-8")
    payloads = build_history_payloads(
        {"a": record("a", "OpenAI releases GPT-6 model", "2026-07-11T01:00:00Z")},
        now=NOW,
        archive_days=21,
        generated_at="2026-07-12T04:00:00Z",
    )

    index = write_history_payloads(
        history_dir,
        payloads,
        generated_at="2026-07-12T04:00:00Z",
    )

    assert not (history_dir / "2026-06-01.json").exists()
    assert (history_dir / "2026-07-11.json").exists()
    assert index["latest_date"] == "2026-07-11"
    saved_index = json.loads((history_dir / "index.json").read_text(encoding="utf-8"))
    assert saved_index["dates"][0]["file"] == "data/history/2026-07-11.json"
