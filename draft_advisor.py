import json
import math
from llm_client import get_completion
from config import DEV_MODE
from config import TAXI_THRESHOLD_QB, TAXI_THRESHOLD_RB, TAXI_THRESHOLD_WR, TAXI_THRESHOLD_TE, REDRAFT_THRESHOLD_QB, REDRAFT_THRESHOLD_RB, REDRAFT_THRESHOLD_WR, REDRAFT_THRESHOLD_TE

def get_system_prompt(is_dynasty=True):
    if is_dynasty:
        return """You are an expert dynasty fantasy football analyst and draft advisor for the 2026 NFL season.
You give vivid, specific, confident recommendations that sound like advice from a knowledgeable friend who watches film and follows the league closely.
Focus on long-term player value, age curves, positional scarcity, and dynasty upside.
Use the team, team_qb1, depth_chart_order, and other teammate fields provided to mention specific offensive context, target share, and role clarity.
Write like a dynasty analyst on a podcast — specific, enthusiastic, and grounded in real situation awareness.
Prioritize age, long-term value, and development potential. Taxi squad eligibility matters.
Do not make up information not provided. If team_qb1 is provided use it. If depth_chart_order is 1, say they are the starter.
CRITICAL: Always reference the user's specific roster by player name in your reasoning. Explain how this pick fills a specific gap, complements an existing player, or why the value justifies taking it over a positional need. For example: 'You already have Allen locked in at QB1 so Williams gives you a high-upside taxi stash' or 'Your RB room is thin with only Jeanty — Price fills that need directly.' Never give generic reasoning that could apply to any team.
Always base your recommendations on the actual league settings provided, including roster construction and scoring format.
Always respond in valid JSON only. No preamble, no markdown, no explanation outside the JSON."""
    else:
        return """You are an expert redraft fantasy football analyst and draft advisor for the 2026 NFL season.
You give vivid, specific, confident recommendations that sound like advice from a knowledgeable friend who watches film and follows the league closely.
Focus on current season production, role, opportunity, and offensive context.
Use the team and team_qb1 fields provided to mention specific teammates, offensive schemes, and target share context.
Write like a fantasy analyst on a podcast — specific, enthusiastic, and grounded in real situation.
Ignore dynasty value and long-term upside — every comment should be about winning your league THIS season.
Do not make up information not provided. If team_qb1 is provided use it. If depth_chart_order is 1, say they are the starter.
CRITICAL: Always reference the user's specific roster by player name in your reasoning. Explain how this pick fills a specific gap or complements what they already have. For example: 'You have Bijan locked in at RB1 but no RB2 — Price gives you a legit handcuff with upside' or 'Your WR room is set with Nabers and Smith so this is pure depth.' Never give generic reasoning that could apply to any team.
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

def build_prompt(picks, available, my_roster, league_context, pick_number, all_players=None):
    taxi_thresholds = {
        "QB": TAXI_THRESHOLD_QB,
        "RB": TAXI_THRESHOLD_RB,
        "WR": TAXI_THRESHOLD_WR,
        "TE": TAXI_THRESHOLD_TE,
    }
    # Build team context lookup
    team_qb1 = {}
    team_rb1 = {}
    team_wr1 = {}
    team_te1 = {}
    if all_players:
        for p in all_players.values():
            team = p.get("team", "")
            pos = p.get("position", "")
            order = p.get("depth_chart_order")
            if not team or order != 1:
                continue
            if pos == "QB":
                team_qb1[team] = p.get("full_name")
            elif pos == "RB":
                team_rb1[team] = p.get("full_name")
            elif pos == "WR":
                team_wr1[team] = p.get("full_name")
            elif pos == "TE":
                team_te1[team] = p.get("full_name")
    if league_context.get("is_dynasty"):
        top_available = sorted(
            [p for p in available.values() if "fc_overall_rank" in p],
            key=lambda x: x["fc_overall_rank"]
        )[:20]
    else:
        top_available = sorted(
            [p for p in available.values() if "fc_redraft_value" in p],
            key=lambda x: x.get("fc_redraft_value", 0),
            reverse=True
        )[:20]

    available_summary = []
    for p in top_available:
        team = p.get("team", "")
        entry = {
            "name": p.get("full_name"),
            "position": p.get("position"),
            "team": team,
            "depth_chart_order": p.get("depth_chart_order"),
            "age": p.get("fc_age"),
            "dynasty_value": p.get("fc_value"),
            "redraft_value": p.get("fc_redraft_value"),
            "overall_rank": p.get("fc_overall_rank"),
            "position_rank": p.get("fc_position_rank"),
            "tier": p.get("fc_tier"),
            "trend_30_day": p.get("fc_trend")
        }
        if team:
            if team in team_qb1:
                entry["team_qb1"] = team_qb1[team]
            if team in team_rb1 and p.get("position") != "RB":
                entry["team_rb1"] = team_rb1[team]
            if team in team_wr1 and p.get("position") != "WR":
                entry["team_wr1"] = team_wr1[team]
            if team in team_te1 and p.get("position") != "TE":
                entry["team_te1"] = team_te1[team]
        available_summary.append(entry)

    my_players = list(league_context.get("my_existing_roster", []))

    roster_positions = league_context.get("roster_positions", [])
    te_slots = sum(1 for p in roster_positions if p == "TE")
    qb_slots = sum(1 for p in roster_positions if p in ["QB", "SUPER_FLEX"])
    is_dynasty = league_context.get("is_dynasty", True)
    taxi_total = league_context.get("taxi_slots_total", 0) or 0
    taxi_picks = sum(1 for p in league_context.get("my_picks_this_draft", []) + league_context.get("my_existing_roster", [])
                    if (p.get("redraft_value", 0) or 0) < taxi_thresholds.get(p.get("position", "?"), 100)
                    and (p.get("years_exp", 99) == 0 or league_context.get("taxi_allow_vets", 0) == 1))
    taxi_open = max(0, taxi_total - taxi_picks) if is_dynasty else 0
    picks_remaining = league_context.get("picks_remaining_for_me", 0)
    bpa_player, suggested_pick, bpa_gap = calculate_bpa(available, league_context, all_players)
    if DEV_MODE:
        rep = calculate_redraft_replacement(league_context, all_players)
        print(f"redraft_replacement in build_prompt: {rep}")
        vorp_debug, rep_debug = calculate_vorp(available, league_context, all_players)
        top5 = sorted(vorp_debug, key=lambda x: x['vorp'], reverse=True)[:5]
        print(f"Top 5 VORP: {[(v['player'].get('full_name'), v['position'], round(v['vorp'])) for v in top5]}")
        best_te = sorted([p for p in available.values() if p.get('position') == 'TE' and p.get('fc_overall_rank')], key=lambda x: x['fc_overall_rank'])
        if best_te:
            print(f"Best available TE: {best_te[0].get('full_name')}, rank: {best_te[0].get('fc_overall_rank')}")
        best_wr = sorted([p for p in available.values() if p.get('position') == 'WR' and p.get('fc_overall_rank')], key=lambda x: x['fc_overall_rank'])
        if best_wr:
            print(f"Best available WR: {best_wr[0].get('full_name')}, rank: {best_wr[0].get('fc_overall_rank')}")
        best_rb = sorted([p for p in available.values() if p.get('position') == 'RB' and p.get('fc_overall_rank')], key=lambda x: x['fc_overall_rank'])
        if best_rb:
            print(f"Best available RB: {best_rb[0].get('full_name')}, rank: {best_rb[0].get('fc_overall_rank')}")
        best_qb = sorted([p for p in available.values() if p.get('position') == 'QB' and p.get('fc_overall_rank')], key=lambda x: x['fc_overall_rank'])
        if best_qb:
            print(f"Best available QB: {best_qb[0].get('full_name')}, rank: {best_qb[0].get('fc_overall_rank')}")    
    vorp_players, replacement = calculate_vorp(available, league_context, all_players)
    # Count picks already made by position (this draft + existing roster)
    
    prompt = f"""You are advising on pick {pick_number} in a dynasty rookie draft.

