import requests
import json

SLEEPER_BASE_URL = "https://api.sleeper.app/v1"

def get_all_players():
    url = f"{SLEEPER_BASE_URL}/players/nfl"
    response = requests.get(url)
    response.raise_for_status()
    return response.json()

def save_players(players, filename="players.json"):
    with open(filename, "w") as f:
        json.dump(players, f, indent=2)
    print(f"Saved {len(players)} players to {filename}")

def print_sample(players, n=3):
    sample_ids = list(players.keys())[:n]
    for pid in sample_ids:
        print(json.dumps(players[pid], indent=2))
        print("---")

if __name__ == "__main__":
    print("Fetching players from Sleeper...")
    players = get_all_players()
    save_players(players)
    print_sample(players)
    from player_filter import filter_fantasy_players

    fantasy_players = filter_fantasy_players(players)
    save_players(fantasy_players, "fantasy_players.json")