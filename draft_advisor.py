import json
import math
from llm_client import get_completion

SYSTEM_PROMPT = """You are an expert dynasty fantasy football draft assistant. 
You reason about long-term player value, age curves, and positional scarcity.
You give concise, confident recommendations with clear reasoning.
Always base your recommendations on the actual league settings provided, including roster construction and scoring format.
Always respond in valid JSON only. No preamble, no markdown, no explanation outside the JSON."""

def get_flex_eligible(slot_name):
    known = {
        "FLEX": {"RB", "WR", "TE"},
        "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
        "REC_FLEX": {"WR", "RB", "TE"},
        "WRRB_FLEX": {"WR", "RB"},
    }
    if slot_name in known:
        return known[slot_name]
    parts = slot_name.replace("_FLEX", "").split("_")
    return set(parts)

def calculate_starter_ids(active_ids, players, league_detail):
    roster_positions = league_detail.get("roster_positions", [])

    enriched = []
    for pid in active_ids:
        player = players.get(pid, {})
        if not player:
            continue
        dynasty_val = player.get("fc_value", 0) if isinstance(player.get("fc_value"), int) else 0
        redraft_val = player.get("fc_redraft_value", 0) if isinstance(player.get("fc_redraft_value"), int) else 0
        enriched.append({
            "id": pid,
            "position": player.get("position", "?"),
            "value": redraft_val if redraft_val > 0 else dynasty_val
        })

    enriched.sort(key=lambda x: x["value"], reverse=True)

    starter_ids = set()
    single_positions = {"QB", "RB", "WR", "TE", "K", "DEF"}
    slot_counts = {}
    for slot in roster_positions:
        if slot in single_positions:
            slot_counts[slot] = slot_counts.get(slot, 0) + 1

    slots_remaining = dict(slot_counts)

    for player in enriched:
        pos = player["position"]
        if pos in slots_remaining and slots_remaining[pos] > 0:
            starter_ids.add(player["id"])
            slots_remaining[pos] -= 1

    for slot in roster_positions:
        if slot not in single_positions and slot != "BN":
            eligible = get_flex_eligible(slot)
            for player in enriched:
                if player["id"] not in starter_ids and player["position"] in eligible:
                    starter_ids.add(player["id"])
                    break

    return starter_ids

def calculate_roster_needs(league_detail):
    roster_positions = league_detail.get("roster_positions", [])

    starter_counts = {"QB": 0, "RB": 0, "WR": 0, "TE": 0}
    for slot in roster_positions:
        if slot == "QB":
            starter_counts["QB"] += 1
        elif slot == "RB":
            starter_counts["RB"] += 1
        elif slot == "WR":
            starter_counts["WR"] += 1
        elif slot == "TE":
            starter_counts["TE"] += 1
        elif slot == "SUPER_FLEX":
            starter_counts["QB"] += 0.5
            starter_counts["RB"] += 0.5
            starter_counts["WR"] += 0.5
        elif slot == "FLEX":
            starter_counts["RB"] += 0.5
            starter_counts["WR"] += 0.5
        elif slot == "REC_FLEX":
            starter_counts["WR"] += 0.5
            starter_counts["TE"] += 0.5
        elif slot == "WRRB_FLEX":
            starter_counts["RB"] += 0.5
            starter_counts["WR"] += 0.5

    backup_counts = {pos: math.ceil(count / 2) for pos, count in starter_counts.items()}
    total_needs = {pos: math.ceil(starter_counts[pos]) + backup_counts[pos] for pos in starter_counts}

    return starter_counts, backup_counts, total_needs

