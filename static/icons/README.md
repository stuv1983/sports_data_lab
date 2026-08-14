# App icons

Drop PNG files in this folder to change the browser tab icon and the icon iOS
shows when the app is saved to a home screen. Nothing here is required: with
the folder empty the app falls back to the emoji each sport carries in its
registry entry, which is what it used before these files existed.

`branding.py` reads them; no code change is needed to swap an icon. The
repository currently ships the default favicon and the supplied 60, 76, 120,
and 152 px Apple touch icons.

## Filenames

| File | What it sets |
| --- | --- |
| `default.png` | Tab icon (favicon) for every sport |
| `default-180.png` | Home-screen icon for every sport |
| `default-60.png`, `default-76.png`, `default-120.png`, `default-152.png` | Additional Apple touch sizes |
| `afl.png`, `nba.png`, `mlb.png`, `nfl.png` | That one sport's tab icon |
| `afl-180.png`, `nba-180.png`, … | That one sport's home-screen icon |

A sport's own file wins over `default`. When size-specific Apple files are
present, each is added to the page with its exact size. If none are present,
the `-180` file or plain file is used for the home screen, so a **single
square PNG per sport is enough**.

## Sizes

- Home-screen icon: **180 × 180**, square, no transparency, no rounded
  corners — iOS applies its own mask and a transparent background turns
  black behind it.
- Tab icon: any square size from 32 × 32 up. 180 × 180 is fine for both.

## After changing a file

Icons are served with a cache-busting `?v=` stamped from the file's
modification time, so a replaced file is picked up on the next page load.
Re-adding the app to an iOS home screen is still needed to change an icon
already sitting there — iOS keeps the icon it captured when the shortcut was
made.

Serving these over HTTP needs `[server] enableStaticServing = true` in
`.streamlit/config.toml`, which is already set. With it off the tab icon
still works and the home-screen icon is skipped.
