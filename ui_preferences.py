"""Validated member preferences for navigation and appearance."""

from __future__ import annotations

from copy import deepcopy


SECTION_ORDER = ("Discover", "Explore", "Play", "Account & settings")
PINNED_PAGES = frozenset({"home", "profile"})

PAGE_LABELS = {
    "home": "Home",
    "random_discovery": "Random discovery",
    "player_search": "Player search",
    "advanced_search": "Advanced search",
    "stats_explorer": "Stats explorer",
    "visual_explorer": "Visual explorer",
    "club_explorer": "Club explorer",
    "ground_explorer": "Ground explorer",
    "past_games": "Past games",
    "awards": "Awards",
    "draft": "Draft",
    "grid_solver": "Grid solver",
    "play_grids": "Play grids",
    "leaderboards": "Leaderboards",
    "game_lab": "Game lab",
    "profile": "My profile",
    "account": "Account",
    "database_health": "Database health",
    "admin": "Admin",
}


def defaults() -> dict:
    return {
        "navigation": {
            "section_order": list(SECTION_ORDER),
            "hidden_pages": [],
            "show_database_status": True,
        },
        "appearance": {},
    }


def normalise(raw) -> dict:
    """Return a complete, safe preference document.

    Unknown entries are ignored.  Pinned pages cannot be hidden, and missing
    sections are appended so a preference written by an older app version
    cannot make a new navigation section disappear.
    """
    out = defaults()
    if not isinstance(raw, dict):
        return out

    navigation = raw.get("navigation")
    if isinstance(navigation, dict):
        requested = navigation.get("section_order", [])
        if isinstance(requested, list):
            order = [item for item in requested if item in SECTION_ORDER]
            order = list(dict.fromkeys(order))
            out["navigation"]["section_order"] = order + [
                item for item in SECTION_ORDER if item not in order
            ]

        hidden = navigation.get("hidden_pages", [])
        if isinstance(hidden, list):
            out["navigation"]["hidden_pages"] = sorted({
                str(item) for item in hidden
                if item in PAGE_LABELS and item not in PINNED_PAGES
            })
        out["navigation"]["show_database_status"] = bool(
            navigation.get("show_database_status", True)
        )

    appearance = raw.get("appearance")
    if isinstance(appearance, dict):
        out["appearance"] = deepcopy(appearance)
    return out


def apply_navigation(entries, preferences):
    """Filter and reorder already-authorized page entries.

    ``entries`` maps section labels to ``(page_id, st.Page)`` pairs.  Access
    control must happen before this function; it only customizes presentation.
    """
    prefs = normalise(preferences)["navigation"]
    hidden = set(prefs["hidden_pages"])
    pages = {}
    for section in prefs["section_order"]:
        visible = [page for page_id, page in entries.get(section, [])
                   if page_id not in hidden or page_id in PINNED_PAGES]
        if visible:
            pages[section] = visible
    return pages
