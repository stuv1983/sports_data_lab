"""branding.py -- the tab icon, the iOS home-screen icon, and the head tags
Streamlit does not own.

`st.set_page_config(page_icon=...)` sets the favicon and nothing else. The
`apple-touch-icon` link that decides what an iPhone shows for a page saved to
the home screen lives in a `<head>` Streamlit renders itself and never
exposes, so it is written in from the browser instead (see `head_script`).

Both are read from `static/icons/`, so changing either is dropping a file in
rather than editing Python. Every file is optional and every one is a square
PNG::

    static/icons/default.png        tab icon, for any sport without its own
    static/icons/default-180.png    home-screen icon, likewise
    static/icons/default-152.png    optional additional iOS size
    static/icons/afl.png            one sport's tab icon, beating default
    static/icons/afl-180.png        one sport's home-screen icon

A sport with no file of its own falls back to `default`; a `default` that is
not there falls back to the emoji in the sport's registry entry, which is
what the app showed before this module existed. An empty `static/icons/`
therefore changes nothing.

The home-screen icon is served over HTTP rather than inlined, because iOS
has never reliably honoured a `data:` URI in an `apple-touch-icon`. That
needs `[server] enableStaticServing = true`; with it off the favicon still
works and the home-screen link is simply not written.
"""

import json
from pathlib import Path

#: Where the icons live, and the one directory a user has to know about.
ICON_DIR = Path(__file__).resolve().parent / "static" / "icons"

#: Apple devices have requested several icon sizes over time. Supplying more
#: than one lets each device use the closest asset without scaling. A 180 px
#: icon remains the preferred single-file convention.
APPLE_ICON_SIZES = (60, 76, 120, 152, 180)
APPLE_ICON_SIZE = 180

#: Marks every node this module writes into the parent document, so a rerun
#: replaces its own tags instead of stacking a new set on top of them.
#: Spelled out as an attribute rather than set through `element.dataset`,
#: which would rewrite a camelCase name to kebab-case and leave the selector
#: that cleans up matching nothing.
_MARK = "data-sdl-branding"


def _candidates(sport_key, suffix):
    """Icon filenames for one sport, most specific first."""
    return [f"{sport_key}{suffix}.png", f"default{suffix}.png"]


def _find(sport_key, suffix):
    """The first icon file that exists, or None when none of them do."""
    for name in _candidates(sport_key, suffix):
        path = ICON_DIR / name
        if path.is_file():
            return path
    return None


def favicon_file(sport_key):
    """The tab icon for one sport, or None to fall back to its emoji."""
    return _find(sport_key, "")


def apple_icon_file(sport_key):
    """The home-screen icon for one sport.

    A `-180` file is preferred and a plain one accepted, so a single square
    PNG per sport is enough to set both icons at once.
    """
    return _find(sport_key, f"-{APPLE_ICON_SIZE}") or _find(sport_key, "")


def apple_icon_files(sport_key):
    """Available size-specific home-screen icons, or the single fallback.

    Each item is ``(size, path)``. ``size`` is ``None`` for an unsized plain
    icon. This keeps the original one-file setup working while allowing a
    supplied Apple icon set to retain all of its native resolutions.
    """
    icons = []
    for size in APPLE_ICON_SIZES:
        path = _find(sport_key, f"-{size}")
        if path is not None:
            icons.append((size, path))
    if icons:
        return icons
    path = apple_icon_file(sport_key)
    return [(None, path)] if path is not None else []


def page_icon(sport):
    """What to pass to `st.set_page_config(page_icon=...)`.

    A path when there is an icon file -- Streamlit turns it into a media URL
    itself -- and the sport's emoji when there is not.
    """
    path = favicon_file(sport.key)
    return str(path) if path else sport.icon


def _static_url(path, base_url_path=""):
    """The URL Streamlit's static file server publishes `path` at.

    Absolute, because a page's own URL carries its name ("/Player_Search")
    and a relative href would be resolved against that rather than the app
    root. `?v=` is the file's modification time, so replacing an icon is
    picked up instead of being served from the browser cache forever.
    """
    base = str(base_url_path or "").strip("/")
    prefix = f"/{base}" if base else ""
    return (f"{prefix}/app/static/icons/{path.name}"
            f"?v={int(path.stat().st_mtime)}")


