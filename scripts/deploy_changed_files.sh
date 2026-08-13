#!/usr/bin/env bash
# Copy only the files that changed since the last deploy to the server,
# preserving directory structure, over a plain ssh connection (no rsync
# required -- this only needs ssh/scp/tar, which Git Bash on Windows already
# has).
#
# Usage:
#   ./scripts/deploy_changed_files.sh                # deploy HEAD
#   ./scripts/deploy_changed_files.sh --from origin/main~3
#   ./scripts/deploy_changed_files.sh --dry-run
#   ./scripts/deploy_changed_files.sh --restart sports-data-lab.service
#
# Configure once, either by exporting these or by creating a
# ./scripts/deploy.env file (gitignored) that sets them:
#   SPORTS_DATA_LAB_DEPLOY_HOST=sportslab@example.com
#   SPORTS_DATA_LAB_DEPLOY_DIR=/srv/sports_data_lab
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
MARKER="${PROJECT_DIR}/.git/DEPLOY_HEAD"
TO_REF="HEAD"
FROM_REF=""
DRY_RUN=0
RESTART_SERVICE=""

usage() {
  cat <<'EOF'
Usage: deploy_changed_files.sh [--from REF] [--to REF] [--restart SERVICE] [--dry-run]

  --from REF        Baseline to diff against. Defaults to the sha recorded in
                     .git/DEPLOY_HEAD from the last successful run.
  --to REF          What to deploy. Defaults to HEAD.
  --restart SERVICE Run `systemctl restart SERVICE` on the remote after copying.
  --dry-run         Print what would be copied/deleted/restarted; touch nothing.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --from) FROM_REF=${2:-}; shift 2 ;;
    --to) TO_REF=${2:-}; shift 2 ;;
    --restart) RESTART_SERVICE=${2:-}; shift 2 ;;
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
  echo "--dry-run: nothing copied, nothing deleted, DEPLOY_HEAD not updated."
  exit 0
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

if [[ -n "${RESTART_SERVICE}" ]]; then
  echo "Restarting ${RESTART_SERVICE} on ${HOST}"
  ssh "${HOST}" "sudo systemctl restart '${RESTART_SERVICE}'"
fi

git rev-parse "${TO_REF}" > "${MARKER}"
echo "Done. DEPLOY_HEAD is now $(cat "${MARKER}")."

if echo "${CHANGED}" | grep -qx "requirements.txt"; then
  echo "requirements.txt changed -- remember to run pip install on the server."
fi
