# Ubuntu database updates

Database refreshes use the same Python pipeline whether they are started by
an administrator in Streamlit or by systemd. Each sport is built in a staging
SQLite file, checked, and atomically promoted. A failed sport keeps its current
live database and its failed staging file for diagnosis. The five most recent
live-file backups are retained under each sport's `data/.../backups` directory.

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
lock prevents an Admin-triggered update and a timer update from overlapping.

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

The Admin page's **Hand-entered round results** section takes the round
summary and match CSVs through the browser's own file picker, so an
administrator on a Windows PC selects them in Explorer and they upload to
this server. The desktop window (`python -m utils.afl.load_round_gui`) is
for running the loader *on* the machine holding the files; hosted here it
would try to open a window on the server.

Two server-side requirements:

- the service account needs write access to `data/app/manual_rounds/`,
  where uploads are staged for the detached load to read;
- behind a reverse proxy, allow a request body of at least a few megabytes.
  A round is nine match files of roughly 40 KB plus a summary — well under
  Streamlit's own 200 MB default, but nginx's `client_max_body_size`
  defaults to 1 MB, and an upload refused there fails in the browser
  without reaching the application log.

A round is loaded into a staged copy and promoted only if the loader
accepts it, so a round with a problem in it cannot leave the live database
half-written. **Check this round** runs the same validation and writes
nothing.

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
`logs/database_updates/`. The Admin page reads the same status file and offers
a manual trigger protected by a fresh admin-password check.
