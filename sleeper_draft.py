import requests
import json
from config import SLEEPER_BASE_URL, SEASON

def get_drafts(league_id):
    url = f"{SLEEPER_BASE_URL}/league/{league_id}/drafts"
    response = requests.get(url)
    response.raise_for_status()
    return response.json()

def get_picks(draft_id):
    url = f"{SLEEPER_BASE_URL}/draft/{draft_id}/picks"
    response = requests.get(url)
    response.raise_for_status()
    return response.json()

def get_draft_detail(draft_id):
    url = f"{SLEEPER_BASE_URL}/draft/{draft_id}"
    response = requests.get(url)
    response.raise_for_status()
    return response.json()

def get_available_players(players, picks):
    drafted_ids = {pick["player_id"] for pick in picks}
    return {pid: p for pid, p in players.items() if pid not in drafted_ids}

def summarize_picks(picks, players):
    print(f"\nPicks made so far: {len(picks)}")
    for pick in picks[-5:]:
        pid = pick["player_id"]
        name = players.get(pid, {}).get("full_name", "Unknown")
        pos = players.get(pid, {}).get("position", "?")
        print(f"  Pick {pick['pick_no']} - {name} ({pos}) - by roster {pick['roster_id']}")

def get_available_rookies(players, picks):
    drafted_ids = {pick["player_id"] for pick in picks}
    return {
        pid: p for pid, p in players.items()
        if pid not in drafted_ids
        and p.get("years_exp") == 0
    }

if __name__ == "__main__":
    league_id = "1312068664308019200"
    
    with open("fantasy_players.json") as f:
        players = json.load(f)

    print("Fetching drafts...")
    drafts = get_drafts(league_id)
    for i, d in enumerate(drafts):
        print(f"{i}. {d['type']} - status: {d['status']} - ID: {d['draft_id']}")

    print("\nEnter draft_id from above:")
    draft_id = input().strip()

    print("\nFetching draft detail...")
    detail = get_draft_detail(draft_id)
    print(f"Status: {detail['status']}")
    print(f"Type: {detail['type']}")
    print(f"Rounds: {detail['settings'].get('rounds')}")
    print(f"Teams: {detail['settings'].get('teams')}")
    print(f"Slot to roster map: {json.dumps(detail.get('slot_to_roster_id'), indent=2)}")

    print("\nFetching picks...")
    picks = get_picks(draft_id)

    summarize_picks(picks, players)

    available = get_available_players(players, picks)

    rookies = get_available_rookies(players, picks)
    ranked_rookies = [p for p in rookies.values() if "fc_overall_rank" in p]
    ranked_rookies.sort(key=lambda x: x["fc_overall_rank"])
    print(f"\nAvailable rookies with dynasty rank: {len(ranked_rookies)}")
    print("\nTop 10 available rookies:")
    for p in ranked_rookies[:10]:
        print(f"{p['fc_overall_rank']}. {p['full_name']} ({p.get('position')}) - value: {p['fc_value']}")