def decide_placement(rookie, sim_active, sim_taxi, league_detail, players, reserve_ids, starter_ids):
    taxi_slots_total = league_detail["settings"].get("taxi_slots", 0)
    taxi_allow_vets = league_detail["settings"].get("taxi_allow_vets", 0)
    roster_positions = league_detail.get("roster_positions", [])
    roster_max = len(roster_positions) + len(reserve_ids)

    taxi_eligible = rookie["years_exp"] == 0 or taxi_allow_vets == 1
    open_taxi = taxi_slots_total - len(sim_taxi)
    roster_count = len(sim_active)
    roster_over = max(0, roster_count - roster_max)

    starter_counts, backup_counts, total_needs = calculate_roster_needs(league_detail)
    pos = rookie["position"]
    total_need = total_needs.get(pos, 2)

    # Get all players at this position from sim_active, sorted by redraft then dynasty
    pos_players = [
        p for p in sim_active.values()
        if p.get("position") == pos
    ]
    pos_players.sort(key=lambda x: (x.get("redraft_value", 0), x.get("dynasty_value", 0)), reverse=True)

    # Find rookie's rank among all position players including himself
    all_pos = pos_players + [rookie]
    all_pos.sort(key=lambda x: (x.get("redraft_value", 0), x.get("dynasty_value", 0)), reverse=True)
    rookie_rank = next(i + 1 for i, p in enumerate(all_pos) if p["id"] == rookie["id"])

    # Decision
    starter_count = starter_counts.get(pos, 0)
    if rookie_rank <= math.ceil(starter_count):
        action = "STARTER"
    elif rookie_rank <= total_need:
        action = "ACTIVE_BENCH"
    elif taxi_eligible and open_taxi > 0:
        action = "TAXI"
    elif taxi_eligible and open_taxi == 0:
        action = "TAXI"  # taxi full, will need cascading moves
    else:
        action = "ACTIVE_BENCH"

    # Calculate cascading moves
    cascading_moves = []

    if action == "ACTIVE_BENCH" and roster_over > 0:
        # Cut lowest value non-starter non-IR active bench player
        cut_candidate = get_cut_candidate(sim_active, starter_ids)
        if cut_candidate:
            cascading_moves.append({
                "player_name": cut_candidate["name"],
                "player_id": cut_candidate["id"],
                "action": "CUT",
                "location": "active_bench"
            })

    elif action == "TAXI" and open_taxi == 0:
        # Find lowest value player across active bench and taxi combined
        all_candidates = []
        for p in sim_active.values():
            if p["id"] not in starter_ids and not p.get("on_ir"):
                all_candidates.append({**p, "location": "active_bench"})
        for p in sim_taxi.values():
            all_candidates.append({**p, "location": "taxi"})

        if all_candidates:
            cut_candidate = min(all_candidates, key=lambda x: (x.get("dynasty_value", 0)))
            cascading_moves.append({
                "player_name": cut_candidate["name"],
                "player_id": cut_candidate["id"],
                "action": "CUT",
                "location": cut_candidate["location"]
            })

            # If cut came from active bench, taxi still full, promote taxi player
            if cut_candidate["location"] == "active_bench":
                taxi_years = league_detail["settings"].get("taxi_years", 3)
                remaining_taxi = [
                    {**p, "remaining_years": taxi_years - p.get("years_exp", 0)}
                    for p in sim_taxi.values()
                    if p["id"] != cut_candidate["id"]
                ]
                if remaining_taxi:
                    promote_candidate = min(
                        remaining_taxi,
                        key=lambda x: (x["remaining_years"], -x.get("dynasty_value", 0))
                    )
                    cascading_moves.append({
                        "player_name": promote_candidate["name"],
                        "player_id": promote_candidate["id"],
                        "action": "PROMOTE_TO_BENCH",
                        "location": "taxi"
                    })

    return action, cascading_moves, rookie_rank, total_need

def get_cut_candidate(sim_active, starter_ids):
    bench_players = [
        p for p in sim_active.values()
        if p["id"] not in starter_ids and not p.get("on_ir")
    ]
    if not bench_players:
        return None
    return min(bench_players, key=lambda x: x.get("dynasty_value", 0))

