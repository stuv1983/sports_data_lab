"""
theme.py -- Appearance modes and the stylesheet they drive.

The existing stylesheet was already written entirely against CSS custom
properties, so switching themes only means emitting a different `:root`
block. Three modes:

    Dark    -- the sport's own dark palette (chalkboard green, hardwood)
    Light   -- a legible daylight palette
    Custom  -- seven colour pickers, seeded from whichever mode was last
               active so the user starts from something that works

Palette keys are stable across modes; anything added here must be added
to every palette or the stylesheet will fall back to an unset variable.
"""

KEYS = ["board", "panel", "line", "chalk", "muted", "accent", "hover",
        "tile_rare", "tile_common"]

LABELS = {
    "board": "Background",
    "panel": "Panels",
    "line": "Borders",
    "chalk": "Text",
    "muted": "Muted text",
    "accent": "Accent",
    "hover": "Hover",
    "tile_rare": "Rare tile",
    "tile_common": "Common tile",
}

PALETTES = {
    ("afl", "Dark"): {
        "board": "#0E1A16", "panel": "#16241F", "line": "#2C3F37",
        "chalk": "#E8E4D6", "muted": "#7C8F87", "accent": "#E8A33D",
        "hover": "#1D3129",
    },
    ("afl", "Light"): {
        "board": "#F4F2E9", "panel": "#FFFFFF", "line": "#D5D1C2",
        "chalk": "#1B2620", "muted": "#5F6E66", "accent": "#B26A00",
        "hover": "#EDE9DA",
    },
    ("nba", "Dark"): {
        "board": "#14100C", "panel": "#211A13", "line": "#3B2E22",
        "chalk": "#F0E7DA", "muted": "#96866F", "accent": "#E86A2C",
        "hover": "#2C2218",
    },
    ("nba", "Light"): {
        "board": "#FAF5EE", "panel": "#FFFFFF", "line": "#DFD3C2",
        "chalk": "#21180F", "muted": "#6B5B47", "accent": "#C24E12",
        "hover": "#F2E9DC",
    },
}

#: Gridley colours a solved cell by how rare the answer was. The same
#: idea drives the board here, keyed to our own obscurity rating.
TILE_DEFAULTS = {
    "afl": {"tile_rare": "#4C2FB0", "tile_common": "#C42794"},
    "nba": {"tile_rare": "#3E2A93", "tile_common": "#C4451F"},
}

MODES = ["Dark", "Light", "Custom"]


def palette(sport_key, mode, custom=None):
    """Resolve a palette. Custom falls back to Dark for any missing key."""
    base = PALETTES.get((sport_key, mode if mode != "Custom" else "Dark"),
                        PALETTES[("afl", "Dark")])
    tiles = TILE_DEFAULTS.get(sport_key, TILE_DEFAULTS["afl"])
    base = {**tiles, **base}
    if mode != "Custom":
        return dict(base)
    merged = dict(base)
    merged.update({k: v for k, v in (custom or {}).items() if v})
    return merged


def controls(st, sport_key, key_prefix=""):
    """
    Render the appearance section in the sidebar and return the palette.

    Colour pickers only appear in Custom mode, seeded from the palette
    that was on screen a moment ago, so switching to Custom never drops
    the user onto a blank or jarring board.
    """
    k = lambda name: f"{key_prefix}theme_{name}"  # noqa: E731

    mode = st.sidebar.radio("Appearance", MODES, key=k("mode"),
                            horizontal=True)

    if mode != "Custom":
        resolved = palette(sport_key, mode)
        st.session_state[k("seed")] = resolved
        return resolved

    seed = st.session_state.get(k("seed")) or palette(sport_key, "Dark")
    custom = {}
    with st.sidebar.expander("Custom colours", expanded=True):
        left, right = st.columns(2)
        for i, name in enumerate(KEYS):
            col = left if i % 2 == 0 else right
            custom[name] = col.color_picker(
                LABELS[name], seed.get(name, "#000000"), key=k(name))
        if st.button("Reset to dark", key=k("reset")):
            for name in KEYS:
                st.session_state.pop(k(name), None)
            st.session_state[k("seed")] = palette(sport_key, "Dark")
            st.rerun()
    return palette(sport_key, "Custom", custom)


def css(p):
    """The full stylesheet for a resolved palette."""
    return f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Oswald:wght@300;500;700&family=IBM+Plex+Mono:wght@400;600&display=swap');

