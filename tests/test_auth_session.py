"""The half of a log in that lives in the browser rather than the session.

The database half is covered in test_accounts.py; what is pinned here is
the reconciliation auth_session does on every script run, because getting
it wrong is invisible in one tab and obvious in two.

The cookie writes are Javascript in a component iframe and AppTest has no
browser, so ``_run_js`` is recorded instead of run. The accounts calls are
stubbed for the same reason the real ones are tested elsewhere: what
matters here is which of them each state calls, and with what.
"""

import accounts
import auth_session
from streamlit.testing.v1 import AppTest


READER = accounts.User(7, "reader@example.com", "Reader", "member", True)


def _recorded(monkeypatch, cookie=None, tokens=("fresh-token",)):
    """Stub the browser and the database; return the list of scripts run."""
    scripts = []
    issued = list(tokens)
    monkeypatch.setattr(auth_session, "_run_js", scripts.append)
    monkeypatch.setattr(auth_session, "_cookie_token", lambda: cookie)
    monkeypatch.setattr(
        accounts, "session_user",
        lambda token: READER if token == "known-token" else None)
    monkeypatch.setattr(accounts, "create_session", lambda user_id: issued.pop(0))
    monkeypatch.setattr(accounts, "destroy_session", scripts.append)
    return scripts


RESTORE_ONLY = """
import streamlit as st
import auth_session

auth_session.restore()
st.text(f"user={st.session_state.get('auth_user_id')}")
"""


def test_a_second_tab_recovers_the_log_in_from_the_cookie(monkeypatch):
    """The reported bug: a new tab is a new session, so session_state is
    empty and the member is shown the log in form on a page they are
    already logged in for next door."""
    scripts = _recorded(monkeypatch, cookie="known-token")

    app = AppTest.from_string(RESTORE_ONLY).run()

    assert app.text[0].value == "user=7"
    assert app.session_state[auth_session.TOKEN_KEY] == "known-token"
    # The browser already holds this exact cookie. Writing it back would
    # put an iframe in the layout to tell the browser what it just said.
    assert scripts == []


def test_a_cookie_that_no_longer_means_anything_is_dropped(monkeypatch):
    """Expired, revoked, or naming a disabled account -- one answer."""
    scripts = _recorded(monkeypatch, cookie="stale-token")

    app = AppTest.from_string(RESTORE_ONLY).run()

    assert app.text[0].value == "user=None"
    assert auth_session.TOKEN_KEY not in app.session_state
    assert len(scripts) == 1
    assert "Max-Age=0" in scripts[0]


def test_no_cookie_is_simply_an_anonymous_reader(monkeypatch):
    scripts = _recorded(monkeypatch, cookie=None)

    app = AppTest.from_string(RESTORE_ONLY).run()

    assert app.text[0].value == "user=None"
    assert scripts == []


LOG_IN_THEN_OUT = """
import streamlit as st
import accounts
import auth_session

auth_session.restore()
if st.button("log in"):
    auth_session.remember(
        accounts.User(7, "reader@example.com", "Reader", "member", True))
    st.rerun()
if st.button("log out"):
    auth_session.forget()
    st.rerun()
st.text(f"user={st.session_state.get('auth_user_id')}")
"""


def test_logging_in_hands_the_token_to_the_browser_on_the_next_run(monkeypatch):
    """The write cannot happen on the run that logs in: that run ends in a
    rerun, and an iframe in a discarded run never executes."""
    scripts = _recorded(monkeypatch)

    app = AppTest.from_string(LOG_IN_THEN_OUT).run()
    app.button[0].click().run()

    assert app.text[0].value == "user=7"
    assert app.session_state[auth_session.TOKEN_KEY] == "fresh-token"
    assert len(scripts) == 1
    assert "fresh-token" in scripts[0]
    assert "SameSite=Lax" in scripts[0]

    # And not again on every rerun afterwards.
    app.run()
    assert len(scripts) == 1


def test_logging_out_retires_the_token_and_drops_the_cookie(monkeypatch):
    """A log out that left the cookie standing would log this tab out and
    the next one straight back in."""
    scripts = _recorded(monkeypatch, cookie="known-token")

    app = AppTest.from_string(LOG_IN_THEN_OUT).run()
    assert app.session_state[auth_session.TOKEN_KEY] == "known-token"

    app.button[1].click().run()

    assert app.text[0].value == "user=None"
    assert auth_session.TOKEN_KEY not in app.session_state
    # destroy_session records the token it was asked to retire; the run
    # after it records the cookie deletion.
    assert scripts[0] == "known-token"
    assert "Max-Age=0" in scripts[1]
    assert "known-token" not in scripts[1]
