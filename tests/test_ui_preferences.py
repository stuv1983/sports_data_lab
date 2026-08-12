import ui_preferences
import theme


def test_navigation_preferences_keep_recovery_pages_and_new_sections():
    preferences = ui_preferences.normalise({
        "navigation": {
            "section_order": ["Play", "Play", "Discover"],
            "hidden_pages": ["home", "profile", "awards", "unknown"],
            "show_database_status": False,
        }
    })

    assert preferences["navigation"]["section_order"] == [
        "Play", "Discover", "Explore", "Account & settings"
    ]
    assert preferences["navigation"]["hidden_pages"] == ["awards"]
    assert preferences["navigation"]["show_database_status"] is False


def test_navigation_customization_cannot_add_an_unauthorized_page():
    entries = {
        "Discover": [("home", "home-page")],
        "Explore": [("awards", "awards-page")],
        "Play": [],
        "Account & settings": [("profile", "profile-page")],
    }
    preferences = {
        "navigation": {
            "section_order": ["Explore", "Discover", "Account & settings"],
            "hidden_pages": ["awards"],
        }
    }

    pages = ui_preferences.apply_navigation(entries, preferences)

    assert pages == {
        "Discover": ["home-page"],
        "Account & settings": ["profile-page"],
    }


def test_persisted_custom_colours_cannot_inject_css():
    custom = theme.palette(
        "afl", "Custom",
        {"board": "red; } body { display:none", "accent": "#12abEF"},
    )

    assert custom["board"] == theme.palette("afl", "AFL")["board"]
    assert custom["accent"] == "#12abEF"
