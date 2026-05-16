import requests
import json
from config import FANTASYCALC_URL

def get_dynasty_values(num_qbs=2, ppr=1, num_teams=12):
    params = {
        "isDynasty": "true",
        "numQbs": num_qbs,
        "ppr": ppr,
        "numTeams": num_teams
    }
    response = requests.get(FANTASYCALC_URL, params=params)
    response.raise_for_status()
    return response.json()

def merge_into_players(players, fc_data):
    for entry in fc_data:
        player = entry.get("player", {})
        sleeper_id = player.get("sleeperId")
        if sleeper_id and sleeper_id in players:
            players[sleeper_id]["fc_value"] = entry.get("value")
            players[sleeper_id]["fc_overall_rank"] = entry.get("overallRank")
            players[sleeper_id]["fc_position_rank"] = entry.get("positionRank")
            players[sleeper_id]["fc_tier"] = entry.get("maybeTier")
            players[sleeper_id]["fc_trend"] = entry.get("trend30Day")
            players[sleeper_id]["fc_age"] = player.get("maybeAge")
    return players

if __name__ == "__main__":
    with open("fantasy_players.json") as f:
        players = json.load(f)

    print("Fetching dynasty values from FantasyCalc...")
    fc_data = get_dynasty_values()
    print(f"Got {len(fc_data)} players from FantasyCalc")

    players = merge_into_players(players, fc_data)

    ranked = [p for p in players.values() if "fc_overall_rank" in p]
    ranked.sort(key=lambda x: x["fc_overall_rank"])

    print(f"Players matched to Sleeper data: {len(ranked)}")
    print("\nTop 10:")
    for p in ranked[:10]:
        print(f"{p['fc_overall_rank']}. {p['full_name']} ({p.get('position')}) - value: {p['fc_value']} - age: {p.get('fc_age')}")

    with open("fantasy_players.json", "w") as f:
        json.dump(players, f, indent=2)