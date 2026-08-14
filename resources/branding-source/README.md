# Branding source art

`icon.jpeg` is the master app-icon artwork. The deployed copies live in
[static/icons/](../../static/icons/) (`default.png` and the sized Apple
touch variants), which `branding.py` serves — regenerate those from this
file when the icon changes, and see `static/icons/README.md` for the
sizes the app looks for.

The generated PNGs are deliberately not kept here a second time: this
folder held byte-identical copies of everything in `static/icons/` until
the duplicates were removed, and only the editable original stayed.
