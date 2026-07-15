from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_cong_branding_is_used_in_the_public_shell():
    index = (ROOT / "index.html").read_text(encoding="utf-8")
    manifest = (ROOT / "site.webmanifest").read_text(encoding="utf-8")

    assert "Cong's AI Radar" in index
    assert "CONG'S SIGNAL LAB" in index
    assert "liu-xiao-cong.github.io/ai-news-radar" in index
    assert "Cong's AI Radar" in manifest
    assert '"src": "./assets/logo.svg"' in manifest
