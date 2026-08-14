"""A log in that survives opening the app in a second tab.

``st.session_state`` is per browser *session*, and every new tab opens a new
one. An app that keeps ``auth_user_id`` there alone therefore shows a member
who middle-clicks a link the log in form again, on a page they are still
logged in for in the tab beside it -- and, because the protected pages were
built out of the anonymous catalogue, a "page cannot be found" as well.

So the durable half of the log in lives in a cookie: the browser sends it
with every new session, that new tab included, and it names a row in
``accounts.user_sessions``.

Streamlit can read cookies -- ``st.context.cookies``, taken from the request
that opened the session -- but has no way to set one: nothing it sends over
the websocket becomes a ``Set-Cookie`` the browser will keep for the page.
Writing therefore goes through a collapsed iframe running one line of
Javascript against the parent document, the same trick (and the same
container) ``branding.apply`` uses to reach ``<head>``. That iframe shares
the app's origin, which is what makes ``window.parent`` reachable, and is
also why this cookie cannot be ``HttpOnly``: a cookie script can write,
script can read. What that buys an attacker is bounded on purpose -- the
token is 32 random bytes, it expires (``SESSION_TTL``), it is retired the
moment anyone logs out, and the server keeps only its digest.
"""

from __future__ import annotations

import json
import sqlite3

import streamlit as st

import accounts


COOKIE_NAME = "sdl_session"

#: This tab's copy of the cookie. Held separately from ``auth_user_id`` so
#: logging out can retire the right row without a second database lookup.
TOKEN_KEY = "auth_token"

#: The token the browser has already been told to store. Writing is a
#: side effect with a visible cost -- an iframe in the layout for one script
#: run -- so it happens on log in and log out, not on every rerun.
_SYNCED_KEY = "_auth_cookie_synced"
_CLEAR_KEY = "_auth_cookie_clear"


def _cookie_token():
    """The login token this browser sent, if any."""
    value = st.context.cookies.get(COOKIE_NAME)
    return value or None


def _run_js(statement):
    """Run one statement against the app's own document, invisibly.

    ``st.markdown(unsafe_allow_html=True)`` cannot do this -- Streamlit
    strips ``<script>`` out of user HTML -- so an iframe it is, collapsed
    the way branding.apply collapses its own: st.iframe refuses a height
    of zero, and the keyed container is what takes it out of the layout.

    The statement is handed ``d`` and ``loc`` already resolved to the
    parent page's document and location, falling back to the iframe's own
    if a browser refuses the cross-frame reach. Both are the app's origin,
    so the cookie lands on the same host either way.
    """
    st.markdown(
        "<style>.st-key-sdl_auth_cookie{height:0;overflow:hidden;margin:0;}"
        "</style>", unsafe_allow_html=True)
    with st.container(key="sdl_auth_cookie"):
        st.iframe(
            "<script>(function(){"
            "var d=document,loc=window.location;"
            "try{d=window.parent.document;loc=window.parent.location;}"
            "catch(e){}"
            f"{statement}"
            "})();</script>",
            height=1,
        )


def _attributes():
    """Path, lifetime, and the flag that keeps the token from wandering.

    ``SameSite=Lax`` stops the cookie riding along with a cross-site POST
    while still arriving on a link the reader followed -- which is the case
    that matters here, opening the app in a new tab being exactly that.
    ``Secure`` is added by the script when the page is served over https;
    hard-coding it would break every local run on http://localhost.
    """
    max_age = int(accounts.SESSION_TTL.total_seconds())
    return f"; Path=/; Max-Age={max_age}; SameSite=Lax"


def _js_literal(value):
    """``value`` as a Javascript string that cannot escape its script tag.

    Only server-issued tokens are written today, so this is belt and
    braces -- but it is the cheap kind: `json.dumps` leaves `<` alone, and
    the HTML parser reaches a `</script>` inside a string before the
    Javascript one does. Same reasoning, and same escapes, as
    ``branding._js_literal``.
    """
    return (json.dumps(str(value))
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026"))


def _write_cookie(token):
    _run_js(
        f"d.cookie={_js_literal(COOKIE_NAME + '=')}+{_js_literal(token)}"
        f"+{_js_literal(_attributes())}"
        "+(loc.protocol==='https:'?'; Secure':'');"
    )


def _clear_cookie():
    _run_js(
        "d.cookie="
        f"{_js_literal(COOKIE_NAME + '=; Path=/; Max-Age=0; SameSite=Lax')};"
    )


def remember(user):
    """Log ``user`` in on this tab, and on every tab this browser opens.

    Called with an authenticated user; issues the durable token and lets
    the next script run put it in the browser.
    """
    st.session_state["auth_user_id"] = user.id
    try:
        st.session_state[TOKEN_KEY] = accounts.create_session(user.id)
    except (OSError, sqlite3.Error):
        # A cookie is a convenience; failing to issue one must not cost
        # the reader the log in they just completed successfully.
        st.session_state.pop(TOKEN_KEY, None)
    st.session_state.pop(_SYNCED_KEY, None)


def forget():
    """Log out of every tab: retire the token, then drop the cookie.

    The cookie is dropped by the *next* script run -- this one is about to
    be discarded by the ``st.rerun`` the caller makes, and an iframe in a
    discarded run never executes.
    """
    token = st.session_state.pop(TOKEN_KEY, None)
    try:
        accounts.destroy_session(token)
    except (OSError, sqlite3.Error):
        pass
    st.session_state.pop("auth_user_id", None)
    st.session_state.pop(_SYNCED_KEY, None)
    st.session_state[_CLEAR_KEY] = True


def restore():
    """Reconcile this tab with the browser's cookie. Call once, early.

    Four states, in order: a log out waiting to be written out; a tab that
    already knows who it is; a fresh tab holding a good cookie; a fresh tab
    holding a cookie that no longer means anything.
    """
    if st.session_state.pop(_CLEAR_KEY, False):
        _clear_cookie()
        return

    token = st.session_state.get(TOKEN_KEY)
    if token is None and st.session_state.get("auth_user_id") is None:
        token = _cookie_token()
        if token:
            try:
                user = accounts.session_user(token)
            except (OSError, sqlite3.Error):
                return
            if user is None:
                # Expired, revoked, or an account since disabled. The
                # browser is told to stop presenting it.
                _clear_cookie()
                return
            st.session_state["auth_user_id"] = user.id
            st.session_state[TOKEN_KEY] = token
            # The browser already holds this exact cookie: adopting it is
            # not a reason to write it back.
            st.session_state[_SYNCED_KEY] = token

    token = st.session_state.get(TOKEN_KEY)
    if token and st.session_state.get(_SYNCED_KEY) != token:
        _write_cookie(token)
        st.session_state[_SYNCED_KEY] = token
