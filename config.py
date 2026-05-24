import os
from dotenv import load_dotenv
load_dotenv()

SLEEPER_BASE_URL = "https://api.sleeper.app/v1"
FANTASYCALC_URL = "https://api.fantasycalc.com/values/current"
SLEEPER_USERNAME = "LFULK33"
SEASON = "2026"
DRAFT_POLL_INTERVAL = 15  # seconds between pick checks
DEV_MODE = os.getenv("DEV_MODE", "false").lower() == "true"
# Best player available thresholds
# If top available player's value exceeds best available at needed position by this amount,
# recommend best player regardless of positional need
BPA_THRESHOLD_DYNASTY = 30
BPA_THRESHOLD_REDRAFT = 800
TAXI_THRESHOLD_QB = 1000
TAXI_THRESHOLD_RB = 100
TAXI_THRESHOLD_WR = 100
TAXI_THRESHOLD_TE = 100