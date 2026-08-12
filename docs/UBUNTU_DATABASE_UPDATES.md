# Ubuntu database updates

Database refreshes use the same Python pipeline whether they are started by
systemd or by hand at the command line. The Streamlit process never starts
one: it is strictly read-only and reports status only. Each sport is built in
a staging SQLite file, checked, and atomically promoted. A failed sport keeps
its current live database and its failed staging file for diagnosis. The five
most recent live-file backups are retained under each sport's
`data/.../backups` directory.

## Install the timers

Use the same Linux account that runs the application, or a dedicated service
account with read/write access to the repository's `data` and `logs`
directories. From the checked-out project:

```bash
sudo bash ./scripts/install_database_update_systemd.sh \
  --user sportslab \
  --project-dir /srv/sports_data_lab \
  --python /srv/sports_data_lab/.venv/bin/python
```

The installer adds these schedules in the `Australia/Sydney` timezone:

- regular AFL, NBA, MLB, and NFL updates at 00:10 Friday through Monday;
- a guarded Brownlow/awards timer at 01:00 Tuesday;
- a guarded Grand Final/awards timer at 01:00 Sunday;
- a Gridley board scan at 06:30 daily;
- an AFL Rising Star nomination check at 08:00 Monday.

The annual timers wake weekly, but the Python calendar guard exits without
writing unless the date is the intended post-event date. `Persistent=true`
means systemd runs a missed timer after the server returns. The shared update
lock prevents a hand-started update and a timer update from overlapping.

The Gridley and Rising Star scans have no calendar guard, because both
promote a database only when their source actually changed. Running either
on an unintended day costs a request and writes nothing, whereas a guard
would refuse the catch-up run `Persistent=true` schedules after downtime.

## Configuration

The service reads `/srv/sports_data_lab/.env` when present. Relevant optional
values are:

```dotenv
SPORTS_DATA_NBA_SOURCE=csv
SPORTS_DATA_NBA_SOURCE_ROOT=/srv/sports_data_lab/data/nba/source
SPORTS_DATA_UPDATE_STEP_TIMEOUT_SECONDS=21600
```

If `SPORTS_DATA_AFL_AWARDS_FETCH_CMD` is configured, it should only fetch or
refresh source files. Database writes belong to the provided loaders so they
target the staged database.

## Hand-entered rounds from a remote machine

The web application accepts no uploads. Copy the round summary and match
CSVs onto the server (`scp`, or a synced directory) and run the loader
there, or run the desktop window (`python -m utils.afl.load_round_gui`) on
the machine that already holds the files — hosted here it would try to open
a window on the server.

Server-side requirement: the service account needs write access to
`data/app/manual_rounds/`, the conventional home for a round's files.

A round is loaded into a staged copy and promoted only if the loader
accepts it, so a round with a problem in it cannot leave the live database
half-written. `--dry-run` runs the same validation and writes nothing. The
round name is checked against an allowlist — a round number or a finals
code (EF, QF, SF, PF, GF) — because it becomes a directory name.

## Operate and diagnose

```bash
systemctl list-timers --all 'sports-data-lab-db-*'
systemctl status sports-data-lab-db-regular.timer
journalctl -u sports-data-lab-db-update@regular.service

# Preview commands without scraping or writing databases
cd /srv/sports_data_lab
.venv/bin/python -m database_updates run --event regular --sports afl nba mlb nfl --dry-run

# Start the same guarded regular job immediately
sudo systemctl start sports-data-lab-db-update@regular.service

# Check for this week's Rising Star nomination without waiting for Monday
.venv/bin/python -m database_updates rising-star-scan

# Check a round's CSVs without writing, then load them
.venv/bin/python -m database_updates manual-round-load \
  --dir /srv/sports_data_lab/data/app/manual_rounds/2026-23 \
  --season 2026 --round 23 --dry-run

# Inspect the structured last-run result
.venv/bin/python -m database_updates status
```

The detailed subprocess log and structured `status.json` are written beneath
`logs/database_updates/`. The Admin page reads the same status file to report
progress and results, and displays these commands; it cannot start any of
them.
