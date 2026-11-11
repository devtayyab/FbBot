#!/bin/zsh
# One hourly cycle (scan + post), logged to data/logs/YYYY-MM-DD.log.
cd "$(dirname "$0")/.." || exit 1
mkdir -p data/logs
./.venv/bin/python bot.py run >> "data/logs/$(date +%Y-%m-%d).log" 2>&1