def get_claude_reasoning(rookie, action, cascading_moves, rookie_rank, total_need, sim_active, sim_taxi, starter_ids, league_detail, players):
    taxi_slots_total = league_detail["settings"].get("taxi_slots", 0)
    open_taxi = taxi_slots_total - len(sim_taxi)
    pos = rookie["position"]

    pos_players = sorted(
        [p for p in sim_active.values() if p.get("position") == pos],
        key=lambda x: (x.get("redraft_value", 0), x.get("dynasty_value", 0)),
        reverse=True
    )

    prompt = f"""You are a dynasty fantasy football roster management expert. A placement decision has already been made for a drafted player. Write a concise 2-3 sentence explanation of why this decision makes sense.

DRAFTED PLAYER:
{json.dumps({
    "name": rookie["name"],
    "position": rookie["position"],
    "age": rookie["age"],
    "dynasty_value": rookie["dynasty_value"],
    "redraft_value": rookie["redraft_value"],
    "years_exp": rookie["years_exp"]
}, indent=2)}

DECISION: {action}

POSITION DEPTH CHART (sorted by redraft value):
{json.dumps([{"name": p["name"], "dynasty_value": p["dynasty_value"], "redraft_value": p["redraft_value"], "is_starter": p["id"] in starter_ids} for p in pos_players], indent=2)}

KEY FACTS:
- This player ranks #{rookie_rank} at {pos} by redraft value on this roster
- Total {pos} roster need (starters + backups): {total_need}
- Open taxi slots: {open_taxi}
- Taxi eligible: {rookie["years_exp"] == 0}
- Cascading moves required: {json.dumps([{"player": m["player_name"], "action": m["action"]} for m in cascading_moves])}

Write ONLY the reasoning as a plain string (2-3 sentences). No JSON, no preamble.
"""

    response = get_completion(prompt, system="You are a dynasty fantasy football expert. Write clear, concise reasoning in 2-3 sentences. No JSON, just plain text.")
    return response.strip()

