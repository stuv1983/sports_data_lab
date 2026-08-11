"""
theme.py -- Appearance modes and the stylesheet they drive.

AFL gets an AFL-inspired navy, red and white scheme in addition to the
existing dark, light and custom modes. Appearance controls are intentionally
kept inside one collapsed sidebar expander so they do not compete with search,
navigation or database information.

Palette keys are stable across modes; anything added here must be added to
every palette or the stylesheet will fall back to an unset variable.
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
    # AFL-inspired application colours: deep navy, red and white. This is a
    # project theme, not a reproduction of an AFL-owned logo or artwork.
    ("afl", "AFL"): {
        "board": "#071B2B", "panel": "#0C2A42", "line": "#31506A",
        "chalk": "#F7F9FC", "muted": "#A7B9C8", "accent": "#E31837",
        "hover": "#123A57",
    },
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
    # Turf green and white chalk, which is the field rather than any team.
    ("nfl", "Dark"): {
        "board": "#0B1710", "panel": "#132318", "line": "#2A3E30",
        "chalk": "#EDF3EC", "muted": "#8CA394", "accent": "#3FA34D",
        "hover": "#1B3122",
    },
    ("nfl", "Light"): {
        "board": "#F3F7F2", "panel": "#FFFFFF", "line": "#D2DFD3",
        "chalk": "#122116", "muted": "#5A6E60", "accent": "#1F7A34",
        "hover": "#E7EFE6",
    },
}

#: Gridley colours a solved cell by how rare the answer was. The same
#: idea drives the board here, keyed to our own obscurity rating.
TILE_DEFAULTS = {
    "afl": {"tile_rare": "#005A9C", "tile_common": "#D71920"},
    "nba": {"tile_rare": "#3E2A93", "tile_common": "#C4451F"},
    "nfl": {"tile_rare": "#1B5E9B", "tile_common": "#B8341F"},
}

MODES = {
    "afl": ["AFL", "Dark", "Light", "Custom"],
    "nba": ["Dark", "Light", "Custom"],
    "nfl": ["Dark", "Light", "Custom"],
}


def modes(sport_key):
    """Return the schemes offered by one sport."""
    return list(MODES.get(sport_key, MODES["afl"]))


def default_mode(sport_key):
    """AFL uses its branded scheme; other sports retain their dark default."""
    return "AFL" if sport_key == "afl" else "Dark"


def palette(sport_key, mode, custom=None):
    """Resolve a complete palette for a sport and appearance mode."""
    fallback_mode = default_mode(sport_key)
    requested = mode if mode != "Custom" else fallback_mode
    base = PALETTES.get(
        (sport_key, requested),
        PALETTES.get((sport_key, fallback_mode), PALETTES[("afl", "AFL")]),
    )
    tiles = TILE_DEFAULTS.get(sport_key, TILE_DEFAULTS["afl"])
    base = {**tiles, **base}
    if mode != "Custom":
        return dict(base)
    merged = dict(base)
    merged.update({k: v for k, v in (custom or {}).items() if v})
    return merged


def controls(st, sport_key, key_prefix=""):
    """Render a compact, collapsed Appearance section and return its palette."""
    k = lambda name: f"{key_prefix}theme_{name}"  # noqa: E731
    offered = modes(sport_key)
    state_key = k("mode")

    # Old sessions may contain a mode no longer valid for the selected sport.
    if st.session_state.get(state_key) not in (None, *offered):
        st.session_state.pop(state_key, None)

    with st.sidebar.expander("Appearance", expanded=False):
        mode = st.radio(
            "Colour scheme",
            offered,
            index=offered.index(default_mode(sport_key)),
            key=state_key,
            horizontal=True,
            label_visibility="collapsed",
        )

        if mode != "Custom":
            resolved = palette(sport_key, mode)
            st.session_state[k("seed")] = resolved
            return resolved

        seed = st.session_state.get(k("seed")) or palette(
            sport_key, default_mode(sport_key)
        )
        custom = {}
        left, right = st.columns(2)
        for i, name in enumerate(KEYS):
            col = left if i % 2 == 0 else right
            custom[name] = col.color_picker(
                LABELS[name], seed.get(name, "#000000"), key=k(name)
            )

        reset_label = "Reset to AFL" if sport_key == "afl" else "Reset to dark"
        if st.button(reset_label, key=k("reset")):
            for name in KEYS:
                st.session_state.pop(k(name), None)
            st.session_state[k("seed")] = palette(
                sport_key, default_mode(sport_key)
            )
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
h2 {{ font-weight: 700 !important; font-size: 1.55rem !important; }}
h3 {{ font-weight: 500 !important; font-size: 1.25rem !important; }}

body, p, div, label, span {{ color: var(--chalk); }}

/* Form controls.
   `body, div, span` above sets the text colour of everything, including
   the inside of a selectbox -- but the selectbox's own background comes
   from Streamlit's base theme, not from this stylesheet. When the two
   disagree the control renders chalk-on-white and is effectively
   invisible: the Club Explorer's club picker and every filter below it.
   .streamlit/config.toml fixes the default; these rules keep the controls
   bound to the live palette, so Light and Custom modes stay legible too.
   Targeting BaseWeb's containers rather than Streamlit's own class names,
   because the latter change between releases. */
div[data-baseweb="select"] > div,
div[data-baseweb="input"] > div,
div[data-baseweb="textarea"] > div,
div[data-baseweb="popover"] div[role="listbox"] {{
  background: var(--panel) !important;
  border-color: var(--line) !important;
  color: var(--chalk) !important;
}}
div[data-baseweb="select"] *,
div[data-baseweb="input"] input,
div[data-baseweb="textarea"] textarea {{
  color: var(--chalk) !important;
  -webkit-text-fill-color: var(--chalk) !important;
}}
div[data-baseweb="popover"] li[role="option"]:hover,
div[data-baseweb="popover"] li[aria-selected="true"] {{
  background: var(--hover) !important;
}}
div[data-baseweb="select"] svg {{ fill: var(--muted) !important; }}

/* Download and form buttons outside the grid, which the square-specific
   rule further down does not reach. */
.stDownloadButton > button,
div[data-testid="stForm"] .stButton > button {{
  background: var(--panel) !important;
  border: 1px solid var(--line) !important;
  color: var(--chalk) !important;
}}
.stDownloadButton > button:hover,
div[data-testid="stForm"] .stButton > button:hover {{
  border-color: var(--amber) !important;
}}

/* Tabs and expanders: the label inherits --chalk, but the surface behind
   it does not, so an expander header reads as a white bar on a dark page. */
div[data-testid="stExpander"] details {{
  background: var(--panel);
  border: 1px solid var(--line);
}}
div[data-testid="stExpander"] summary {{ color: var(--chalk) !important; }}
button[data-baseweb="tab"] {{ color: var(--muted) !important; }}
button[data-baseweb="tab"][aria-selected="true"] {{
  color: var(--chalk) !important;
}}

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

/* Player profile, styled as a vintage trading card back: a themed banner,
   a bio column beside a capped-height season table, and a career-totals
   footer strip. */
.card-banner {{
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: .75rem;
}}
.card-banner-name {{
  font-family: 'Oswald', sans-serif;
  font-weight: 700;
  font-size: 1.8rem;
  letter-spacing: .03em;
  text-transform: uppercase;
  color: var(--chalk);
  line-height: 1.1;
}}
.card-banner-stars {{ padding-top: .3rem; white-space: nowrap; }}
.card-banner-logos {{
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: .45rem;
  min-height: 44px;
  margin-top: .55rem;
}}
.card-banner-logos img {{
  object-fit: contain;
  filter: drop-shadow(0 2px 3px rgba(0,0,0,.2));
}}
.card-banner-sub {{
  font-family: 'IBM Plex Mono', monospace;
  font-size: .74rem;
  color: var(--muted);
  letter-spacing: .03em;
  margin: .2rem 0 .9rem 0;
  padding-bottom: .7rem;
  border-bottom: 2px solid var(--amber);
}}
.card-section-label {{
  font-family: 'IBM Plex Mono', monospace;
  font-size: .64rem;
  letter-spacing: .12em;
  text-transform: uppercase;
  color: var(--muted);
  margin-bottom: .5rem;
}}
.bio-row {{
  display: flex;
  justify-content: space-between;
  font-family: 'IBM Plex Mono', monospace;
  font-size: .78rem;
  padding: .3rem 0;
  border-bottom: 1px dotted var(--line);
}}
.bio-label {{ color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }}
.bio-value {{ color: var(--chalk); }}
.card-footer-divider {{
  border-top: 1px solid var(--line);
  margin: 1.1rem 0 .8rem 0;
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

/* Match scorecard. The two sides sit either side of the score so the
   reader sees who played whom and by how much in one glance, which is the
   first thing anyone asks of a result. Sized in clamp() rather than fixed
   points because this renders inside a dialog on a phone as often as on a
   desktop. */
.scoreboard {{
  display: flex;
  align-items: center;
  justify-content: center;
  gap: clamp(.5rem, 3vw, 1.75rem);
  flex-wrap: nowrap;
  background: var(--panel);
  border: 1px solid var(--line);
  border-top: 3px solid var(--amber);
  padding: .9rem .6rem 1rem .6rem;
  margin-bottom: .9rem;
}}
.score-side {{
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: .4rem;
  flex: 1 1 0;
  min-width: 0;
}}
.score-side img {{ object-fit: contain; }}
.score-club {{
  font-family: 'Oswald', sans-serif;
  font-weight: 500;
  font-size: clamp(.7rem, 2.6vw, .95rem);
  letter-spacing: .05em;
  text-transform: uppercase;
  color: var(--chalk);
  text-align: center;
  line-height: 1.15;
}}
.score-role {{
  font-family: 'IBM Plex Mono', monospace;
  font-size: .58rem;
  letter-spacing: .12em;
  text-transform: uppercase;
  color: var(--muted);
}}
.score-value {{
  font-family: 'Oswald', sans-serif;
  font-size: clamp(2rem, 9vw, 3.4rem);
  font-weight: 700;
  line-height: 1;
  color: var(--muted);
}}
.score-value.is-winner {{ color: var(--amber); }}
.score-detail {{
  font-family: 'IBM Plex Mono', monospace;
  font-size: .66rem;
  color: var(--muted);
  text-align: center;
  margin-top: .2rem;
}}
.score-verdict {{
  font-family: 'IBM Plex Mono', monospace;
  font-size: .68rem;
  letter-spacing: .06em;
  text-transform: uppercase;
  color: var(--muted);
  text-align: center;
  margin: -.5rem 0 1rem 0;
}}
.score-verdict b {{ color: var(--chalk); font-weight: 600; }}

/* One statistic, both sides, as a mirrored bar. Reads as "who had more of
   this", which a pair of numbers in a table does not. */
.statbar {{
  display: flex;
  align-items: center;
  gap: .5rem;
  font-family: 'IBM Plex Mono', monospace;
  font-size: .74rem;
  padding: .18rem 0;
}}
.statbar-name {{
  flex: 0 1 7.5rem;
  color: var(--muted);
  text-transform: uppercase;
  font-size: .6rem;
  letter-spacing: .06em;
  text-align: center;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}}
.statbar-num {{ flex: 0 0 2.8rem; color: var(--chalk); }}
.statbar-num.left {{ text-align: right; }}
.statbar-num.right {{ text-align: left; }}
.statbar-track {{
  flex: 1 1 0;
  height: .5rem;
  background: var(--line);
  min-width: 0;
  display: flex;
}}
.statbar-track.left {{ justify-content: flex-end; }}
.statbar-fill {{ height: 100%; background: var(--amber); opacity: .55; }}
.statbar-fill.is-more {{ opacity: 1; }}

/* Mobile layout fixes to prevent st.columns from wrapping to a vertical
   stack. .st-key-grid_board is the class Streamlit puts on the keyed
   container in 15_Play_Grids.py, so this really does wrap the board. */
.st-key-grid_board [data-testid="stHorizontalBlock"] {{
  flex-wrap: nowrap !important;
  overflow-x: auto;
}}
.st-key-grid_board [data-testid="column"] {{
  min-width: 0 !important;
}}
</style>
"""
