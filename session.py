import json
from sleeper_league import get_user, get_leagues, get_rosters, get_league
from sleeper_draft import get_drafts, get_picks, get_draft_detail
from config import SLEEPER_USERNAME, SEASON

def start_session():
    print("Fetching user...")
    user = get_user(SLEEPER_USERNAME)
    user_id = user["user_id"]
    print(f"Found user: {user.get('display_name')}")

    print("\nFetching leagues...")
    leagues = get_leagues(user_id)
    for i, league in enumerate(leagues):
        print(f"{i}. {league['name']}")
    
    league_index = int(input("\nEnter league number: "))
    league = leagues[league_index]
    league_id = league["league_id"]
    print(f"Selected: {league['name']}")

    rosters = get_rosters(league_id)
    my_roster = next(r for r in rosters if r["owner_id"] == user_id)
    my_roster_id = my_roster["roster_id"]
    print(f"Your roster ID: {my_roster_id}")

    print("\nFetching drafts...")
    drafts = get_drafts(league_id)
    for i, d in enumerate(drafts):
        print(f"{i}. {d['type']} - status: {d['status']}")

    draft_index = int(input("\nEnter draft number: "))
    draft = drafts[draft_index]
    draft_id = draft["draft_id"]

    detail = get_draft_detail(draft_id)

    with open("fantasy_players.json") as f:
        players = json.load(f)

    picks = get_picks(draft_id)

    return {
        "user_id": user_id,
        "league_id": league_id,
        "league": league,
        "my_roster_id": my_roster_id,
        "draft_id": draft_id,
        "draft_detail": detail,
        "players": players,
        "picks": picks
    }

if __name__ == "__main__":
    session = start_session()
    print(f"\nSession ready.")
    print(f"League: {session['league']['name']}")
    print(f"Draft status: {session['draft_detail']['status']}")
    print(f"Picks made: {len(session['picks'])}")
    print(f"Your roster ID: {session['my_roster_id']}")