def build_prompt(picks, available, my_roster, league_context, pick_number):
    top_available = sorted(
        [p for p in available.values() if "fc_value" in p],
        key=lambda x: x["fc_value"],
        reverse=True
    )[:20]

    available_summary = []
    for p in top_available:
        available_summary.append({
            "name": p.get("full_name"),
            "position": p.get("position"),
            "age": p.get("fc_age"),
            "dynasty_value": p.get("fc_value"),
            "redraft_value": p.get("fc_redraft_value"),
            "overall_rank": p.get("fc_overall_rank"),
            "position_rank": p.get("fc_position_rank"),
            "tier": p.get("fc_tier"),
            "trend_30_day": p.get("fc_trend")
        })

    my_players = list(league_context.get("my_existing_roster", []))

    roster_positions = league_context.get("roster_positions", [])
    te_slots = sum(1 for p in roster_positions if p == "TE")
    qb_slots = sum(1 for p in roster_positions if p in ["QB", "SUPER_FLEX"])
    taxi_open = league_context.get("taxi_slots_total", 0) - league_context.get("taxi_slots_used", 0)
    picks_remaining = league_context.get("picks_remaining_for_me", 0)
    bpa_player, suggested_pick, bpa_gap = calculate_bpa(available, league_context)
    vorp_players, replacement = calculate_vorp(available, league_context)
    prompt = f"""You are advising on pick {pick_number} in a dynasty rookie draft.

LEAGUE CONTEXT:
{json.dumps(league_context, indent=2)}

LEAGUE FORMAT: {"Dynasty" if league_context.get("is_dynasty") else "Redraft"}

{"Dynasty notes: Prioritize age, long-term value, and development potential. Taxi squad eligibility matters." if league_context.get("is_dynasty") else "Redraft notes: Prioritize immediate production and current season value. Ignore long-term dynasty upside."}

MY CURRENT ROSTER:
{json.dumps(my_players, indent=2)}

MY PICKS SO FAR THIS DRAFT:
{json.dumps(league_context.get("my_picks_this_draft", []), indent=2)}

TOP 20 AVAILABLE PLAYERS BY DYNASTY VALUE:
{json.dumps(available_summary, indent=2)}

TOP 10 AVAILABLE PLAYERS BY VORP (Value Over Replacement):
{json.dumps([{"name": v["player"].get("full_name"), "position": v["position"], "vorp": round(v["vorp"]), "dynasty_value": v["value"]} for v in sorted(vorp_players, key=lambda x: x["vorp"], reverse=True)[:10]], indent=2)}

TOTAL PICKS MADE SO FAR: {len(picks)}

IMPORTANT ROSTER CONSTRUCTION NOTES:
- This league has {te_slots} dedicated TE slot(s). {"TE is low priority unless elite." if te_slots == 0 else "TE has some value but is not premium."}
- This league has {qb_slots} QB-eligible slots including SUPER_FLEX. QB is elevated in value.
- Only recommend a TE if they are clearly the best available player by a significant margin.
- Prioritize QB, RB, and WR unless a TE represents exceptional value.
- You have {taxi_open} open taxi squad slots. Developmental rookies can be stashed there for up to {league_context.get("taxi_years")} years.
- You have {picks_remaining} picks remaining in this draft including this one.
- {"Taxi space is available so developmental stashes are viable." if taxi_open > 0 else "Taxi is full. Only draft players ready to contribute soon."}
ROSTER CONSTRUCTION DETAIL:
{json.dumps({
    pos: f"{d['dedicated_slots']} dedicated {pos} slot(s) + {d['flex_eligible']} flex slot(s) eligible for {pos}"
    for pos, d in league_context.get("roster_construction_detail", {}).items()
}, indent=2)}

PLAYERS ALREADY DRAFTED BY POSITION THIS DRAFT:
MY CURRENT STARTING LINEUP BY POSITION:
{json.dumps({pos: len([p for p in league_context.get("my_starters", []) if p["position"] == pos]) for pos in ["QB", "RB", "WR", "TE"]}, indent=2)}

NOTE: These are the actual players filling starter slots including flex. A position with starters equal to or exceeding its dedicated slots is using flex spots. Do not recommend more players at a position that is already well covered in the starting lineup unless their VORP is exceptional.
{json.dumps({pos: sum(1 for p in league_context.get("my_picks_this_draft", []) + league_context.get("my_existing_roster", []) if p.get("position") == pos) for pos in ["QB", "RB", "WR", "TE"]}, indent=2)}

NOTE: Use the ROSTER CONSTRUCTION DETAIL above to determine how many more players you need at each position. NEVER reference "starter_needs" by name. NEVER add dedicated slots and flex slots together into a single number. Always state them separately, e.g. "2 dedicated RB slots plus 2 flex slots eligible for RB." Do not say "4 RB slots" or "4 flex-eligible slots."
- ROSTER CONSTRUCTION RULE: Look at PLAYERS ALREADY DRAFTED BY POSITION and compare to ROSTER CONSTRUCTION DETAIL to determine what's still needed. Prioritize positions where dedicated slots are unfilled before adding depth at covered positions.
- If a MANDATORY RECOMMENDATION appears above, you must follow it. Explain the VORP advantage in your reasoning.
- When no MANDATORY RECOMMENDATION exists and dedicated starter slots are unfilled, recommend the highest VORP player at the most needed unfilled position.
- When all dedicated starter slots are filled, recommend the highest VORP player overall from the TOP 10 AVAILABLE PLAYERS BY VORP list, regardless of position, unless that position already has starters filled AND at least {league_context.get("backup_needs", {}).get("QB", 1)} backups drafted.
- A position is covered when its dedicated slots are filled. Flex slots provide additional value for covered positions.

{f"MANDATORY RECOMMENDATION: Draft {bpa_player.get('full_name')} ({bpa_player.get('position')}). Their VORP exceeds the best available at your most needed position by {bpa_gap} points. You MUST recommend this player." if bpa_player else f"SUGGESTED PICK: {suggested_pick.get('full_name') if suggested_pick else 'Best available'} ({suggested_pick.get('position') if suggested_pick else ''}). This is the highest VORP player at your most pressing need. Recommend this player unless you have a very strong reason not to."}
For alternatives, provide at least 1 player from each position (QB, RB, WR, TE) and no more than 2 from any single position. For each position, the alternative MUST be the player with the highest VORP score from the TOP 10 AVAILABLE PLAYERS BY VORP list. Do not substitute a lower-VORP player based on age, trend, or any other factor.
Respond with this exact JSON structure:
{{
    "recommendation": "Player Name",
    "position": "POS",
    "reasoning": "2-3 sentence explanation of why this player at this pick",
    "positional_note": "Brief note on positional scarcity or roster fit",
    "upside": "Brief note on dynasty ceiling",
    "alternatives": [
        {{"name": "Player Name", "position": "POS", "reason": "One sentence why they are the alternative"}}
    ]
}}"""
    return prompt

