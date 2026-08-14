#!/usr/bin/env bash
# Copy only the files that changed since the last deploy to the server,
# preserving directory structure, over a plain ssh connection (no rsync
# required -- this only needs ssh/scp/tar, which Git Bash on Windows already
# has).
#
# This replaces the old deploy.ps1, which every time: killed the tmux
# session, `rm -rf`'d the whole remote project directory, and `scp -r`'d the
# entire local checkout back -- including the multi-gigabyte data/*.db files,
# which is why it was slow. Those databases are gitignored and are now kept
# current on the server by its own systemd update timers (see
# docs/UBUNTU_DATABASE_UPDATES.md), so this script never touches them: it
# only ever ships git-tracked files, and only the ones that actually changed.
#
# Usage:
#   ./scripts/deploy_changed_files.sh                # deploy HEAD
#   ./scripts/deploy_changed_files.sh --from origin/main~3
#   ./scripts/deploy_changed_files.sh --dry-run
#   ./scripts/deploy_changed_files.sh --no-tmux-kill --no-server-script
#
# Configure once, either by exporting these or by creating a
# ./scripts/deploy.env file (gitignored) that sets them:
#   SPORTS_DATA_LAB_DEPLOY_HOST=arm@10.0.40.100
#   SPORTS_DATA_LAB_DEPLOY_DIR=/home/arm/projects/sports_data_lab
#   SPORTS_DATA_LAB_DEPLOY_TMUX_SESSION=sports-data-lab      # optional
#   SPORTS_DATA_LAB_DEPLOY_SERVER_SCRIPT=/home/arm/bin/deploy-sports-data-lab.sh  # optional
#   SPORTS_DATA_LAB_DEPLOY_URL=http://10.0.40.100:6969       # optional, just printed at the end
#
# What counts as "changed" is tracked locally in .git/DEPLOY_HEAD -- the sha
# that was last successfully pushed with this script. First run has no
# baseline, so it refuses to guess and asks for --from explicitly.
set -euo pipefail

PROJECT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${PROJECT_DIR}"

if [[ -f "scripts/deploy.env" ]]; then
  # shellcheck disable=SC1091
  source "scripts/deploy.env"
fi

HOST=${SPORTS_DATA_LAB_DEPLOY_HOST:-}
REMOTE_DIR=${SPORTS_DATA_LAB_DEPLOY_DIR:-}
TMUX_SESSION=${SPORTS_DATA_LAB_DEPLOY_TMUX_SESSION:-sports-data-lab}
SERVER_SCRIPT=${SPORTS_DATA_LAB_DEPLOY_SERVER_SCRIPT:-/home/arm/bin/deploy-sports-data-lab.sh}
DEPLOY_URL=${SPORTS_DATA_LAB_DEPLOY_URL:-}
MARKER="${PROJECT_DIR}/.git/DEPLOY_HEAD"
TO_REF="HEAD"
FROM_REF=""
DRY_RUN=0
KILL_TMUX=1
RUN_SERVER_SCRIPT=1
VERIFY=1

usage() {
  cat <<'EOF'
Usage: deploy_changed_files.sh [options]

  --from REF          Baseline to diff against. Defaults to the sha recorded
                       in .git/DEPLOY_HEAD from the last successful run.
  --to REF            What to deploy. Defaults to HEAD.
  --no-tmux-kill       Don't kill the remote tmux session before copying.
  --no-server-script   Don't run the remote deploy script after copying.
  --no-verify          Skip the post-copy sanity check for app.py/requirements.txt.
  --dry-run            Print what would be copied/deleted/run; touch nothing.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --from) FROM_REF=${2:-}; shift 2 ;;
    --to) TO_REF=${2:-}; shift 2 ;;
    --no-tmux-kill) KILL_TMUX=0; shift ;;
    --no-server-script) RUN_SERVER_SCRIPT=0; shift ;;
    --no-verify) VERIFY=0; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "${HOST}" || -z "${REMOTE_DIR}" ]]; then
  echo "Set SPORTS_DATA_LAB_DEPLOY_HOST and SPORTS_DATA_LAB_DEPLOY_DIR" >&2
  echo "(export them, or put them in scripts/deploy.env)." >&2
  exit 2