def head_script(sport, *, theme_color="", static_serving=True,
                base_url_path=""):
    """JavaScript that writes this sport's head tags into the parent page.

    Returns "" when there is nothing to write. The script removes its own
    previous tags first, so switching sport swaps the icon rather than
    leaving both links in the head for iOS to choose between; it also drops
    any `apple-touch-icon` Streamlit shipped, which would otherwise still be
    a candidate.
    """
    tags = []
    icons = apple_icon_files(sport.key) if static_serving else []
    for size, icon in icons:
        url = _static_url(icon, base_url_path)
        attrs = {"tag": "link", "rel": "apple-touch-icon", "href": url}
        if size is not None:
            attrs["sizes"] = f"{size}x{size}"
        tags.append(attrs)
        precomposed = dict(attrs)
        precomposed["rel"] = "apple-touch-icon-precomposed"
        tags.append(precomposed)
    tags.append({"tag": "meta", "name": "apple-mobile-web-app-capable",
                 "content": "yes"})
    tags.append({"tag": "meta", "name": "mobile-web-app-capable",
                 "content": "yes"})
    tags.append({"tag": "meta", "name": "apple-mobile-web-app-title",
                 "content": sport.label})
    if theme_color:
        tags.append({"tag": "meta", "name": "theme-color",
                     "content": theme_color})
        tags.append({"tag": "meta",
                     "name": "apple-mobile-web-app-status-bar-style",
                     "content": "black-translucent"})

    return _SCRIPT % {"mark": _MARK, "tags": _js_literal(tags)}


def _js_literal(value):
    """`value` as JSON that is safe to sit inside a `<script>` element.

    `json.dumps` escapes quotes and backslashes but leaves `<` and `>` alone,
    so a string containing `</script>` would close the block early -- the
    HTML parser gets there before the JavaScript one does. These characters
    only ever appear inside string values here, and `\\uXXXX` is the same
    string to a JSON reader, so escaping them costs nothing.
    """
    return (json.dumps(value)
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026"))


#: Written into a zero-height component iframe. Streamlit renders components
#: with `srcdoc`, so the iframe shares the app's origin and `window.parent`
#: is reachable; nothing here works -- or is needed -- outside that.
_SCRIPT = """
<script>
(function () {
  var doc = window.parent && window.parent.document;
  if (!doc || !doc.head) { return; }
  var mark = "%(mark)s";
  doc.head.querySelectorAll("[" + mark + "]").forEach(function (node) {
    node.remove();
  });
  doc.head.querySelectorAll(
    "link[rel~='apple-touch-icon'], link[rel~='apple-touch-icon-precomposed']"
  ).forEach(function (node) { node.remove(); });
  %(tags)s.forEach(function (spec) {
    var node = doc.createElement(spec.tag);
    Object.keys(spec).forEach(function (name) {
      if (name !== "tag") { node.setAttribute(name, spec[name]); }
    });
    node.setAttribute(mark, "1");
    doc.head.appendChild(node);
  });
})();
</script>
"""


def apply(st, sport, theme_color=""):
    """Write this sport's head tags, from inside a Streamlit script run.

    Draws a zero-height iframe whose container is collapsed by CSS. A hidden
    iframe still loads and runs its script in every browser this app
    supports, so nothing is visible and nothing is lost.
    """
    try:
        static_serving = bool(
            st.get_option("server.enableStaticServing"))
        base_url_path = st.get_option("server.baseUrlPath") or ""
    except Exception:
        static_serving, base_url_path = False, ""

    script = head_script(sport, theme_color=theme_color,
                         static_serving=static_serving,
                         base_url_path=base_url_path)
    if not script:
        return
    st.markdown(
        "<style>.st-key-sdl_branding{height:0;overflow:hidden;margin:0;}"
        "</style>", unsafe_allow_html=True)
    with st.container(key="sdl_branding"):
        # One pixel because `st.iframe` refuses a height of zero; the
        # container above is what actually collapses it out of the layout.
        st.iframe(script, height=1)
