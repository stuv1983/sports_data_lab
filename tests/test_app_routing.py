import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_navigation_pages_cannot_bypass_app_bootstrap():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    page_paths = re.findall(r'st\.Page\("([^"]+\.py)"', source)

    assert page_paths
    assert not (ROOT / "pages").exists()
    assert all(path.startswith("app_pages/") for path in page_paths)
    assert all((ROOT / path).is_file() for path in page_paths)