LEAGUE CONTEXT:
{json.dumps(league_context, indent=2)}

LEAGUE FORMAT: {"Dynasty" if league_context.get("is_dynasty") else "Redraft"}
SEASON: 2026

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
- You have {picks_remaining} picks remaining in this draft including this one.
{f"- You have {taxi_open} open taxi squad slots. Developmental rookies can be stashed there for up to {league_context.get('taxi_years')} years." if is_dynasty else "- This is a REDRAFT league. Every player must contribute this season. Do not consider dynasty value or long-term upside."}
{f"- {'Taxi space is available so developmental stashes are viable.' if taxi_open > 0 else 'Taxi is full. Only draft players ready to contribute soon.'}" if is_dynasty else ""}
{"- K and DST should be drafted in the final rounds based on schedule matchups. Do not recommend K or DST until all skill position needs are filled." if not is_dynasty else ""}
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

{f"MANDATORY RECOMMENDATION: Draft {bpa_player.get('full_name')} ({bpa_player.get('position')}). Their VORP exceeds the best available at your most needed position by {bpa_gap} points. You MUST recommend this player." if bpa_player else (f"SUGGESTED PICK: {suggested_pick.get('full_name')} ({suggested_pick.get('position')}). This is the highest VORP player at your most pressing need. Recommend this player unless you have a very strong reason not to." if suggested_pick else "NO STRONG RECOMMENDATION: Your roster is at capacity. All remaining available players are below replacement level or would be cut. Consider skipping this pick or taking the highest dynasty value player available for trade bait.")}
For alternatives, provide at least 1 player from each position (QB, RB, WR, TE) and no more than 2 from any single position. For each position, the alternative MUST be the player with the highest VORP score from the TOP 10 AVAILABLE PLAYERS BY VORP list who is also taxi-eligible (years_exp = 0) if we are in taxi territory (all starter and backup slots filled). Do not suggest veterans with no taxi eligibility as alternatives in late rounds.
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

