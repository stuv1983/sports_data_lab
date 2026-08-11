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

# Inspect the structured last-run result
.venv/bin/python -m database_updates status
```

The detailed subprocess log and structured `status.json` are written beneath
`logs/database_updates/`. The Admin page reads the same status file and offers
a manual trigger protected by a fresh admin-password check.
