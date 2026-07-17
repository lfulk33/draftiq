"""
evaluate_trade.py — Standalone trade evaluator for Dynasty/Redraft leagues.

Usage:
    python3 evaluate_trade.py

Edit the TRADE configuration section below to define the trade.
Pulls live roster and league settings from Sleeper, uses fantasy_players.json
for FC values, and evaluates using the same VORP logic as the draft assistant.
"""

from sleeper_league import get_user, get_league, get_rosters, load_players
from draft_advisor import FLEX_ELIGIBILITY, calculate_replacement_levels

# ── TRADE CONFIGURATION ──────────────────────────────────────────────────────
LEAGUE_ID   = "1312646372024930304"
USERNAME    = "LFULK33"
SEASON      = "2026"

# Players you are GIVING away
GIVE = ["Ray Davis", "2027 Late 4th Round Pick"]

# Players you are RECEIVING
RECEIVE = ["Isaiah Likely"]

# Player you would need to cut to make room (None if no cut needed)
# Their VORP will be subtracted from the RECEIVE side as an additional cost
CUT_PLAYER = None

# Path to your fantasy_players.json (FC-enriched player data)
PLAYERS_FILE = "fantasy_players.json"

# Manual pick values (FC dynasty value approximations)
# Late = picks 9-12, Mid = picks 5-8, Early = picks 1-4
PICK_VALUES = {
    "2026 early 1st": 8500,
    "2026 mid 1st": 7000,
    "2026 late 1st": 5500,
    "2026 early 2nd": 3500,
    "2026 mid 2nd": 2800,
    "2026 late 2nd": 2200,
    "2026 early 3rd": 1500,
    "2026 mid 3rd": 1100,
    "2026 late 3rd": 800,
    "2026 early 4th": 600,
    "2026 mid 4th": 450,
    "2026 late 4th": 300,
    "2027 early 1st": 6000,
    "2027 mid 1st": 4800,
    "2027 late 1st": 3800,
    "2027 early 2nd": 2500,
    "2027 mid 2nd": 2000,
    "2027 late 2nd": 1600,
    "2027 early 3rd": 1100,
    "2027 mid 3rd": 800,
    "2027 late 3rd": 600,
    "2027 early 4th": 450,
    "2027 mid 4th": 320,
    "2027 late 4th": 220,
}
# ─────────────────────────────────────────────────────────────────────────────

def find_my_roster(rosters, user_id):
    for roster in rosters:
        if roster.get("owner_id") == user_id:
            return roster
    return None

def build_league_context(league):
    roster_positions = league.get("roster_positions", [])
    settings = league.get("settings", {})
    num_teams = settings.get("num_teams", 12)

    dedicated = {"QB": 0, "RB": 0, "WR": 0, "TE": 0}
    flex_slot_counts = {}

    for slot in roster_positions:
        if slot in dedicated:
            dedicated[slot] += 1
        elif slot in FLEX_ELIGIBILITY:
            flex_slot_counts[slot] = flex_slot_counts.get(slot, 0) + 1

    import math
    backup_needs = {
        pos: math.ceil(dedicated[pos] / 2) if dedicated[pos] > 0 else 0
        for pos in dedicated
    }

    scoring_settings = league.get("scoring_settings", {})
    is_ppr = scoring_settings.get("rec", 0)

    return {
        "num_teams": num_teams,
        "dedicated_slots": dedicated,
        "flex_slot_counts": flex_slot_counts,
        "backup_needs": backup_needs,
        "is_dynasty": league.get("previous_league_id") is not None or settings.get("taxi_slots", 0) > 0,
        "taxi_slots_total": settings.get("taxi_slots", 0),
        "is_ppr": is_ppr,
        "roster_positions": roster_positions,
        "scoring_settings": scoring_settings,
    }

def get_vorp(player, replacement, value_key):
    pos = player.get("position", "?")
    val = player.get(value_key, 0) or 0
    if pos not in replacement or not val:
        return None
    return val - replacement[pos]

def find_player(name, players):
    name_lower = name.lower()
    # Exact match first
    for p in players.values():
        if (p.get("full_name") or "").lower() == name_lower:
            return p
    # Partial match fallback
    for p in players.values():
        if name_lower in (p.get("full_name") or "").lower():
            return p
    return None

def count_pos_on_roster(roster_player_ids, players, pos):
    count = 0
    for pid in roster_player_ids:
        p = players.get(pid, {})
        if p.get("position") == pos:
            count += 1
    return count