def get_recommendation(picks, available, my_roster, league_context, pick_number, all_players=None):
    prompt = build_prompt(picks, available, my_roster, league_context, pick_number, all_players)
    is_dynasty = league_context.get("is_dynasty", True)
    response = get_completion(prompt, system=get_system_prompt(is_dynasty))
    try:
        rec = json.loads(response)
    except json.JSONDecodeError:
        clean = response.strip().removeprefix("```json").removesuffix("```").strip()
        rec = json.loads(clean)

    is_dynasty = league_context.get("is_dynasty", True)
    tier, gap = calculate_confidence(rec.get("recommendation"), available, rec.get("alternatives", []), is_dynasty)
    rec["confidence_tier"] = tier
    rec["confidence_gap"] = gap

    return rec

def calculate_vorp(available, league_context, all_players=None):
    
    value_type = "fc_value" if league_context.get("is_dynasty") else "fc_redraft_value"
    is_dynasty = league_context.get("is_dynasty", True)
    if DEV_MODE and not is_dynasty:
        print(f"calculate_vorp: all_players={all_players is not None}, available size={len(available)}")
    if is_dynasty:
        max_rank = max((p.get("fc_overall_rank", 999) for p in available.values() if p.get("fc_overall_rank")), default=999)
        vorp_players = []
        for p in available.values():
            rank = p.get("fc_overall_rank")
            if rank:
                vorp = max_rank - rank
                vorp_players.append({
                    "player": p,
                    "vorp": vorp,
                    "value": p.get("fc_value", 0),
                    "position": p.get("position", "?")
                })
        return vorp_players, {}
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
        player_pool = all_players if all_players else available
        pool = sorted(
            [p for p in player_pool.values() 
             if p.get("position") in eligible_positions and p.get(value_type, 0)],
            key=lambda x: x.get(value_type, 0),
            reverse=True
        )
        
        # Account for dedicated starters already filling non-flex slots
        dedicated_starters = sum(dedicated.get(pos, 0) for pos in eligible_positions) * num_teams
        adjusted_cutoff = slot_count + dedicated_starters
        if len(pool) > adjusted_cutoff:
            replacement_val = pool[adjusted_cutoff].get(value_type, 0)
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
        player_pool = all_players if all_players else available
        pos_players = sorted(
            [p for p in player_pool.values() 
             if p.get("position") == pos and p.get(value_type, 0)],
            key=lambda x: x.get(value_type, 0),
            reverse=True
        )
        cutoff = int(dedicated[pos] * num_teams)
        if len(pos_players) > cutoff:
            positional_replacement[pos] = pos_players[cutoff].get(value_type, 0)
        elif pos_players:
            positional_replacement[pos] = pos_players[-1].get(value_type, 0)
        else:
            positional_replacement[pos] = 0

    # Final replacement:
    # - Positions with dedicated slots: max of positional and flex replacement
    # - Positions with NO dedicated slots: flex replacement only (must compete for flex)
    replacement = {
        pos: flex_replacement.get(pos, 0) if dedicated.get(pos, 0) == 0
        else max(positional_replacement.get(pos, 0), flex_replacement.get(pos, 0))
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

def simulate_placement(candidate, sim_active, sim_taxi, league_context):
    """
    Simulate where a candidate player would land on the roster.
    Returns (placement, cut_candidate) where placement is one of:
    STARTER, ACTIVE_BENCH, TAXI, CUT
    and cut_candidate is the player who would be cut to make room (or None)
    """
    roster_positions = league_context.get("roster_positions", [])
    taxi_slots_total = league_context.get("taxi_slots_total", 0) or 0
    taxi_allow_vets = league_context.get("taxi_allow_vets", 0)
    
    # Calculate active roster capacity (all slots except taxi)
    active_capacity = sum(1 for s in roster_positions if s not in ["K", "DEF"])
    
    taxi_eligible = candidate.get("years_exp", 99) == 0 or taxi_allow_vets == 1
    
    # Build sorted active roster by redraft value descending
    all_active = sorted(
        list(sim_active.values()) + [candidate],
        key=lambda x: x.get("redraft_value", 0),
        reverse=True
    )
    
    # Find candidate's rank in active roster
    candidate_rank = next(
        i + 1 for i, p in enumerate(all_active) 
        if p.get("id") == candidate.get("id")
    )
    
    if candidate_rank <= active_capacity:
        # Candidate fits on active roster
        if len(sim_active) < active_capacity:
            # Active roster not full, no cuts needed
            if candidate_rank == 1:
                return "STARTER", None
            return "ACTIVE_BENCH", None
        else:
            # Active roster full, someone gets bumped
            bumped = all_active[active_capacity]  # player just outside active capacity
            if bumped.get("id") == candidate.get("id"):
                # Candidate is the one being bumped, shouldn't happen here
                pass
            # Bumped player goes to taxi if eligible, otherwise cut
            bumped_taxi_eligible = bumped.get("years_exp", 99) == 0 or taxi_allow_vets == 1
            if bumped_taxi_eligible and len(sim_taxi) < taxi_slots_total:
                return "ACTIVE_BENCH", None  # bumped goes to taxi, no cut
            else:
                return "ACTIVE_BENCH", bumped  # bumped gets cut
    else:
        # Candidate doesn't fit on active roster
        if taxi_eligible and len(sim_taxi) < taxi_slots_total:
            return "TAXI", None
        elif taxi_eligible and len(sim_taxi) >= taxi_slots_total:
            # Taxi full during draft - can still add if better dynasty value than worst taxi player
            if not sim_taxi:
                return "CUT", None
            lowest_taxi = min(sim_taxi.values(), key=lambda x: x.get("dynasty_value", 0))
            if candidate.get("dynasty_value", 0) > lowest_taxi.get("dynasty_value", 0):
                return "TAXI", None  # no cut during draft, just note taxi is over capacity
            else:
                return "CUT", None
        else:
            return "CUT", None

def calculate_redraft_replacement(league_context, all_players):
    """Calculate redraft replacement levels for positional need assessment."""
    num_teams = league_context.get("num_teams", 12)
    roster_positions = league_context.get("roster_positions", [])
    flex_slots = {
        "FLEX": {"RB", "WR", "TE"},
        "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
        "WRRB_FLEX": {"RB", "WR"},
        "REC_FLEX": {"WR", "TE"},
    }
    dedicated = {
        "QB": sum(1 for s in roster_positions if s == "QB"),
        "RB": sum(1 for s in roster_positions if s == "RB"),
        "WR": sum(1 for s in roster_positions if s == "WR"),
        "TE": sum(1 for s in roster_positions if s == "TE"),
    }
    replacement = {}
    for pos in ["QB", "RB", "WR", "TE"]:
        flex_count = sum(
            sum(1 for s in roster_positions if s == slot_type)
            for slot_type, eligible in flex_slots.items()
            if pos in eligible
        )
        cutoff = int((dedicated[pos] + flex_count) * num_teams)
        player_pool = all_players if all_players else {}
        pos_players = sorted(
            [p for p in player_pool.values() if p.get("position") == pos and p.get("fc_redraft_value", 0)],
            key=lambda x: x.get("fc_redraft_value", 0),
            reverse=True
        )
        if len(pos_players) > cutoff:
            replacement[pos] = pos_players[cutoff].get("fc_redraft_value", 0)
        elif pos_players:
            replacement[pos] = pos_players[-1].get("fc_redraft_value", 0)
        else:
            replacement[pos] = 0
    return replacement

def calculate_bpa(available, league_context, all_players=None):
    value_type = "fc_value" if league_context.get("is_dynasty") else "fc_redraft_value"
    threshold = league_context.get("bpa_threshold", 1000)
    if DEV_MODE:
        print(f"bpa_threshold from context: {league_context.get('bpa_threshold')}, threshold: {threshold}")

    vorp_players, replacement = calculate_vorp(available, league_context, all_players)
    if not vorp_players:
        return None, None, None

    # Build sim state from existing roster + draft picks
    all_picks = (
        league_context.get("my_picks_this_draft", []) + 
        league_context.get("my_existing_roster", [])
    )
    if DEV_MODE:
        print(f"my_picks_this_draft: {len(league_context.get('my_picks_this_draft', []))}")
        print(f"my_existing_roster: {len(league_context.get('my_existing_roster', []))}")
        print(f"all_picks total: {len(all_picks)}")
    
    roster_positions = league_context.get("roster_positions", [])
    taxi_slots_total = league_context.get("taxi_slots_total", 0) or 0
    taxi_allow_vets = league_context.get("taxi_allow_vets", 0)
    active_capacity = sum(1 for s in roster_positions if s not in ["K", "DEF"])

    # Build redraft replacement levels to identify taxi candidates
    redraft_rep = calculate_redraft_replacement(league_context, all_players)
    
    sim_active = {}
    sim_taxi = {}
    
    taxi_thresholds = {
        "QB": TAXI_THRESHOLD_QB,
        "RB": TAXI_THRESHOLD_RB,
        "WR": TAXI_THRESHOLD_WR,
        "TE": TAXI_THRESHOLD_TE,
    }

    for p in all_picks:
        pid = p.get("id", p.get("name"))
        if not pid:
            continue
        pos = p.get("position", "?")
        redraft_val = p.get("redraft_value", 0) or 0
        taxi_eligible = p.get("years_exp", 99) == 0 or taxi_allow_vets == 1
        pos_threshold = taxi_thresholds.get(pos, 100)
        if redraft_val >= pos_threshold:
            # Above taxi threshold - goes to active roster
            sim_active[pid] = p
        elif taxi_eligible and len(sim_taxi) < taxi_slots_total:
            # Below threshold and taxi eligible - goes to taxi
            sim_taxi[pid] = p
        elif taxi_eligible and len(sim_taxi) >= taxi_slots_total:
            # Taxi full - goes to active bench
            sim_active[pid] = p
        else:
            # Below threshold, not taxi eligible - active bench
            sim_active[pid] = p
    if DEV_MODE:
        print(f"active_capacity: {active_capacity}, taxi_slots_total: {taxi_slots_total}")
        print(f"sim_active count: {len(sim_active)}")
        print(f"After sim build - sim_active: {len(sim_active)}, sim_taxi: {len(sim_taxi)}")
        beck = next((p for p in sim_active.values() if p.get('name') == 'Carson Beck'), None)
        beck_taxi = next((p for p in sim_taxi.values() if p.get('name') == 'Carson Beck'), None)
        print(f"Beck in sim_active: {beck is not None}, Beck in sim_taxi: {beck_taxi is not None}")
        print(f"Beck redraft: {beck.get('redraft_value') if beck else beck_taxi.get('redraft_value') if beck_taxi else 'not found'}")
        qbs = [p for p in sim_active.values() if p.get('position') == 'QB']
        print(f"QBs in sim: {[(p.get('name'), p.get('redraft_value')) for p in qbs]}")
        print(f"sim_taxi players: {[(p.get('name'), p.get('redraft_value')) for p in sim_taxi.values()]}")
        print(f"sim_active QBs: {[(p.get('name'), p.get('redraft_value')) for p in sim_active.values() if p.get('position') == 'QB']}")
    # Sort vorp_players by vorp descending
    sorted_vorp = sorted(
        [v for v in vorp_players if v["position"] not in ["K", "DEF"]],
        key=lambda x: x["vorp"],
        reverse=True
    )

    # Filter out players who would be CUT with no room
    viable = []
    for v in sorted_vorp:
        player = v["player"]
        candidate = {
            "id": player.get("sleeper_id") or player.get("full_name"),
            "name": player.get("full_name"),
            "position": player.get("position"),
            "dynasty_value": player.get("fc_value", 0),
            "redraft_value": player.get("fc_redraft_value", 0),
            "years_exp": player.get("years_exp", 99),
            "overall_rank": player.get("fc_overall_rank", 999),
        }
        placement, cut_candidate = simulate_placement(candidate, sim_active, sim_taxi, league_context)
        if placement == "CUT":
            continue
        viable.append(v)

    if DEV_MODE:
        print(f"sim_taxi count: {len(sim_taxi)}, taxi_slots_total: {taxi_slots_total}")
        print(f"viable count: {len(viable)}")
        print(f"top 3 viable: {[(v['player'].get('full_name'), v['position'], round(v['vorp'])) for v in viable[:3]]}")
    if not viable:
        # No viable picks - all remaining players would be cut or are worse than existing roster
        # Return the highest VORP player anyway so Claude can explain why options are limited
        if vorp_players:
            best = max(vorp_players, key=lambda x: x["vorp"])
            return None, best["player"], 0
        return None, None, None

    is_dynasty = league_context.get("is_dynasty", True)
    if not is_dynasty:
        # Redraft mode - no taxi, just positional need and BPA
        backup_needs = league_context.get("backup_needs", {})
        redraft_thresholds = {
            "QB": REDRAFT_THRESHOLD_QB,
            "RB": REDRAFT_THRESHOLD_RB,
            "WR": REDRAFT_THRESHOLD_WR,
            "TE": REDRAFT_THRESHOLD_TE,
        }
        picks_by_pos = {}
        for pid, p in sim_active.items():
            pos = p.get("position", "?")
            redraft_val = p.get("redraft_value", 0) or 0
            if redraft_val >= redraft_thresholds.get(pos, 500):
                picks_by_pos[pos] = picks_by_pos.get(pos, 0) + 1
        dedicated = {
            "QB": sum(1 for s in roster_positions if s in ["QB", "SUPER_FLEX"]),
            "RB": sum(1 for s in roster_positions if s in ["RB", "FLEX", "WRRB_FLEX"]),
            "WR": sum(1 for s in roster_positions if s in ["WR", "FLEX", "REC_FLEX", "WRRB_FLEX"]),
            "TE": sum(1 for s in roster_positions if s in ["TE", "FLEX", "REC_FLEX"]),
        }
        # In redraft, QB backup has no lineup value in 1QB leagues
        # Only need backup QB if there are SUPER_FLEX slots
        has_superflex = any(s == "SUPER_FLEX" for s in roster_positions)
        effective_backup_needs = dict(backup_needs)
        if not has_superflex:
            effective_backup_needs["QB"] = 0

        needed_positions = [
            pos for pos in ["QB", "RB", "WR", "TE"]
            if picks_by_pos.get(pos, 0) < dedicated.get(pos, 0) + effective_backup_needs.get(pos, 0)
        ]
        if DEV_MODE:
            print(f"redraft needed_positions: {needed_positions}")
            print(f"redraft picks_by_pos: {picks_by_pos}")
            print(f"redraft dedicated: {dedicated}")
            print(f"redraft effective_backup_needs: {effective_backup_needs}")
        # In redraft, prioritize positions with most slots still unfilled
        position_deficit = {
            pos: dedicated.get(pos, 0) + effective_backup_needs.get(pos, 0) - picks_by_pos.get(pos, 0)
            for pos in needed_positions
        }
        most_needed_pos = max(position_deficit, key=lambda p: position_deficit[p]) if position_deficit else None
        if DEV_MODE:
            print(f"position_deficit: {position_deficit}")
            print(f"most_needed_pos: {most_needed_pos}")
            print(f"top 5 viable: {[(v['player'].get('full_name'), v['position'], round(v['vorp'])) for v in viable[:5]]}")
        best_needed = next(
            (v for v in viable if v["position"] == most_needed_pos),
            None
        ) if most_needed_pos else None
        if best_needed is None:
            best_needed = next(
                (v for v in viable if v["position"] in needed_positions),
                None
            )
        # Exclude positions already at capacity from BPA consideration
        at_capacity_positions = [
            pos for pos in ["QB", "RB", "WR", "TE"]
            if picks_by_pos.get(pos, 0) >= dedicated.get(pos, 0) + effective_backup_needs.get(pos, 0)
        ]
        if DEV_MODE:
            print(f"at_capacity_positions: {at_capacity_positions}")
        best_overall = next(
            (v for v in viable if v["position"] not in at_capacity_positions),
            viable[0] if viable else None
        )
        if DEV_MODE:
            print(f"best_overall: {best_overall['player'].get('full_name') if best_overall else None}, vorp: {best_overall['vorp'] if best_overall else None}")
            print(f"best_needed: {best_needed['player'].get('full_name') if best_needed else None}, vorp: {best_needed['vorp'] if best_needed else None}")
            print(f"gap: {best_overall['vorp'] - best_needed['vorp'] if best_overall and best_needed else None}, threshold: {threshold}")
        if not best_overall:
            return None, None, None
        if best_needed is None:
            return None, best_overall["player"], 0
        gap = best_overall["vorp"] - best_needed["vorp"]
        if gap > threshold and best_overall["player"].get("full_name") != best_needed["player"].get("full_name"):
            return best_overall["player"], best_needed["player"], gap
        return None, best_needed["player"], gap
    taxi_full = len(sim_taxi) >= taxi_slots_total

    # Find positional needs - only count players with real redraft value
    backup_needs = league_context.get("backup_needs", {})
    
    picks_by_pos = {}
    for pid, p in sim_active.items():
        pos = p.get("position", "?")
        redraft_val = p.get("redraft_value", 0) or 0
        pos_threshold = taxi_thresholds.get(pos, 100)
        if redraft_val >= pos_threshold:
            picks_by_pos[pos] = picks_by_pos.get(pos, 0) + 1

    dedicated = {
        "QB": sum(1 for s in roster_positions if s in ["QB", "SUPER_FLEX"]),
        "RB": sum(1 for s in roster_positions if s in ["RB", "FLEX", "WRRB_FLEX"]),
        "WR": sum(1 for s in roster_positions if s in ["WR", "FLEX", "REC_FLEX", "WRRB_FLEX"]),
        "TE": sum(1 for s in roster_positions if s in ["TE", "FLEX", "REC_FLEX"]),
    }

    needed_positions = [
        pos for pos in ["QB", "RB", "WR", "TE"]
        if picks_by_pos.get(pos, 0) < dedicated.get(pos, 0) + backup_needs.get(pos, 0)
    ]

    if DEV_MODE:
        print(f"taxi_full: {taxi_full}, needed_positions: {needed_positions}")
        print(f"picks_by_pos: {picks_by_pos}")

    def has_redraft_value(v):
        pos = v["position"]
        fc_redraft = v["player"].get("fc_redraft_value")
        if fc_redraft is None:
            return False
        return fc_redraft >= taxi_thresholds.get(pos, 100)

    redraft_viable = [v for v in viable if has_redraft_value(v)]
    dynasty_viable = [v for v in viable if not has_redraft_value(v)]

    if DEV_MODE:
        print(f"redraft_viable: {[(v['player'].get('full_name'), round(v['vorp'])) for v in redraft_viable[:3]]}")
        print(f"dynasty_viable: {[(v['player'].get('full_name'), round(v['vorp'])) for v in dynasty_viable[:3]]}")

    if taxi_full:
        if redraft_viable:
            # Taxi full - pick best redraft player at needed position
            best_redraft_needed = next(
                (v for v in redraft_viable if v["position"] in needed_positions),
                None
            )
            best_redraft_overall = redraft_viable[0]
            if best_redraft_needed is None:
                return None, best_redraft_overall["player"], 0
            gap = best_redraft_overall["vorp"] - best_redraft_needed["vorp"]
            if gap > threshold and best_redraft_overall["position"] not in needed_positions:
                return best_redraft_overall["player"], best_redraft_needed["player"], gap
            return None, best_redraft_needed["player"], 0
        elif dynasty_viable:
            # No redraft value left - pick best dynasty player (rookie goes to taxi displacing worst)
            return None, dynasty_viable[0]["player"], 0
        return None, viable[0]["player"], 0
    else:
        # Taxi not full
        best_overall = viable[0]
        if not needed_positions:
            # Check if taxi stash has higher VORP than best overall active pick
            if dynasty_viable and dynasty_viable[0]["vorp"] > best_overall["vorp"]:
                return None, dynasty_viable[0]["player"], 0
            return None, best_overall["player"], 0

        best_needed = next(
            (v for v in viable if v["position"] in needed_positions),
            None
        )
        if best_needed is None:
            return None, best_overall["player"], 0

        # Check if taxi stash has higher VORP than needed position pick
        # Only recommend dynasty_viable players as taxi stashes if actually taxi-eligible
        taxi_allow_vets = league_context.get("taxi_allow_vets", 0)
        actual_taxi_stashes = [v for v in dynasty_viable if v["player"].get("years_exp", 99) == 0 or taxi_allow_vets == 1]
        if actual_taxi_stashes and actual_taxi_stashes[0]["vorp"] > best_needed["vorp"]:
            return None, actual_taxi_stashes[0]["player"], 0

        if DEV_MODE:
            print(f"at_capacity_positions: {at_capacity_positions}")
            print(f"best_overall after capacity filter: {best_overall['player'].get('full_name') if best_overall else None}")
            print(f"best_overall: {best_overall['player'].get('full_name')}, vorp: {best_overall['vorp']}")
            print(f"best_needed: {best_needed['player'].get('full_name')}, vorp: {best_needed['vorp']}")
            print(f"gap: {best_overall['vorp'] - best_needed['vorp']}, threshold: {threshold}")
        gap = best_overall["vorp"] - best_needed["vorp"]
        if gap > threshold and best_overall["position"] not in needed_positions:
            return best_overall["player"], best_needed["player"], gap

        return None, best_needed["player"], gap

def calculate_confidence(recommendation_name, available, alternatives, is_dynasty=True):
    if is_dynasty:
        ranked = sorted(
            [p for p in available.values() if "fc_overall_rank" in p],
            key=lambda x: x["fc_overall_rank"]
        )
        if len(ranked) < 2:
            return "high", None
        top = ranked[0]
        second = ranked[1]
        gap = second.get("fc_overall_rank", 0) - top.get("fc_overall_rank", 0)
        if gap >= 10:
            tier = "high"
        elif gap >= 4:
            tier = "medium"
        else:
            tier = "low"
    else:
        ranked = sorted(
            [p for p in available.values() if "fc_redraft_value" in p],
            key=lambda x: x["fc_redraft_value"],
            reverse=True
        )
        if len(ranked) < 2:
            return "high", None
        top = ranked[0]
        second = ranked[1]
        gap = top.get("fc_redraft_value", 0) - second.get("fc_redraft_value", 0)
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