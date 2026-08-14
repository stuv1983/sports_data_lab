import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

APP = (ROOT / "app.py").read_text(encoding="utf-8")


def test_navigation_pages_cannot_bypass_app_bootstrap():
    # Every page script named anywhere in app.py, whether it reaches
    # st.Page directly or through the gated-page helper.
    page_paths = re.findall(r'"([^"]+\.py)"', APP)

    assert page_paths
    assert not (ROOT / "pages").exists()
    assert all(path.startswith("app_pages/") for path in page_paths)
    assert all((ROOT / path).is_file() for path in page_paths)


def test_a_protected_page_is_hidden_from_the_menu_rather_than_unregistered():
    """A page missing from the catalogue is a URL Streamlit does not know.

    Building the catalogue out of ``can_access`` answered "page cannot be
    found" to anyone who opened a protected page in a new tab -- a second
    tab starts as a fresh session, so it is anonymous until the log in
    cookie has been read back. Registering the page and hiding it puts the
    log in gate there instead.
    """
    features = re.search(r"_PROTECTED_PAGES = \{(.*?)\}", APP, re.S)
    assert features
    protected = re.findall(r'"([^"]+)":\s*"([a-z_]+)"', features.group(1))
    assert protected

    for title, feature in protected:
        assert f'_PROTECTED_PAGES["{title}"]' in APP, title

    # The helper is the only thing that decides a protected page's
    # visibility, and it registers unconditionally.
    helper = re.search(r"def _gated_page\(.*?\n\n\n", APP, re.S).group(0)
    assert 'visibility=("visible" if accounts.can_access' in helper
    assert "hidden" in helper
