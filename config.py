import os
from dotenv import load_dotenv
load_dotenv()

SLEEPER_BASE_URL = "https://api.sleeper.app/v1"
FANTASYCALC_URL = "https://api.fantasycalc.com/values/current"
SLEEPER_USERNAME = os.environ.get("SLEEPER_USERNAME", "")
SEASON = "2026"
DRAFT_POLL_INTERVAL = 15  # seconds between pick checks
DEV_MODE = os.getenv("DEV_MODE", "false").lower() == "true"
# Best player available thresholds
# If top available player's value exceeds best available at needed position by this amount,
# recommend best player regardless of positional need
BPA_THRESHOLD_DYNASTY = 500
BPA_THRESHOLD_REDRAFT = 500
TAXI_THRESHOLD_QB = 1000
TAXI_THRESHOLD_RB = 100
TAXI_THRESHOLD_WR = 100
TAXI_THRESHOLD_TE = 100
REDRAFT_THRESHOLD_QB = 2000
REDRAFT_THRESHOLD_RB = 500
REDRAFT_THRESHOLD_WR = 500
REDRAFT_THRESHOLD_TE = 300
# Urgency modifier for BPA decision scoring.
# Controls how much positional urgency influences the pick recommendation
# relative to raw VORP. Higher values favor urgent positions; lower values
# favor pure VORP. Default 1.0 = equal weight.
# This will eventually be tunable via the Draft Strategy Slider.
URGENCY_MODIFIER = 1.0
# LLM model selection (see llm_client.py for options)
DEFAULT_MODEL = "claude-haiku"