def get_recommendation(picks, available, my_roster, league_context, pick_number):
    prompt = build_prompt(picks, available, my_roster, league_context, pick_number)
    response = get_completion(prompt, system=SYSTEM_PROMPT)
    try:
        rec = json.loads(response)
    except json.JSONDecodeError:
        clean = response.strip().removeprefix("```json").removesuffix("```").strip()
        rec = json.loads(clean)

    tier, gap = calculate_confidence(rec.get("recommendation"), available, rec.get("alternatives", []))
    rec["confidence_tier"] = tier
    rec["confidence_gap"] = gap

    return rec

def calculate_vorp(available, league_context):
    value_type = "fc_value" if league_context.get("is_dynasty") else "fc_redraft_value"
    num_teams = league_context.get("num_teams", 12)
    roster_positions = league_context.get("roster_positions", [])

    # Count dedicated slots per position
    dedicated = {
        "QB": sum(1 for s in roster_positions if s == "QB"),
        "RB": sum(1 for s in roster_positions if s == "RB"),
        "WR": sum(1 for s in roster_positions if s == "WR"),
        "TE": sum(1 for s in roster_positions if s == "TE"),
    }

    # Count flex slots and their eligible positions
    flex_slots = {
        "FLEX": {"RB", "WR", "TE"},
        "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
        "WRRB_FLEX": {"RB", "WR"},
        "REC_FLEX": {"WR", "TE"},
    }

    # For each flex slot type, find replacement level
    flex_replacement = {"QB": 0, "RB": 0, "WR": 0, "TE": 0}
    
    for slot_type, eligible_positions in flex_slots.items():
        slot_count = sum(1 for s in roster_positions if s == slot_type) * num_teams
        if slot_count == 0:
            continue
        
        # Pool all available players eligible for this slot
        pool = sorted(
            [p for p in available.values() 
             if p.get("position") in eligible_positions and p.get(value_type, 0)],
            key=lambda x: x.get(value_type, 0),
            reverse=True
        )
        
        # Replacement = value of player just outside the starting pool
        if len(pool) > slot_count:
            replacement_val = pool[slot_count].get(value_type, 0)
        elif pool:
            replacement_val = pool[-1].get(value_type, 0)
        else:
            replacement_val = 0
        
        # Update flex replacement for each eligible position
        for pos in eligible_positions:
            flex_replacement[pos] = max(flex_replacement[pos], replacement_val)

    # Positional replacement level = (dedicated_slots * num_teams + 1)th best player
    positional_replacement = {}
    for pos in ["QB", "RB", "WR", "TE"]:
        pos_players = sorted(
            [p for p in available.values() 
             if p.get("position") == pos and p.get(value_type, 0)],
            key=lambda x: x.get(value_type, 0),
            reverse=True
        )
        cutoff = dedicated[pos] * num_teams
        if len(pos_players) > cutoff:
            positional_replacement[pos] = pos_players[cutoff].get(value_type, 0)
        elif pos_players:
            positional_replacement[pos] = pos_players[-1].get(value_type, 0)
        else:
            positional_replacement[pos] = 0

    # Final replacement = max of positional and flex replacement
    replacement = {
        pos: max(positional_replacement.get(pos, 0), flex_replacement.get(pos, 0))
        for pos in ["QB", "RB", "WR", "TE"]
    }

    # Calculate VORP for each available player
    vorp_players = []
    for p in available.values():
        pos = p.get("position", "?")
        val = p.get(value_type, 0)
        if pos in replacement and val:
            vorp = val - replacement[pos]
            vorp_players.append({
                "player": p,
                "vorp": vorp,
                "value": val,
                "position": pos
            })

    return vorp_players, replacement

