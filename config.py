"""Credentials and other local settings that must not be committed.

Secrets are read from the environment first and from Streamlit's
`.streamlit/secrets.toml` second, so the same code works for a build script
run from a shell and for the app run by Streamlit. Both sources are
optional: everything here returns "" when nothing is configured, and the
caller decides whether that is fatal.

To add a key, either set it in the environment::

    setx NBA_API_KEY "..."          # Windows, new shells
    export NBA_API_KEY=...          # bash

or copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and
fill it in. That file is gitignored.
"""

import os

#: Cached secrets.toml contents. Read once; a changed file needs a restart,
#: which is also true of Streamlit's own secrets handling.
_SECRETS = None


def _secrets():
    global _SECRETS
    if _SECRETS is None:
        _SECRETS = _read_secrets()
    return _SECRETS


def _read_secrets():
    """`.streamlit/secrets.toml` as a flat dict, or {} when absent."""
    from pathlib import Path
    path = Path(__file__).resolve().parent / ".streamlit" / "secrets.toml"
    if not path.exists():
        return {}
    try:
        import tomllib
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError, ImportError):
        return {}
    return data if isinstance(data, dict) else {}


def secret(name, default=""):
    """One setting, from the environment or secrets.toml, in that order."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    found = _secrets().get(name)
    if isinstance(found, str) and found.strip():
        return found.strip()
    return default


# ------------------------------------------------------------------- NBA

#: Set to enable the authenticated NBA source. Empty means the adapter runs
#: unauthenticated, which is what NBA.com's undocumented endpoints expect;
#: a key is only needed for a provider that requires one.
NBA_API_KEY_NAME = "NBA_API_KEY"


def nba_api_key():
    """The NBA API key, or "" when none is configured."""
    return secret(NBA_API_KEY_NAME)


def nba_auth_headers(key=None):
    """Request headers carrying the NBA API key, or {} when there is none.

    The header name is a placeholder: it is what a bearer-token provider
    wants, and it is the one line to change for a provider that wants
    `x-api-key`, a query parameter, or anything else.
    """
    key = nba_api_key() if key is None else str(key).strip()
    return {"Authorization": f"Bearer {key}"} if key else {}
