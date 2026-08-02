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