def calculate_bpa(available, league_context):
    value_type = "fc_value" if league_context.get("is_dynasty") else "fc_redraft_value"
    threshold = league_context.get("bpa_threshold", 1000)
    starter_needs = league_context.get("starter_needs", {})
    backup_needs = league_context.get("backup_needs", {})

    vorp_players, replacement = calculate_vorp(available, league_context)
    if not vorp_players:
        return None, None, None

    # Count picks already made by position (this draft + existing roster)
    picks_by_pos = {}
    for p in league_context.get("my_picks_this_draft", []) + league_context.get("my_existing_roster", []):
        pos = p.get("position", "?")
        picks_by_pos[pos] = picks_by_pos.get(pos, 0) + 1

    # Dedicated starter counts
    dedicated = {
        "QB": sum(1 for s in league_context.get("roster_positions", []) if s == "QB"),
        "RB": sum(1 for s in league_context.get("roster_positions", []) if s == "RB"),
        "WR": sum(1 for s in league_context.get("roster_positions", []) if s == "WR"),
        "TE": sum(1 for s in league_context.get("roster_positions", []) if s == "TE"),
    }

    # PHASE 1: Unfilled starter slots
    needed_positions = [pos for pos, need in starter_needs.items() if need > 0]
    if needed_positions:
        best_needed_vorp = max(
            (v for v in vorp_players if v["position"] in needed_positions),
            key=lambda x: x["vorp"],
            default=None
        )
        if not best_needed_vorp:
            return None, None, None

        best_overall_vorp = max(vorp_players, key=lambda x: x["vorp"])
        gap = best_overall_vorp["vorp"] - best_needed_vorp["vorp"]

        if gap > threshold and best_overall_vorp["position"] not in needed_positions:
            return best_overall_vorp["player"], best_needed_vorp["player"], gap
        return None, best_needed_vorp["player"], gap

    # PHASE 2: Unfilled backup slots
    backup_unfilled = {
        pos: max(0, dedicated.get(pos, 0) + backup_needs.get(pos, 0) - picks_by_pos.get(pos, 0))
        for pos in ["QB", "RB", "WR", "TE"]
    }
    needed_backup_positions = [pos for pos, need in backup_unfilled.items() if need > 0]

    if needed_backup_positions:
        best_needed_vorp = max(
            (v for v in vorp_players if v["position"] in needed_backup_positions),
            key=lambda x: x["vorp"],
            default=None
        )
        if not best_needed_vorp:
            return None, None, None

        best_overall_vorp = max(vorp_players, key=lambda x: x["vorp"])
        gap = best_overall_vorp["vorp"] - best_needed_vorp["vorp"]

        if gap > threshold and best_overall_vorp["position"] not in needed_backup_positions:
            return best_overall_vorp["player"], best_needed_vorp["player"], gap
        return None, best_needed_vorp["player"], gap

    # PHASE 3: All starters and backups filled - taxi territory
    positive_vorp = [v for v in vorp_players if v["vorp"] > 0]
    if positive_vorp:
        best = max(positive_vorp, key=lambda x: x["vorp"])
        return None, best["player"], 0
    return None, None, None