:root {{
  --board:  {p['board']};
  --panel:  {p['panel']};
  --line:   {p['line']};
  --chalk:  {p['chalk']};
  --muted:  {p['muted']};
  --amber:  {p['accent']};
  --hover:  {p['hover']};
  --tile-rare:   {p['tile_rare']};
  --tile-common: {p['tile_common']};
}}

.stApp {{ background: var(--board); }}
section[data-testid="stSidebar"] {{ background: var(--panel); }}

h1, h2, h3, .board-label {{
  font-family: 'Oswald', sans-serif !important;
  letter-spacing: .04em;
  text-transform: uppercase;
  color: var(--chalk) !important;
}}
h1 {{ font-weight: 700 !important; font-size: 2.1rem !important; }}

body, p, div, label, span {{ color: var(--chalk); }}

/* Square face: a Gridley-style gradient tile. The hue carries meaning --
   indigo for a rare best answer, magenta for a common one -- so the board
   reads at a glance without anyone parsing the star row. */
.square {{
  background:
    radial-gradient(circle at 22% 18%, rgba(255,255,255,.16) 0 1px, transparent 1px),
    radial-gradient(circle at 68% 34%, rgba(255,255,255,.11) 0 1px, transparent 1px),
    radial-gradient(circle at 41% 77%, rgba(255,255,255,.13) 0 1px, transparent 1px),
    radial-gradient(circle at 84% 66%, rgba(255,255,255,.09) 0 1px, transparent 1px),
    linear-gradient(150deg,
      color-mix(in srgb, var(--tile-common) 82%, black) 0%,
      var(--tile-common) 55%,
      color-mix(in srgb, var(--tile-common) 72%, #ff7ad9) 100%);
  border: 1px solid color-mix(in srgb, var(--tile-common) 60%, black);
  border-radius: 2px;
  padding: .6rem .7rem .5rem .7rem;
  min-height: 104px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
}}
.square.tile-rare {{
  background:
    radial-gradient(circle at 22% 18%, rgba(255,255,255,.18) 0 1px, transparent 1px),
    radial-gradient(circle at 68% 34%, rgba(255,255,255,.12) 0 1px, transparent 1px),
    radial-gradient(circle at 41% 77%, rgba(255,255,255,.14) 0 1px, transparent 1px),
    radial-gradient(circle at 84% 66%, rgba(255,255,255,.10) 0 1px, transparent 1px),
    linear-gradient(150deg,
      color-mix(in srgb, var(--tile-rare) 74%, black) 0%,
      var(--tile-rare) 58%,
      color-mix(in srgb, var(--tile-rare) 70%, #7f6bff) 100%);
  border-color: color-mix(in srgb, var(--tile-rare) 55%, black);
}}
.square.is-open {{
  border-color: var(--amber);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--amber) 55%, transparent);
}}
.square.is-empty {{
  background: var(--panel);
  border: 1px solid var(--line);
  opacity: .55;
}}
.square-name {{
  font-family: 'Oswald', sans-serif;
  text-transform: uppercase;
  letter-spacing: .03em;
  font-size: .95rem;
  line-height: 1.1;
  color: #FFFFFF;
  text-shadow: 0 1px 2px rgba(0,0,0,.45);
}}
.square.is-empty .square-name {{ color: var(--chalk); text-shadow: none; }}
.square-meta {{
  font-family: 'IBM Plex Mono', monospace;
  font-size: .66rem;
  color: rgba(255,255,255,.78);
  letter-spacing: .04em;
  text-transform: uppercase;
}}
.square.is-empty .square-meta {{ color: var(--muted); }}
.square .stars-back {{ color: rgba(255,255,255,.30); }}
.square .stars-fore {{ color: #FFD87A; }}
.square .stars-num  {{ color: rgba(255,255,255,.78); }}

/* The open button sits under each square face. */
div[data-testid="column"] .stButton > button {{
  width: 100%;
  background: transparent;
  border: 1px solid var(--line);
  border-top: none;
  border-radius: 0 0 2px 2px;
  color: var(--muted);
  font-family: 'IBM Plex Mono', monospace;
  font-size: .66rem;
  letter-spacing: .08em;
  text-transform: uppercase;
  padding: .25rem 0;
  transition: background .12s, border-color .12s, color .12s;
}}
div[data-testid="column"] .stButton > button:hover {{
  background: var(--hover);
  border-color: var(--amber);
  color: var(--chalk);
}}
div[data-testid="column"] .stButton > button:focus:not(:active) {{
  border-color: var(--amber);
  color: var(--amber);
}}

/* Star ratings. A clipped filled row over a hollow row, so half stars
   render exactly without depending on a half-star glyph in the font. */
.stars {{
  position: relative;
  display: inline-block;
  font-size: .95rem;
  line-height: 1;
  letter-spacing: .06em;
  vertical-align: -1px;
}}
.stars-back {{ color: var(--line); }}
.stars-fore {{
  position: absolute;
  left: 0; top: 0;
  overflow: hidden;
  white-space: nowrap;
  color: var(--amber);
}}
.stars-num {{
  font-family: 'IBM Plex Mono', monospace;
  font-size: .68rem;
  color: var(--muted);
  margin-left: .4rem;
}}
.stars-none {{ color: var(--muted); }}

/* Summary cards shown for an opened square. */
.card {{
  background: var(--panel);
  border: 1px solid var(--line);
  border-left: 4px solid var(--amber);
  padding: .85rem 1rem;
  height: 100%;
}}
.card-label {{
  font-family: 'IBM Plex Mono', monospace;
  font-size: .64rem;
  letter-spacing: .12em;
  text-transform: uppercase;
  color: var(--muted);
  margin-bottom: .35rem;
}}
.card-value {{
  font-family: 'Oswald', sans-serif;
  font-size: 1.5rem;
  font-weight: 700;
  letter-spacing: .03em;
  text-transform: uppercase;
  color: var(--chalk);
  line-height: 1.05;
}}
.card-sub {{
  font-family: 'IBM Plex Mono', monospace;
  font-size: .68rem;
  color: var(--muted);
  margin-top: .3rem;
}}

.axis {{
  font-family: 'Oswald', sans-serif;
  text-transform: uppercase;
  font-size: .82rem;
  letter-spacing: .05em;
  color: var(--chalk);
  line-height: 1.25;
  padding: 4px 0;
}}
.axis .sub {{ color: var(--muted); font-size: .72rem; }}

div[data-testid="stDataFrame"] {{ font-family: 'IBM Plex Mono', monospace; }}

.count {{
  font-family: 'Oswald', sans-serif;
  font-size: 2.6rem;
  font-weight: 700;
  color: var(--amber);
  line-height: 1;
}}
.count-label {{
  font-family: 'IBM Plex Mono', monospace;
  font-size: .7rem;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: .1em;
}}
hr {{ border-color: var(--line); }}

.brand {{
  font-family: 'Oswald', sans-serif;
  font-size: 1.55rem;
  font-weight: 700;
  letter-spacing: .06em;
  color: var(--chalk);
  text-transform: uppercase;
  line-height: 1.05;
  margin: .2rem 0 .15rem 0;
}}
.brand-sub {{
  font-family: 'IBM Plex Mono', monospace;
  color: var(--muted);
  font-size: .68rem;
  letter-spacing: .04em;
  margin-bottom: 1rem;
}}
.hero {{
  background: linear-gradient(135deg, var(--panel) 0%, var(--board) 100%);
  border: 1px solid var(--line);
  border-left: 5px solid var(--amber);
  padding: 1.4rem 1.6rem;
  margin: .25rem 0 1.3rem 0;
}}
.hero-title {{
  font-family: 'Oswald', sans-serif;
  font-size: 2.5rem;
  font-weight: 700;
  letter-spacing: .045em;
  text-transform: uppercase;
  color: var(--chalk);
  line-height: 1;
}}
.hero-copy {{
  color: var(--muted);
  margin-top: .55rem;
  max-width: 850px;
  font-size: .96rem;
}}
.feature-card {{
  min-height: 142px;
  background: var(--panel);
  border: 1px solid var(--line);
  padding: 1rem 1.05rem;
  margin-bottom: .8rem;
}}
.feature-card h4 {{
  font-family: 'Oswald', sans-serif;
  text-transform: uppercase;
  letter-spacing: .04em;
  color: var(--chalk);
  margin: 0 0 .45rem 0;
}}
.feature-card p {{ color: var(--muted); font-size: .84rem; margin: 0; }}
.status-row {{
  font-family: 'IBM Plex Mono', monospace;
  color: var(--muted);
  font-size: .69rem;
  line-height: 1.55;
}}
.status-row b {{ color: var(--chalk); font-weight: 600; }}
.footnote {{
  font-family: 'IBM Plex Mono', monospace;
  font-size: .64rem;
  color: var(--muted);
  letter-spacing: .03em;
}}
</style>
"""
