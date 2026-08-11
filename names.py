"""
names.py -- One name-normalisation rule, shared by every module.

Draftguru embeds non-breaking spaces in player names ("Martin\\xa0Leslie"),
so exact comparison against the AFL Tables names silently matches nothing.
Anything comparing names must go through normalise_name().
"""

import re
import unicodedata


def normalise_name(value):
    """Casefolded, Unicode-normalised, whitespace-collapsed name key."""
    if value is None:
        return ""
    value = unicodedata.normalize("NFKC", str(value))
    value = (value.replace("\u200b", " ")   # zero-width space
                  .replace("\u00a0", " ")   # non-breaking space
                  .replace("\u2019", "'")   # curly apostrophe
                  .replace("\u2018", "'")
                  .replace("\u2013", "-")   # en dash
                  .replace("\u2014", "-"))
    value = re.sub(r"\s+", " ", value).strip()
    return value.casefold()


def search_key(value):
    """Case-, accent- and punctuation-blind key for substring search.

    The player picker treats punctuation, spacing and accents as
    interchangeable so that "acuna" finds Acuña and "o'brien" finds the
    OBrien that AFL Tables strips the apostrophe from. This is the same
    rule for SQL: registered as a connection function so a LIKE over
    ``search_key(player)`` matches by the letters alone.

    NFKD splits an accented letter from its combining mark so the mark can
    be dropped; a letter with no ASCII equivalent (an "ø" or "đ") is
    removed with the rest of the non-alphanumerics, same as any stray
    punctuation.
    """
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def like_contains(value):
    """A LIKE pattern that matches `value` anywhere in a column, literally.

    SQLite's LIKE reads ``%`` and ``_`` in the *pattern* as wildcards, so a
    search box fed straight into ``'%' + term + '%'`` quietly over-matches:
    an underscore matches any letter and a stray percent sign matches
    everything. Every query using this pattern must declare the escape
    character::

        WHERE column LIKE ? ESCAPE '\\'
    """
    text = str(value or "")
    text = (text.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_"))
    return f"%{text}%"


def name_variants(value):
    """
    Extra keys for fuzzier matching: punctuation stripped, and the
    'first initial + surname' form ("m leslie").
    """
    base = normalise_name(value)
    nopunct = re.sub(r"[^a-z0-9 ]", "", base)
    parts = nopunct.split()
    initial = f"{parts[0][0]} {' '.join(parts[1:])}" if len(parts) > 1 else ""
    return {"key": base, "nopunct": nopunct, "initial": initial}
