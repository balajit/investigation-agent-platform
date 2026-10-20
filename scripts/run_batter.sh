set -a; source ./.env.local; source ~/bin/.iap.env.local; set +a
uv run python scripts/live_battery.py --tenant n-batt
