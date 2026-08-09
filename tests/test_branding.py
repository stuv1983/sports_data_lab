"""The tab icon and the iOS home-screen icon.

`static/icons/` is empty in the repository -- the icons are the operator's,
not the project's -- so these tests write their own PNGs into a temporary
directory and point `branding.ICON_DIR` at it. The one thing asserted about
the real folder is that an empty one changes nothing.
"""

import json
import re

import pytest

import branding
import sports


@pytest.fixture
def icons(tmp_path, monkeypatch):
    """An icon folder under test control, empty to begin with."""
    monkeypatch.setattr(branding, "ICON_DIR", tmp_path)
    return tmp_path


def _png(path):
    """Smallest thing that is honestly a file; nothing here decodes it."""
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    return path


# ------------------------------------------------------------ which file

def test_with_no_icons_at_all_a_sport_keeps_its_emoji(icons):
    """The fallback that makes this whole module optional: drop nothing in
    and the app looks exactly as it did before it existed."""
    assert branding.favicon_file("nba") is None
    assert branding.page_icon(sports.NBA) == sports.NBA.icon


def test_a_default_icon_covers_every_sport(icons):
    _png(icons / "default.png")
    for sport in (sports.AFL, sports.NBA, sports.MLB, sports.NFL):
        assert branding.page_icon(sport) == str(icons / "default.png")


def test_a_sports_own_icon_beats_the_default(icons):
    _png(icons / "default.png")
    _png(icons / "nba.png")
    assert branding.page_icon(sports.NBA) == str(icons / "nba.png")
    assert branding.page_icon(sports.AFL) == str(icons / "default.png")


def test_one_square_png_is_enough_for_both_icons(icons):
    """Without this, setting a home-screen icon would mean remembering to
    add a second file named -180, and forgetting would silently leave iOS
    with a screenshot of the page."""
    _png(icons / "afl.png")
    assert branding.apple_icon_file("afl") == icons / "afl.png"


def test_a_180_file_is_preferred_for_the_home_screen(icons):
    _png(icons / "afl.png")
    _png(icons / "afl-180.png")
    assert branding.apple_icon_file("afl") == icons / "afl-180.png"
    # ...and does not become the tab icon in the process
    assert branding.favicon_file("afl") == icons / "afl.png"


# ------------------------------------------------------- the head script

def _tags(script):
    """The tag specifications the injected script would append."""
    payload = re.search(r"\n  (\[.*?\])\.forEach", script, re.DOTALL)
    assert payload, script
    return json.loads(payload.group(1))


def test_the_home_screen_icon_is_served_by_url_not_inlined(icons):
    """iOS has never reliably honoured a data: URI here, so the link has to
    point at Streamlit's static file server."""
    _png(icons / "nba-180.png")
    tags = _tags(branding.head_script(sports.NBA))
    hrefs = [t["href"] for t in tags if t["tag"] == "link"]
    assert hrefs, "no apple-touch-icon link was written"
    for href in hrefs:
        assert href.startswith("/app/static/icons/nba-180.png?v=")


def test_a_replaced_icon_is_not_served_from_cache_forever(icons):
    """The version stamp is the file's own mtime, so overwriting the PNG is
    all it takes -- no rename, no restart."""
    path = _png(icons / "default-180.png")
    first = _tags(branding.head_script(sports.AFL))[0]["href"]

    import os
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 60))
    assert _tags(branding.head_script(sports.AFL))[0]["href"] != first


def test_without_static_serving_the_meta_tags_still_go_in(icons):
    """A link to a URL nothing answers would give iOS a broken icon, which
    is worse than none. The web-app tags cost nothing and stay."""
    _png(icons / "default-180.png")
    tags = _tags(branding.head_script(sports.AFL, static_serving=False))
    assert not [t for t in tags if t["tag"] == "link"]
    assert [t for t in tags if t["name"] == "apple-mobile-web-app-title"]


def test_the_home_screen_title_is_the_sport_being_read(icons):
    tags = _tags(branding.head_script(sports.MLB))
    title = [t for t in tags if t.get("name") == "apple-mobile-web-app-title"]
    assert title and title[0]["content"] == sports.MLB.label


def test_the_status_bar_is_tinted_only_when_a_colour_is_known(icons):
    plain = _tags(branding.head_script(sports.AFL))
    assert not [t for t in plain if t.get("name") == "theme-color"]

    tinted = _tags(branding.head_script(sports.AFL, theme_color="#071B2B"))
    colour = [t for t in tinted if t.get("name") == "theme-color"]
    assert colour and colour[0]["content"] == "#071B2B"


def test_switching_sport_replaces_the_icon_rather_than_adding_one(icons):
    """iOS picks among every apple-touch-icon in the head. Leaving the last
    sport's link there would make which icon you get a coin toss."""
    script = branding.head_script(sports.NFL)
    # The attribute the script writes and the one it cleans up by must be
    # spelled identically; `element.dataset` silently rewrites the case and
    # broke exactly that, so tags piled up on every rerun.
    assert "setAttribute(mark" in script
    assert f'"{branding._MARK}"' in script
    assert '"[" + mark + "]"' in script
    assert ".remove()" in script
    # Streamlit ships an apple-touch-icon of its own, which stays a
    # candidate unless it goes too -- and it has to go before ours lands.
    assert script.index("link[rel~='apple-touch-icon']") < script.index(
        "appendChild")


def test_a_sport_label_cannot_close_the_script_tag_early(icons):
    """Tag values are encoded into the script rather than pasted. JSON alone
    is not enough: it leaves `<` and `>` as themselves, and the HTML parser
    reaches a `</script>` inside a string before the JavaScript one does --
    ending the block early and running whatever followed it."""
    hostile = "AFL\" </script><script>alert(1)</script>"
    sport = type("S", (), {"key": "afl", "label": hostile,
                           "icon": "🏉"})()
    script = branding.head_script(sport)

    assert script.count("</script>") == 1, "the label closed the block early"
    # ...and the label still arrives intact on the other side
    tags = _tags(script)
    title = [t for t in tags if t.get("name") == "apple-mobile-web-app-title"]
    assert title and title[0]["content"] == hostile


# ------------------------------------------------------- the real folder

def test_the_shipped_icon_folder_holds_only_png_files_and_its_readme():
    """A stray .ico or .svg would be silently ignored by `_candidates`, and
    the reader would be left wondering why their icon never appeared."""
    directory = branding.ICON_DIR
    if not directory.is_dir():
        pytest.skip("no static/icons folder")
    unexpected = [p.name for p in directory.iterdir()
                  if p.is_file() and p.suffix.lower() not in (".png", ".md")]
    assert not unexpected, (
        f"branding.py only reads .png: {unexpected}")
