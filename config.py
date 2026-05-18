import os

SLEEPER_BASE_URL = "https://api.sleeper.app/v1"
FANTASYCALC_URL = "https://api.fantasycalc.com/values/current"
SLEEPER_USERNAME = "LFULK33"
SEASON = "2026"
DRAFT_POLL_INTERVAL = 15  # seconds between pick checks
DEV_MODE = os.getenv("DEV_MODE", "false").lower() == "true"