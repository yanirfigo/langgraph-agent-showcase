#!/bin/bash
# Install or update Job Copilot on the host. Run as the service user; needs no sudo.
#   cd ~/job-copilot && git pull && bash deploy/install.sh
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:${PATH:-/usr/bin:/bin}"
command -v uv >/dev/null 2>&1 || { echo "uv not found (expected in $HOME/.local/bin or on PATH)"; exit 1; }

uv sync --frozen
uv run --frozen pytest -q -p no:cacheprovider >/dev/null 2>&1 || { echo "tests failed: not installing"; exit 1; }
mkdir -p "$HOME/.local/share/job-copilot" "$HOME/.config/systemd/user"
chmod 700 "$HOME/.local/share/job-copilot"

for unit in deploy/systemd/*; do
  install -m 0644 "$unit" "$HOME/.config/systemd/user/"
done
systemctl --user daemon-reload
systemctl --user enable --now job-copilot-bot.service job-copilot-run.timer job-copilot-promote.timer
systemctl --user restart job-copilot-bot.service
systemctl --user --no-pager list-timers 'job-copilot-*'
systemctl --user is-active job-copilot-bot.service
