#!/usr/bin/env bash
# Auto-commit and push agentflow changes to sync across hosts.
# Installed as a cron job: every 30 minutes.

set -euo pipefail

REPO="/home/tam/Projects/agentflow"
LOG="/home/tam/Projects/agentflow/scripts/auto-sync.log"

cd "$REPO"

# Nothing to do if tree is clean
if git diff --quiet && git diff --cached --quiet && [ -z "$(git ls-files --others --exclude-standard)" ]; then
    exit 0
fi

TIMESTAMP="$(date '+%Y-%m-%d %H:%M:%S')"
git add -A
git commit -m "auto-sync: ${TIMESTAMP}" --no-verify 2>&1 | tee -a "$LOG"
git push origin HEAD 2>&1 | tee -a "$LOG"

echo "[${TIMESTAMP}] sync done" >> "$LOG"
