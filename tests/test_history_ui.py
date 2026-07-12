from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_history_date_picker_and_client_loader_are_wired():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    app = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")

    assert 'id="historyDateSelect"' in html
    assert 'id="timeRangeSelect"' not in html
    assert "loadHistoryIndexData()" in app
    assert "loadHistoryDate(event.target.value)" in app
    assert 'url.searchParams.set("date", date)' in app
    assert 'window.addEventListener("popstate"' in app
