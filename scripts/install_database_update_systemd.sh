#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: sudo $0 --user USER [--group GROUP] [--project-dir DIR] [--python PATH]"
}

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this installer as root (normally with sudo)." >&2
  exit 1
fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd "${SCRIPT_DIR}/.." && pwd)
RUN_USER=""
RUN_GROUP=""
PYTHON_EXE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) RUN_USER=${2:-}; shift 2 ;;
    --group) RUN_GROUP=${2:-}; shift 2 ;;
    --project-dir) PROJECT_DIR=${2:-}; shift 2 ;;
    --python) PYTHON_EXE=${2:-}; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z ${RUN_USER} ]]; then
  echo "--user is required." >&2
  usage >&2
  exit 2
fi
if ! id "${RUN_USER}" >/dev/null 2>&1; then
  echo "Linux user does not exist: ${RUN_USER}" >&2
  exit 2
fi
if [[ -z ${RUN_GROUP} ]]; then
  RUN_GROUP=$(id -gn "${RUN_USER}")
fi
if ! getent group "${RUN_GROUP}" >/dev/null; then
  echo "Linux group does not exist: ${RUN_GROUP}" >&2
  exit 2
fi

PROJECT_DIR=$(cd "${PROJECT_DIR}" && pwd)
if [[ -z ${PYTHON_EXE} ]]; then
  if [[ -x "${PROJECT_DIR}/.venv/bin/python" ]]; then
    PYTHON_EXE="${PROJECT_DIR}/.venv/bin/python"
  else
    PYTHON_EXE=$(command -v python3)
  fi
fi
if [[ ! -x ${PYTHON_EXE} ]]; then
  echo "Python executable not found or not executable: ${PYTHON_EXE}" >&2
  exit 2
fi
if [[ ! -f "${PROJECT_DIR}/database_updates.py" ]]; then
  echo "database_updates.py was not found in ${PROJECT_DIR}" >&2
  exit 2
fi

for value in "${PROJECT_DIR}" "${PYTHON_EXE}" "${RUN_USER}" "${RUN_GROUP}"; do
  if [[ ${value} == *$'\n'* || ${value} == *'|'* || ${value} == *'&'* || \
        ${value} == *'\'* || ${value} == *'%'* ]]; then
    echo "Installer values contain characters that cannot be rendered safely." >&2
    exit 2
  fi
done

(cd "${PROJECT_DIR}" && "${PYTHON_EXE}" -c "import database_updates")

SOURCE_DIR="${PROJECT_DIR}/deploy/systemd"
TEMP_DIR=$(mktemp -d)
trap 'rm -rf -- "${TEMP_DIR}"' EXIT

render() {
  local source=$1
  local destination=$2
  sed \
    -e "s|@PROJECT_DIR@|${PROJECT_DIR}|g" \
    -e "s|@PYTHON_EXE@|${PYTHON_EXE}|g" \
    -e "s|@RUN_USER@|${RUN_USER}|g" \
    -e "s|@RUN_GROUP@|${RUN_GROUP}|g" \
    "${source}" > "${TEMP_DIR}/${destination}"
  install -m 0644 "${TEMP_DIR}/${destination}" "/etc/systemd/system/${destination}"
}

render "${SOURCE_DIR}/sports-data-lab-db-update@.service.in" \
  "sports-data-lab-db-update@.service"
render "${SOURCE_DIR}/sports-data-lab-db-regular.timer.in" \
  "sports-data-lab-db-regular.timer"
render "${SOURCE_DIR}/sports-data-lab-db-brownlow.timer.in" \
  "sports-data-lab-db-brownlow.timer"
render "${SOURCE_DIR}/sports-data-lab-db-grand-final.timer.in" \
  "sports-data-lab-db-grand-final.timer"
render "${SOURCE_DIR}/sports-data-lab-db-gridley.timer.in" \
  "sports-data-lab-db-gridley.timer"

systemctl daemon-reload
systemctl enable --now \
  sports-data-lab-db-regular.timer \
  sports-data-lab-db-brownlow.timer \
  sports-data-lab-db-grand-final.timer \
  sports-data-lab-db-gridley.timer

echo "Installed database update timers:"
systemctl list-timers --all 'sports-data-lab-db-*'
