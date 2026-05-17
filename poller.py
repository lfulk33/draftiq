import time
import json
from sleeper_draft import get_picks, get_available_rookies
from config import DRAFT_POLL_INTERVAL

def poll_draft(draft_id, players, on_new_pick, is_rookie_draft=True):
    print(f"Polling draft {draft_id} every {DRAFT_POLL_INTERVAL} seconds...")
    last_pick_count = 0

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
                    roster_id = pick["roster_id"]
                    print(f"\nNew pick: {pick['pick_no']} - {name} ({pos}) - roster {roster_id}")

                last_pick_count = current_count
                available = get_available_rookies(players, picks) if is_rookie_draft else get_available_players(players, picks)
                on_new_pick(picks, available)

            else:
                print(f"No new picks. Total so far: {current_count}")

        except Exception as e:
            print(f"Poll error: {e}")

        time.sleep(DRAFT_POLL_INTERVAL)

if __name__ == "__main__":
    from session import start_session
    from sleeper_draft import get_available_players

    session = start_session()
    draft_id = session["draft_id"]
    players = session["players"]

    def on_new_pick(picks, available):
        ranked = [p for p in available.values() if "fc_overall_rank" in p]
        ranked.sort(key=lambda x: x["fc_overall_rank"])
        print(f"Available with rank: {len(ranked)}")
        print("Top 5 available:")
        for p in ranked[:5]:
            print(f"  {p['fc_overall_rank']}. {p['full_name']} ({p.get('position')}) - value: {p['fc_value']}")

    poll_draft(draft_id, players, on_new_pick)