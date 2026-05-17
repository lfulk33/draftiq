import requests
import json
from config import SLEEPER_BASE_URL, SLEEPER_USERNAME, SEASON

def get_user(username):
    url = f"{SLEEPER_BASE_URL}/user/{username}"
    response = requests.get(url)
    response.raise_for_status()
    return response.json()

def get_leagues(user_id, season=SEASON):
    url = f"{SLEEPER_BASE_URL}/user/{user_id}/leagues/nfl/{season}"
    response = requests.get(url)
    response.raise_for_status()
    return response.json()

def get_league(league_id):
    url = f"{SLEEPER_BASE_URL}/league/{league_id}"
    response = requests.get(url)
    response.raise_for_status()
    return response.json()

def get_rosters(league_id):
    url = f"{SLEEPER_BASE_URL}/league/{league_id}/rosters"
    response = requests.get(url)
    response.raise_for_status()
    return response.json()

def get_scoring_settings(league_id):
    league = get_league(league_id)
    return {
        "roster_positions": league.get("roster_positions"),
        "scoring_settings": league.get("scoring_settings")
    }

def get_taxi_count(my_roster):
    taxi = my_roster.get("taxi") or []
    return len(taxi)

def get_league_users(league_id):
    url = f"{SLEEPER_BASE_URL}/league/{league_id}/users"
    response = requests.get(url)
    response.raise_for_status()
    return response.json()
    
if __name__ == "__main__":
    print("Fetching user...")
    user = get_user(SLEEPER_USERNAME)
    user_id = user["user_id"]
    print(f"User ID: {user_id}")

    print("\nFetching leagues...")
    leagues = get_leagues(user_id)
    for i, league in enumerate(leagues):
        print(f"{i}. {league['name']} - ID: {league['league_id']}")

    print("\nPaste your dynasty league_id from above and press enter:")
    league_id = input().strip()

    print("\nFetching league settings...")
    league = get_league(league_id)
    print(json.dumps(league["settings"], indent=2))
    print(json.dumps(league.get("roster_positions"), indent=2))
    print("\nFetching rosters...")
    rosters = get_rosters(league_id)
    print(f"\nGot {len(rosters)} rosters")
    for r in rosters:
        print(f"Roster {r['roster_id']} - owner: {r['owner_id']} - players: {len(r.get('players') or [])}")