def calculate_confidence(recommendation_name, available, alternatives):
    ranked = sorted(
        [p for p in available.values() if "fc_value" in p],
        key=lambda x: x["fc_value"],
        reverse=True
    )

    if len(ranked) < 2:
        return "high", None

    top = ranked[0]
    second = ranked[1]
    gap = top.get("fc_value", 0) - second.get("fc_value", 0)

    if gap >= 300:
        tier = "high"
    elif gap >= 100:
        tier = "medium"
    else:
        tier = "low"

    return tier, gap


def get_roster_recommendations(my_roster, players, league_detail, my_draft_picks, starter_ids):
    from sleeper_league import get_taxi_players

    taxi_ids = set(get_taxi_players(my_roster))
    reserve_ids = set(my_roster.get("reserve") or [])

    active_ids = set(my_roster.get("players") or [])
    active_ids = active_ids - taxi_ids

    roster_positions = league_detail.get("roster_positions", [])
    roster_max = len(roster_positions) + len(reserve_ids)

    def enrich(pid):
        p = players.get(pid, {})
        if not p:
            return None
        return {
            "id": pid,
            "name": p.get("full_name", "Unknown"),
            "position": p.get("position", "?"),
            "age": p.get("fc_age") or p.get("age", "?"),
            "dynasty_value": p.get("fc_value", 0) if isinstance(p.get("fc_value"), int) else 0,
            "redraft_value": p.get("fc_redraft_value", 0) if isinstance(p.get("fc_redraft_value"), int) else 0,
            "years_exp": p.get("years_exp", 99),
            "on_ir": pid in reserve_ids
        }

    sim_taxi = {p["id"]: p for p in [enrich(pid) for pid in taxi_ids] if p}
    sim_active = {p["id"]: p for p in [enrich(pid) for pid in active_ids] if p}

    new_rookies = []
    for pid in (my_draft_picks or []):
        p = enrich(pid)
        if p:
            new_rookies.append(p)

    recommendations = []

    for rookie in new_rookies:
        action, cascading_moves, rookie_rank, total_need = decide_placement(
            rookie, sim_active, sim_taxi, league_detail, players, reserve_ids, starter_ids
        )
        reasoning = get_claude_reasoning(
            rookie, action, cascading_moves, rookie_rank, total_need,
            sim_active, sim_taxi, starter_ids, league_detail, players
        )

        # Format cascading moves for display
        display_moves = [
            {
                "player_name": m["player_name"],
                "action": m["action"],
                "reason": f"{'Lowest value player across active bench and taxi' if m['action'] == 'CUT' else 'Fewest remaining taxi years'}"
            }
            for m in cascading_moves
        ]

        recommendations.append({
            "player": rookie["name"],
            "position": rookie["position"],
            "action": action,
            "reasoning": reasoning,
            "cascading_moves": display_moves,
            "severity": {
                "STARTER": "success",
                "ACTIVE_BENCH": "info",
                "TAXI": "info",
                "CUT": "error"
            }.get(action, "info")
        })

        # Update sim state
        if action == "TAXI":
            sim_taxi[rookie["id"]] = rookie
        elif action in ("STARTER", "ACTIVE_BENCH"):
            sim_active[rookie["id"]] = rookie

        # Apply cascading moves
        for move in cascading_moves:
            pid = move.get("player_id")
            move_action = move.get("action")
            if pid:
                if move_action == "CUT":
                    sim_active.pop(pid, None)
                    sim_taxi.pop(pid, None)
                elif move_action == "TAXI":
                    matched = sim_active.pop(pid, None) or sim_taxi.get(pid)
                    if matched:
                        sim_taxi[pid] = matched
                elif move_action == "PROMOTE_TO_BENCH":
                    matched = sim_taxi.pop(pid, None)
                    if matched:
                        sim_active[pid] = matched

    return recommendations, sim_active, sim_taxi