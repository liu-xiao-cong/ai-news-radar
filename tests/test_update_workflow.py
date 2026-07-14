from pathlib import Path


WORKFLOW = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "update-news.yml")


def test_update_workflow_has_staggered_fallbacks_and_deduplication_guard():
    text = WORKFLOW.read_text(encoding="utf-8")

    assert 'cron: "17,37,57 0-3 * * *"' in text
    assert 'cron: "17 4 * * *"' in text
    assert "Check whether today's snapshot already exists" in text
    assert "should_update=false" in text
    assert "if: steps.freshness.outputs.should_update == 'true'" in text
    assert "cancel-in-progress: false" in text
    assert "git rebase origin/master" in text
