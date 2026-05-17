import time
import json
from sleeper_draft import get_picks, get_available_rookies, get_available_players, count_my_picks
from sleeper_league import get_rosters, get_league, get_taxi_count
from draft_advisor import get_recommendation
from config import DRAFT_POLL_INTERVAL

def is_rookie_draft(draft_detail):
    return draft_detail.get("type") == "linear" and draft_detail["settings"].get("rounds", 99) <= 5

def poll_draft(draft_id, players, my_roster, my_roster_id, league_detail, draft_detail):
    print(f"Polling draft {draft_id} every {DRAFT_POLL_INTERVAL} seconds...")
    last_pick_count = 0
    rookie_draft = is_rookie_draft(draft_detail)

    my_existing_players = []
    for pid in my_roster.get("players") or []:
        player = players.get(pid, {})
        if player:
            my_existing_players.append({
                "name": player.get("full_name"),
                "position": player.get("position"),
                "age": player.get("fc_age") or player.get("age"),
                "dynasty_value": player.get("fc_value", "unranked")
            })

    while True:
        try:
            picks = get_picks(draft_id)
            current_count = len(picks)

            if current_count > last_pick_count:
                new_picks = picks[last_pick_count:]
                for pick in new_picks:
                    pid = pick["player_id"]
                    player = players.get(pid, {})
                    name = player.get("full_name", "Unknown")
                    pos = player.get("position", "?")
                    print(f"\nNew pick: {pick['pick_no']} - {name} ({pos}) - roster {pick['roster_id']}")

                last_pick_count = current_count

                available = get_available_rookies(players, picks) if rookie_draft else get_available_players(players, picks)

                my_picks_count = count_my_picks(picks, my_roster_id)
                total_rounds = draft_detail["settings"].get("rounds", 4)
                picks_remaining = total_rounds - my_picks_count

                league_context = {
                    "num_teams": league_detail["settings"].get("num_teams"),
                    "roster_positions": league_detail.get("roster_positions"),
                    "scoring_settings": league_detail.get("scoring_settings"),
                    "draft_type": draft_detail.get("type"),
                    "rounds": total_rounds,
                    "taxi_slots_total": league_detail["settings"].get("taxi_slots"),
                    "taxi_slots_used": get_taxi_count(my_roster),
                    "taxi_years": league_detail["settings"].get("taxi_years"),
                    "picks_made_by_me": my_picks_count,
                    "picks_remaining_for_me": picks_remaining,
                    "my_existing_roster": my_existing_players
                }

                pick_number = current_count + 1
                print(f"\nGetting recommendation for pick {pick_number}...")
                rec = get_recommendation(picks, available, my_roster, league_context, pick_number)

                print(f"\nRECOMMENDATION: {rec['recommendation']} ({rec['position']})")
                print(f"Reasoning: {rec['reasoning']}")
                print(f"Positional note: {rec['positional_note']}")
                print(f"Upside: {rec['upside']}")
                print(f"\nAlternatives:")
                for alt in rec.get("alternatives", []):
                    print(f"  - {alt['name']}: {alt['reason']}")

            else:
                print(f"No new picks. Total so far: {current_count}")

        except Exception as e:
            print(f"Poll error: {e}")

        time.sleep(DRAFT_POLL_INTERVAL)

if __name__ == "__main__":
    from session import start_session

    session = start_session()
    draft_id = session["draft_id"]
    players = session["players"]
    league_id = session["league_id"]
    my_roster_id = session["my_roster_id"]

    league_detail = get_league(league_id)
    rosters = get_rosters(league_id)
    my_roster = next(r for r in rosters if r["roster_id"] == my_roster_id)

    poll_draft(draft_id, players, my_roster, my_roster_id, league_detail, session["draft_detail"])