def evaluate():
    print("Loading player data...")
    all_players = load_players(PLAYERS_FILE)

    print(f"Fetching league {LEAGUE_ID}...")
    league = get_league(LEAGUE_ID)
    league_context = build_league_context(league)

    print(f"Fetching roster for {USERNAME}...")
    user_id = get_user(USERNAME)["user_id"]
    rosters = get_rosters(LEAGUE_ID)
    my_roster = find_my_roster(rosters, user_id)

    if not my_roster:
        print(f"Could not find roster for {USERNAME}")
        return

    my_player_ids = my_roster.get("players") or []
    is_dynasty = league_context["is_dynasty"]
    value_key = "fc_value" if is_dynasty else "fc_redraft_value"

    print(f"\nLeague: {league.get('name')}")
    print(f"Mode: {'Dynasty' if is_dynasty else 'Redraft'} | Teams: {league_context['num_teams']}")
    print(f"Dedicated slots: {league_context['dedicated_slots']}")
    print(f"Flex slots: {league_context['flex_slot_counts']}")
    print(f"Roster size: {len(my_player_ids)} players")

    # Calculate replacement levels from full player pool
    replacement, drafted_count, dedicated_cutoff = calculate_replacement_levels(
        league_context, all_players, value_key
    )

    print(f"\nReplacement levels ({value_key}):")
    for pos, val in replacement.items():
        print(f"  {pos}: {val}")

    # Evaluate trade
    print(f"\n{'='*50}")
    print(f"TRADE EVALUATION")
    print(f"  GIVING:    {', '.join(GIVE)}")
    print(f"  RECEIVING: {', '.join(RECEIVE)}")
    print(f"{'='*50}\n")

    def evaluate_side(names, label):
        total_vorp = 0
        print(f"{label}:")
        for name in names:
            # Check if this is a pick
            pick_key = name.lower()
            if pick_key in PICK_VALUES:
                pick_val = PICK_VALUES[pick_key]
                rep_val = replacement.get("RB", 0)
                pick_vorp = pick_val - rep_val
                print(f"  {name}")
                print(f"    Dynasty value:  {pick_val:,} (manual estimate)")
                print(f"    VORP:           {round(pick_vorp):+}")
                total_vorp += pick_vorp
                continue

            p = find_player(name, all_players)
            if not p:
                print(f"  {name}: NOT FOUND in player data")
                continue

            pos = p.get("position", "?")
            fc_val = p.get(value_key, 0) or 0
            fc_redraft = p.get("fc_redraft_value", 0) or 0
            fc_dynasty = p.get("fc_value", 0) or 0
            vorp = get_vorp(p, replacement, value_key)

            pos_count = count_pos_on_roster(my_player_ids, all_players, pos)
            dedicated = league_context["dedicated_slots"].get(pos, 0)
            backup = league_context["backup_needs"].get(pos, 0)
            need = dedicated + backup

            flex_slots = league_context["flex_slot_counts"]
            flex_eligible_slots = sum(
                count for slot, count in flex_slots.items()
                if pos in FLEX_ELIGIBILITY.get(slot, set())
            )

            roster_status = "NEEDED" if pos_count < need else "DEPTH"
            ded_str = f"{dedicated} dedicated + {flex_eligible_slots} flex-eligible"

            print(f"  {name} ({pos})")
            print(f"    Dynasty value:  {fc_dynasty:,}")
            print(f"    Redraft value:  {fc_redraft:,}")
            print(f"    VORP ({value_key}): {round(vorp) if vorp is not None else 'N/A'}")
            print(f"    Your {pos}s:     {pos_count} owned, need {need} ({ded_str})")
            print(f"    Roster status:  {roster_status}")

            if vorp is not None:
                total_vorp += vorp

        return total_vorp

    give_vorp = evaluate_side(GIVE, "GIVING AWAY")
    print()
    receive_vorp = evaluate_side(RECEIVE, "RECEIVING")
    cut_vorp = 0
    if CUT_PLAYER:
        print()
        cut_vorp = evaluate_side([CUT_PLAYER], "CUT COST (player you lose to make room)")

    net = receive_vorp - give_vorp - cut_vorp
    print(f"\n{'='*50}")
    print(f"NET VORP CHANGE: {round(net):+}")
    if net > 0:
        print(f"VERDICT: Lean ACCEPT — you gain {round(net)} VORP")
    elif net < 0:
        print(f"VERDICT: Lean DECLINE — you lose {round(abs(net))} VORP")
    else:
        print(f"VERDICT: EVEN trade by VORP")

    # No-TE league note
    ded_te = league_context["dedicated_slots"].get("TE", 0)
    if ded_te == 0:
        print(f"\nNOTE: No dedicated TE slot in this league.")
        print(f"  TE value is discounted — they compete only for flex spots.")
        print(f"  Redraft value is more relevant than dynasty value for TEs here.")
        if any(find_player(n, all_players) and find_player(n, all_players).get("position") == "TE"
               for n in GIVE + RECEIVE):
            te_replacement_redraft, _, _ = calculate_replacement_levels(
                league_context, all_players, "fc_redraft_value"
            )
            print(f"  TE redraft replacement level: {te_replacement_redraft.get('TE', 0):,}")
    print(f"{'='*50}")

if __name__ == "__main__":
    evaluate()