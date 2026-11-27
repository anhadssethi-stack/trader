#!/bin/bash
# Runs the daily update + scan. Called by launchd (see scripts/install_mac_schedule.sh).
set -u
cd "$(dirname "$0")/.."
mkdir -p data/logs
source .venv/bin/activate
echo "===== $(date) =====" >> data/logs/daily.log
python -m nse_research.cli daily >> data/logs/daily.log 2>&1
echo "exit code $?" >> data/logs/daily.log