fi

if [[ -z "${FROM_REF}" ]]; then
  if [[ -f "${MARKER}" ]]; then
    FROM_REF=$(cat "${MARKER}")
  else
    echo "No recorded last deploy (.git/DEPLOY_HEAD is missing)." >&2
    echo "Pass --from explicitly the first time, e.g. --from origin/main~1" >&2
    exit 2
  fi
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "Working tree is not clean. Commit or stash before deploying," >&2
  echo "so DEPLOY_HEAD records something you can redeploy from later." >&2
  exit 2
fi

CHANGED=$(git diff --name-only --diff-filter=ACMR "${FROM_REF}" "${TO_REF}" --)
DELETED=$(git diff --name-only --diff-filter=D "${FROM_REF}" "${TO_REF}" --)

if [[ -z "${CHANGED}" && -z "${DELETED}" ]]; then
  echo "Nothing changed between ${FROM_REF} and ${TO_REF}."
  exit 0
fi

echo "Deploying ${FROM_REF} -> ${TO_REF} to ${HOST}:${REMOTE_DIR}"
[[ -n "${CHANGED}" ]] && { echo "Changed:"; echo "${CHANGED}" | sed 's/^/  /'; }
[[ -n "${DELETED}" ]] && { echo "Deleted:"; echo "${DELETED}" | sed 's/^/  /'; }

if [[ ${DRY_RUN} -eq 1 ]]; then
  [[ ${KILL_TMUX} -eq 1 ]] && echo "Would kill tmux session '${TMUX_SESSION}' on ${HOST}."
  [[ ${RUN_SERVER_SCRIPT} -eq 1 ]] && echo "Would run 'sudo ${SERVER_SCRIPT}' on ${HOST} afterward."
  echo "--dry-run: nothing copied, nothing deleted, DEPLOY_HEAD not updated."
  exit 0
fi

if [[ ${KILL_TMUX} -eq 1 ]]; then
  echo "Stopping tmux session '${TMUX_SESSION}' on ${HOST} (if running)"
  ssh "${HOST}" "tmux kill-session -t '${TMUX_SESSION}' 2>/dev/null || true"
fi

if [[ -n "${CHANGED}" ]]; then
  # git archive pulls each file's content straight from the ${TO_REF} tree, so
  # local edits after that commit can't leak into the deploy and untracked
  # files never get pulled in by accident.
  git archive "${TO_REF}" -- ${CHANGED} | ssh "${HOST}" "mkdir -p '${REMOTE_DIR}' && tar -xf - -C '${REMOTE_DIR}'"
fi

if [[ -n "${DELETED}" ]]; then
  while IFS= read -r path; do
    [[ -z "${path}" ]] && continue
    ssh "${HOST}" "rm -f -- '${REMOTE_DIR}/${path}'"
  done <<< "${DELETED}"
fi

if [[ ${VERIFY} -eq 1 ]]; then
  echo "Verifying critical files on ${HOST}"
  if ! ssh "${HOST}" "test -s '${REMOTE_DIR}/app.py' && test -s '${REMOTE_DIR}/requirements.txt'"; then
    echo "Remote verification failed: app.py or requirements.txt is missing/empty." >&2
    echo "DEPLOY_HEAD was not updated -- fix the remote state, then rerun." >&2
    exit 1
  fi
fi

if [[ ${RUN_SERVER_SCRIPT} -eq 1 ]]; then
  echo "Running ${SERVER_SCRIPT} on ${HOST}"
  ssh -t "${HOST}" "sudo '${SERVER_SCRIPT}'"
fi

git rev-parse "${TO_REF}" > "${MARKER}"
echo "Done. DEPLOY_HEAD is now $(cat "${MARKER}")."

if echo "${CHANGED}" | grep -qx "requirements.txt"; then
  echo "requirements.txt changed -- confirm ${SERVER_SCRIPT} reinstalls dependencies."
fi

[[ -n "${DEPLOY_URL}" ]] && echo "${DEPLOY_URL}"
