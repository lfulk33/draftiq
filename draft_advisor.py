import json
import math
from llm_client import get_completion
from config import DEV_MODE
from config import TAXI_THRESHOLD_QB, TAXI_THRESHOLD_RB, TAXI_THRESHOLD_WR, TAXI_THRESHOLD_TE, REDRAFT_THRESHOLD_QB, REDRAFT_THRESHOLD_RB, REDRAFT_THRESHOLD_WR, REDRAFT_THRESHOLD_TE, URGENCY_MODIFIER, DEFAULT_MODEL

# Flex slot eligibility — positions that can fill each flex slot type.
# Used in replacement level calculation, urgency scoring, and capacity checks.
FLEX_ELIGIBILITY = {
    "FLEX":       {"RB", "WR", "TE"},
    "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
    "WRRB_FLEX":  {"RB", "WR"},
    "REC_FLEX":   {"WR", "TE"},
}

# Redraft-value floor per position below which a player is treated as a
# developmental taxi stash rather than an active-roster contributor.
TAXI_THRESHOLDS = {
    "QB": TAXI_THRESHOLD_QB,
    "RB": TAXI_THRESHOLD_RB,
    "WR": TAXI_THRESHOLD_WR,
    "TE": TAXI_THRESHOLD_TE,
}


def get_system_prompt(is_dynasty=True):
    """
    Build the Claude system prompt for draft recommendations.

    Shared instructions apply to both dynasty and redraft. Mode-specific
    instructions are appended at the end to keep the base prompt DRY.

    Args:
        is_dynasty: True for dynasty leagues, False for redraft

    Returns:
        str: system prompt for Claude
    """
    mode_label = "dynasty" if is_dynasty else "redraft"
    analyst_focus = (
        "Focus on long-term player value, age curves, positional scarcity, and dynasty upside."
        if is_dynasty else
        "Focus on current season production, role, opportunity, and offensive context."
    )
    mode_instruction = (
        "Prioritize age, long-term value, and development potential. Taxi squad eligibility matters."
        if is_dynasty else
        "Ignore dynasty value and long-term upside — every comment should be about winning your league THIS season."
    )

    # Shared base prompt — applies to all league types
    base = f"""You are an expert {mode_label} fantasy football analyst and draft advisor for the 2026 NFL season.
You give vivid, specific, confident recommendations that sound like advice from a knowledgeable friend who watches film and follows the league closely.
{analyst_focus}
Use the team, team_qb1, depth_chart_order, and other teammate fields provided to mention specific offensive context, target share, and role clarity.
Write like a fantasy analyst on a podcast — specific, enthusiastic, and grounded in real situation awareness.
{mode_instruction}
Never use the word "VORP" anywhere in your response, not even in phrases like "VORP gap" or "VORP advantage." Instead say things like "biggest gap on the board", "best value available", "most underpriced player at this position", or "the board falls off a cliff after him."
Never mention raw numerical values like dynasty value scores, redraft values, or ranking numbers. Translate those into plain language instead — "one of the top assets at the position", "elite value at this pick", "consensus top-5 at his position", "clear drop-off after him on the board."
You may use widely established player nicknames and abbreviations only when they are universally recognized in fantasy football — "JSN" for Jaxon Smith-Njigba, "CMC" for Christian McCaffrey. Never invent abbreviations or use the wrong initials. When in doubt, use the full name.
Never refer to a player as a "rookie" unless their years_exp is explicitly 0. A player with years_exp of 1 or more is a veteran, even if young.
Never abbreviate team names — always use the full city or team abbreviation as provided in the player data (e.g. "NYG", "SF", "LAR").
CRITICAL: Players change teams via trades, free agency, and releases. The "team" field in the player data is their CURRENT team as of right now — always use it, even if it contradicts a team you associate with that player from past seasons or general knowledge. Never state or imply a player's team without checking the provided "team" field first.
CRITICAL: Never name a specific teammate, backfield-mate, or "alongside X" pairing unless X appears in that exact player's own team_qb1/team_rb1/team_wr1/team_te1 fields, or is explicitly listed elsewhere in the provided data for that player. Do not invent or assume who else is on a player's team from your own knowledge — rosters change, and a name you associate with a team may no longer play there or may never have played there at all.
Do not make up information not provided. If team_qb1 is provided use it. If depth_chart_order is 1, say they are the starter.
CRITICAL: Always reference the user's specific roster by player name in your reasoning. Explain how this pick fills a specific gap, complements an existing player, or why the value justifies taking it over a positional need. Never give generic reasoning that could apply to any team.
CRITICAL: The recommendation has NOT been made yet. Do not assume the user drafted the recommended player when writing alternatives. Frame alternatives as genuine preference alternatives to the recommendation.
Always base your recommendations on the actual league settings provided, including roster construction and scoring format.
Always respond in valid JSON only. No preamble, no markdown, no explanation outside the JSON."""

    return base

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
            "value": redraft_val  # use redraft value only; 0 means not starter-worthy
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
            eligible = FLEX_ELIGIBILITY.get(slot) or set(slot.replace("_FLEX", "").split("_"))
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

    # Only count backup needs for positions with dedicated slots.
    # Flex-eligible positions don't create backup needs — a flex slot can be
    # filled by any eligible position, so there's no specific backup requirement.
    backup_counts = {
        pos: math.ceil(count / 2) if math.floor(count) > 0 else 0
        for pos, count in starter_counts.items()
    }
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

    response = get_completion(prompt, model_key=DEFAULT_MODEL, system="You are a dynasty fantasy football expert. Write clear, concise reasoning in 2-3 sentences. No JSON, just plain text.")
    return response.strip()

def build_prompt(picks, available, my_roster, league_context, pick_number, all_players=None):
    
    taxi_thresholds = TAXI_THRESHOLDS
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
    has_superflex = any(p == "SUPER_FLEX" for p in roster_positions)
    is_dynasty = league_context.get("is_dynasty", True)
    taxi_total = league_context.get("taxi_slots_total", 0) or 0
    taxi_used = league_context.get("taxi_slots_used", 0) or 0
    taxi_open = max(0, taxi_total - taxi_used) if is_dynasty else 0
    
    picks_remaining = league_context.get("picks_remaining_for_me", 0)
    bpa_player, suggested_pick, bpa_gap, trade_bait_players = calculate_bpa(available, league_context, all_players)
    if DEV_MODE:
        print(f"bpa_player: {bpa_player.get('full_name') if bpa_player else None}")
        print(f"suggested_pick: {suggested_pick.get('full_name') if suggested_pick else None}")
        print(f"trade_bait_players: {[t['name'] for t in trade_bait_players]}")
        if bpa_player:
            print(f"  → prompt will say MANDATORY: {bpa_player.get('full_name')}")
        elif suggested_pick:
            print(f"  → prompt will say SUGGESTED: {suggested_pick.get('full_name')}")
        else:
            print(f"  → prompt will say NO STRONG RECOMMENDATION")
    
    vorp_players, replacement, _, _ = calculate_vorp(available, league_context, all_players)
    # Count picks already made by position (this draft + existing roster)
    # Top 3 available players by VORP at each position — used to ground alternatives
    top_by_pos = {}
    for v in sorted(vorp_players, key=lambda x: x["vorp"], reverse=True):
        pos = v["position"]
        if pos not in top_by_pos:
            top_by_pos[pos] = []
        if len(top_by_pos[pos]) < 3:
            top_by_pos[pos].append({
                "name": v["player"].get("full_name"),
                "position": pos,
                "team": v["player"].get("team"),
                "vorp": round(v["vorp"]),
            })
    trade_bait_json = json.dumps([
        {
            "name": t["name"],
            "position": t["position"],
            "type": t["type"],
            "reason": ""
        }
        for t in trade_bait_players
        if t["name"] != (suggested_pick.get("full_name") if suggested_pick else None)
        and t["name"] != (bpa_player.get("full_name") if bpa_player else None)
    ]) if trade_bait_players else "[]"

    prompt = f"""You are advising on pick {pick_number} in a dynasty rookie draft.

LEAGUE CONTEXT:
{json.dumps(league_context, indent=2)}

LEAGUE FORMAT: {"Dynasty" if league_context.get("is_dynasty") else "Redraft"}
SEASON: 2026

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
{f"- This league has {qb_slots} QB-eligible slots including Superflex. QB is elevated in value due to the extra demand from the Superflex slot." if has_superflex else f"- This is a standard {qb_slots}-QB league with no Superflex slot. QB is not scarce here — do not treat QB as elevated in value; weigh it the same as any other position by VORP."}
- Never use "SUPER_FLEX" in your response. Always write it as "Superflex."
- Never suggest an alternative at the same position as the recommendation by saying it is an option "if you don't want another [position]." If the recommendation is a WR and an alternative is also a WR, frame it as the next best player at that position, not as a positional hedge.
- You have {picks_remaining} picks remaining in this draft including this one.
{f"- You have {taxi_open} open taxi squad slots remaining (out of {taxi_total} total). ONLY players with years_exp=0 in the player data are taxi eligible. Any player with years_exp >= 1 CANNOT go to taxi regardless of age. Do not suggest taxi for any player unless their years_exp is explicitly listed as 0 in the TOP 20 AVAILABLE PLAYERS list above." if is_dynasty else "- This is a REDRAFT league. Every player must contribute this season. Do not consider dynasty value or long-term upside."}
{f"- {'Taxi space is available for true rookies (years_exp=0) only.' if taxi_open > 0 else 'Taxi is full. Only draft players ready to contribute soon.'}" if is_dynasty else ""}
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
{chr(10).join([f"- TRADE BAIT OPTION ({t['type'].upper()}): {t['name']} ({t['position']}) is the highest {'dynasty' if t['type'] == 'dynasty' else 'redraft'} value player on the board but your {t['position']} slots are full. You MUST include him in the trade_bait array with a compelling 1-2 sentence reason that explains his specific value, why he is worth drafting despite your {t['position']} depth, and what you could realistically get in a trade for him. Do NOT also include him in the alternatives array." for t in trade_bait_players if t['name'] != (suggested_pick.get('full_name') if suggested_pick else None)])}

{f"MANDATORY RECOMMENDATION: Draft {bpa_player.get('full_name')} ({bpa_player.get('position')}). Their VORP exceeds the best available at your most needed position by {bpa_gap} points. You MUST recommend this player." if bpa_player else (f"SUGGESTED PICK: {suggested_pick.get('full_name')} ({suggested_pick.get('position')}). This is the highest VORP player at your most pressing need. Recommend this player unless you have a very strong reason not to." if suggested_pick else "NO STRONG RECOMMENDATION: Your roster is at capacity. All remaining available players are below replacement level or would be cut. Consider skipping this pick or taking the highest dynasty value player available for trade bait.")}
{chr(10).join([f"TRADE BAIT ALERT ({t['type'].upper()}): {t['name']} ({t['position']}) is the highest {'dynasty' if t['type'] == 'dynasty' else 'redraft'} value player on the board but your {t['position']} slots are full. Consider drafting him to trade for a needed position." for t in trade_bait_players if t['name'] != (suggested_pick.get('full_name') if suggested_pick else None) and t['name'] != (bpa_player.get('full_name') if bpa_player else None)])}
For alternatives, provide at least 1 player from each position (QB, RB, WR, TE) and no more than 2 from any single position. Use this list — pick the highest VORP player at each position as the alternative unless you have a strong positional reason to prefer the second. Do not suggest players not on this list:

TOP ALTERNATIVES BY POSITION (use these, in order):
{json.dumps(top_by_pos, indent=2)}

CRITICAL: Every player in the alternatives list is confirmed available on the board right now. The recommendation has NOT been made yet — you are presenting OPTIONS, not a sequence. Write each alternative as if the user is choosing INSTEAD OF the recommendation, not AFTER it. You may reference players already confirmed on MY CURRENT ROSTER using phrases like "pair with" or "alongside" — but never reference the recommended player as if they are already drafted. Say "if you'd rather go QB here instead" or "if you prefer RB over WR at this pick."
Respond with this exact JSON structure:
{{
    "recommendation": "Player Name",
    "position": "POS",
    "reasoning": "2-3 sentence explanation of why this player at this pick",
    "positional_note": "Brief note on positional scarcity or roster fit",
    "upside": "Brief note on dynasty ceiling",
    "alternatives": [
        {{"name": "Player Name", "position": "POS", "reason": "One sentence why they are the alternative"}}
    ],
    "trade_bait": {trade_bait_json}
}}"""
    return prompt

def get_recommendation(picks, available, my_roster, league_context, pick_number, all_players=None):
    prompt = build_prompt(picks, available, my_roster, league_context, pick_number, all_players)
    is_dynasty = league_context.get("is_dynasty", True)
    response = get_completion(prompt, model_key=DEFAULT_MODEL, system=get_system_prompt(is_dynasty))

    try:
        rec = json.loads(response)
    except json.JSONDecodeError:
        clean = response.strip().removeprefix("```json").removesuffix("```").strip()
        rec = json.loads(clean)

    is_dynasty = league_context.get("is_dynasty", True)
    tier, gap = calculate_confidence(rec.get("recommendation"), available, rec.get("alternatives", []), is_dynasty)
    rec["confidence_tier"] = tier
    rec["confidence_gap"] = gap

    # Look up team for recommended player
    rec_name = rec.get("recommendation", "")
    matched = next(
        (p for p in available.values() if p.get("full_name") == rec_name),
        None
    )
    rec["team"] = matched.get("team") if matched else None

    # Enrich alternatives with team
    for alt in rec.get("alternatives", []):
        alt_matched = next(
            (p for p in available.values() if p.get("full_name") == alt.get("name")),
            None
        )
        alt["team"] = alt_matched.get("team") if alt_matched else None

    # The whole draft is either dynasty or redraft — never a per-entry choice.
    # Claude sometimes fabricates a trade_bait entry not present in the
    # server-computed hint (e.g. when the recommended player is itself the
    # trade bait candidate), and has no anchor for "type" in that case.
    # Force it to the actual league mode rather than trust whatever it wrote.
    for tb in rec.get("trade_bait", []):
        tb["type"] = "dynasty" if is_dynasty else "redraft"

    return rec

def calculate_replacement_levels(league_context, player_pool, value_key):
    """
    Calculate the replacement level value for each position (QB, RB, WR, TE).

    Replacement level = the value of the best player you could pick up if you
    skipped this position entirely. It's the baseline every player is measured
    against when calculating VORP.

    Works identically for dynasty (value_key='fc_value') and redraft
    (value_key='fc_redraft_value') — only the value source differs.

    Flex slots are handled via competition-based simulation:
      1. Remove all dedicated starters from the pool (24 RBs gone in a
         12-team/2-RB league, etc.)
      2. Simulate flex filling: sort remaining eligible players by value,
         assign them to flex slots in order until all flex slots are filled.
         This correctly reflects that RBs and WRs fill most FLEX spots,
         not TEs — so TE replacement level stays near its dedicated cutoff.
      3. Replacement level = value of the first undrafted player at each
         position after dedicated + flex slots are filled.

    Args:
        league_context: dict from build_league_context, must contain
                        dedicated_slots, flex_slot_counts, num_teams
        player_pool:    dict of all players (not just available) — used to
                        find replacement level across the full talent pool
        value_key:      'fc_value' for dynasty, 'fc_redraft_value' for redraft

    Returns:
        dict: {pos: replacement_value} for QB, RB, WR, TE
    """
    num_teams = league_context.get("num_teams", 12)
    dedicated = league_context.get("dedicated_slots", {})
    flex_slot_counts = league_context.get("flex_slot_counts", {})

    # Step 1: Sort all players at each position by value, descending.
    # We work from the full player pool so replacement level reflects
    # league-wide scarcity, not just what's available to one team.
    pos_players = {}
    for pos in ["QB", "RB", "WR", "TE"]:
        pos_players[pos] = sorted(
            [
                p for p in player_pool.values()
                if p.get("position") == pos and p.get(value_key, 0)
            ],
            key=lambda x: x.get(value_key, 0),
            reverse=True
        )

    # Step 2: Mark dedicated starters as drafted.
    # Each team drafts dedicated_slots[pos] players at each position.
    # dedicated_cutoff[pos] = index of first undrafted player after dedicated slots.
    dedicated_cutoff = {
        pos: dedicated.get(pos, 0) * num_teams
        for pos in ["QB", "RB", "WR", "TE"]
    }

    # Track how many players at each position have been "drafted" so far.
    # Starts at the dedicated cutoff — we'll push this further as flex slots fill.
    drafted_count = dict(dedicated_cutoff)

    # Step 3: Simulate flex slot filling via competition.
    # For each flex slot type, build a pool of remaining eligible players
    # sorted by value. Assign them to flex slots in order — best player gets
    # the slot, regardless of position. This correctly models how drafters
    # actually fill flex spots (best available wins, not evenly distributed).
    total_flex_slots = sum(
        count * num_teams
        for slot_type, count in flex_slot_counts.items()
    )

    if total_flex_slots > 0:
        # Run separate competition for each flex slot type.
        # This prevents QB from filling FLEX slots (RB/WR/TE only) just because
        # QB is eligible for SUPER_FLEX in the same league.
        for slot_type, count in flex_slot_counts.items():
            eligible_positions = FLEX_ELIGIBILITY.get(slot_type, set())
            slots_for_this_type = count * num_teams

            # Build candidate pool for this specific slot type
            flex_candidates = []
            for pos in eligible_positions:
                remaining = pos_players[pos][drafted_count[pos]:]
                for p in remaining:
                    flex_candidates.append((p.get(value_key, 0), pos, p))

            # Sort by value descending — best player fills first
            flex_candidates.sort(key=lambda x: x[0], reverse=True)

            # Fill this slot type's slots
            slots_remaining = slots_for_this_type
            for value, pos, player in flex_candidates:
                if slots_remaining == 0:
                    break
                drafted_count[pos] += 1
                slots_remaining -= 1

    # Step 4: Replacement level = value of the first undrafted player
    # at each position after dedicated + flex slots are filled.
    replacement = {}
    for pos in ["QB", "RB", "WR", "TE"]:
        cutoff = drafted_count[pos]
        players_at_pos = pos_players[pos]
        if cutoff < len(players_at_pos):
            # The player just outside the draft window is the replacement
            replacement[pos] = players_at_pos[cutoff].get(value_key, 0)
        elif players_at_pos:
            # Everyone at this position is already drafted — use the last player
            replacement[pos] = players_at_pos[-1].get(value_key, 0)
        else:
            replacement[pos] = 0

    #if DEV_MODE:
        #print(f"calculate_replacement_levels ({value_key}):")
        #print(f"  dedicated_cutoff: {dedicated_cutoff}")
        #print(f"  drafted_count after flex: {drafted_count}")
        #print(f"  replacement levels: {replacement}")

    return replacement, drafted_count, dedicated_cutoff

def calculate_vorp(available, league_context, all_players=None):
    """
    Score every available player by Value Over Replacement Player (VORP).

    VORP = player_value - replacement_level[position]

    Uses the same math for dynasty and redraft — only the value source differs:
      - Dynasty:  fc_value (long-term dynasty value from FantasyCalc)
      - Redraft:  fc_redraft_value (current season value from FantasyCalc)

    Replacement levels are calculated from the full player pool (all_players)
    so they reflect league-wide scarcity, not just what's on the board.
    Falls back to available players if all_players not provided.

    Args:
        available:      dict of players still on the board (not yet drafted)
        league_context: dict from build_league_context
        all_players:    dict of all players in the player pool (optional)

    Returns:
        list of dicts: [{player, vorp, value, position}] sorted by vorp desc
        dict: replacement levels per position (for debugging)
    """
    is_dynasty = league_context.get("is_dynasty", True)
    value_key = league_context.get("value_key", "fc_value" if is_dynasty else "fc_redraft_value")

    # Use full player pool for replacement level calculation if available,
    # otherwise fall back to just the available players (less accurate but functional)
    player_pool = all_players if all_players else available

    # Calculate replacement levels using competition-based simulation
    replacement, drafted_count, dedicated_cutoff = calculate_replacement_levels(league_context, player_pool, value_key)

    #if DEV_MODE:
        #print(f"calculate_vorp ({'dynasty' if is_dynasty else 'redraft'}):")
        #print(f"  available size: {len(available)}, pool size: {len(player_pool)}")
        #print(f"  replacement levels: {replacement}")

    # Score every available player against replacement level
    vorp_players = []
    for p in available.values():
        pos = p.get("position", "?")
        val = p.get(value_key, 0) or 0

        # Skip players with no value data or at non-standard positions (K, DEF)
        if pos not in replacement or not val:
            continue

        vorp = val - replacement[pos]
        vorp_players.append({
            "player": p,
            "vorp": vorp,
            "value": val,
            "position": pos
        })

    # Sort by VORP descending so highest value players come first
    vorp_players.sort(key=lambda x: x["vorp"], reverse=True)

    return vorp_players, replacement, drafted_count, dedicated_cutoff

def _build_sim_state(all_picks, league_context):
    """
    Build a simulated roster state from all picks made so far.

    Two-pass approach:
      Pass 1: Route players with significant redraft value to sim_active.
              Collect taxi-eligible players (low/no redraft value, years_exp=0)
              as candidates.
      Pass 2: Fill taxi slots from candidates using position priority:
              QB > TE > WR > RB (RBs have shortest development window).
              Within each position, keep highest dynasty value on taxi.
              Remaining candidates go to sim_active.

    In redraft leagues taxi_slots_total=0 so all players go to sim_active.

    Args:
        all_picks:      list of player dicts from my_picks_this_draft +
                        my_existing_roster. Each must have: id, position,
                        redraft_value, years_exp.
        league_context: dict from build_league_context

    Returns:
        sim_active: dict of {player_id: player} on the active roster
        sim_taxi:   dict of {player_id: player} on the taxi squad
    """
    taxi_slots_total = league_context.get("taxi_slots_total", 0) or 0
    taxi_allow_vets = league_context.get("taxi_allow_vets", 0)

    taxi_thresholds = TAXI_THRESHOLDS

    sim_active = {}
    sim_taxi = {}
    taxi_candidates = {}  # players eligible for taxi but not yet assigned

    # Pass 1: separate active roster players from taxi candidates
    for p in all_picks:
        pid = p.get("id", p.get("name"))
        if not pid:
            continue

        pos = p.get("position", "?")
        redraft_val = p.get("redraft_value", 0) or p.get("redraft_proxy", 0) or 0
        pos_threshold = taxi_thresholds.get(pos, 100)
        taxi_eligible = p.get("years_exp", 99) == 0 or taxi_allow_vets == 1

        if redraft_val >= pos_threshold:
            # Proven redraft value — normally active roster.
            # But if this position is already over active capacity AND
            # the player is taxi-eligible, treat as taxi candidate instead.
            dedicated = league_context.get("dedicated_slots", {})
            backup_needs = league_context.get("backup_needs", {})
            # Active need includes dedicated slots, flex slots eligible for this
            # position, and backup needs. For QB this means dedicated + superflex + backup.
            flex_slot_counts = league_context.get("flex_slot_counts", {})
            flex_for_pos = sum(
                count for slot_type, count in flex_slot_counts.items()
                if pos in FLEX_ELIGIBILITY.get(slot_type, set())
            )
            active_need = dedicated.get(pos, 0) + flex_for_pos + backup_needs.get(pos, 0)
            current_active_at_pos = sum(
                1 for ap in sim_active.values()
                if ap.get("position") == pos
            )
            if taxi_eligible and current_active_at_pos >= active_need and taxi_slots_total > 0:
                # Position at active capacity — overflow goes to taxi candidates
                taxi_candidates[pid] = p
            else:
                sim_active[pid] = p
        elif taxi_eligible:
            # Low/no redraft value and taxi eligible — candidate for taxi
            taxi_candidates[pid] = p
        else:
            # Not taxi eligible, low value — active bench
            sim_active[pid] = p

    # Pass 2: fill taxi slots by position priority
    # QB and TE benefit most from developmental stashing (scarce, long runway)
    # WR is abundant but worth stashing
    # RB has shortest path to relevance — fill last, bump first
    taxi_priority = ["QB", "TE", "WR", "RB"]

    slots_remaining = taxi_slots_total
    for pos_tier in taxi_priority:
        if slots_remaining == 0:
            break
        # Within each position tier, prioritize lowest redraft value for taxi
        # (pure developmental stashes). Higher redraft value = closer to
        # active-ready = goes to active roster instead.
        # Fall back to highest dynasty value if no redraft values present.
        tier_candidates_list = [
            (pid, p) for pid, p in taxi_candidates.items()
            if p.get("position") == pos_tier
        ]
        any_has_redraft = any(
            (p.get("redraft_value") or 0) > 0
            for _, p in tier_candidates_list
        )
        if any_has_redraft:
            # Sort by redraft value ascending — lowest redraft (most developmental) first.
            # Players with no FC redraft value (0) sort before players with real values,
            # then use search_rank as tiebreaker within the no-FC-value group.
            tier_candidates = sorted(
                tier_candidates_list,
                key=lambda x: (
                        1 if (x[1].get("redraft_value") or 0) > 0 else 0,
                        x[1].get("redraft_value", 0) or 0,
                        x[1].get("redraft_proxy", 0) or 0
                    )
            )
        else:
            # No FC redraft values at all — sort by search_rank ascending
            # (lower search_rank = higher community consensus value = more active-ready)
            # Fall back to dynasty value if no search_rank
            tier_candidates = sorted(
                tier_candidates_list,
                key=lambda x: (
                    x[1].get("search_rank", 999),
                    -(x[1].get("dynasty_value", 0) or 0)
                )
            )
        for pid, p in tier_candidates:
            if slots_remaining == 0:
                break
            sim_taxi[pid] = p
            slots_remaining -= 1

    # Any taxi candidates who didn't make the cut go to active roster
    for pid, p in taxi_candidates.items():
        if pid not in sim_taxi:
            sim_active[pid] = p

    return sim_active, sim_taxi

def _has_active_redraft_viable(pos, viable_active):
    """
    Returns True if any active-bound player at this position has enough
    redraft value to contribute to the active roster.
    Used to determine if urgency should remain active for a position.
    """
    taxi_thresholds = TAXI_THRESHOLDS
    threshold = taxi_thresholds.get(pos, 100)
    return any(
        v["position"] == pos and v["player"].get("fc_redraft_value", 0) >= threshold
        for v in viable_active
    )

def _calculate_urgency(viable, picks_by_pos, league_context, drafted_count=None, dedicated_cutoff=None, sim_taxi=None):
    """
    Calculate how urgently each position needs to be addressed THIS pick.

    Combines two signals:
      1. Opportunity cost: how much value do you lose by waiting one round?
         = best_available_now[pos] - best_available_after_N_picks[pos]
         where N = num_teams (picks until you pick again)

      2. Scarcity ratio: how many positive-VORP players are left relative
         to how many you still need?
         = slots_still_needed / positive_vorp_players_left

    urgency = opportunity_cost * scarcity_ratio

    This replaces threshold-based BPA entirely — positions are compared
    directly by urgency score. No arbitrary threshold needed.

    Args:
        viable:         list of {player, vorp, value, position} sorted by vorp desc
        picks_by_pos:   dict of {pos: count} from _count_picks_by_pos
        league_context: dict from build_league_context

    Returns:
        most_urgent_pos: position with highest urgency score
        urgency_scores:  dict of {pos: score} for debugging
    """
    dedicated = league_context.get("dedicated_slots", {})
    backup_needs = league_context.get("backup_needs", {})
    num_teams = league_context.get("num_teams", 12)
    my_draft_slot = league_context.get("my_draft_slot", 1) or 1
    current_pick = sum(picks_by_pos.values()) + 1  # approximate current pick number

    # Calculate picks until your next turn based on draft slot and snake format.
    # In a snake draft, if you're at slot S in a N-team league:
    # - Odd rounds: you pick at position S, next turn is (N-S) + S = N picks away... 
    # Actually: picks until next turn = (num_teams - my_draft_slot) + (my_draft_slot) = num_teams
    # But at the turn (slot 1 or slot N): picks until next = num_teams * 2 - 1 or 1
    # Simplification: picks_until_next = num_teams + (num_teams - 2 * my_draft_slot + 1).abs() 
    # Recalculate picks until next turn based on current round and draft slot.
    # In odd rounds: slot 1 picks first, waits 2*(N-1)+1 picks until next turn
    # In even rounds: slot 1 picks last, only 1 pick until next turn (back-to-back)
    # Use actual pick number from league context, not just our picks
    picks_made_total = league_context.get("picks_made_total", 0)
    if DEV_MODE:
        print(f"  picks_made_total from context: {picks_made_total}")
    picks_made_total = picks_made_total or sum(picks_by_pos.values()) * num_teams
    current_round = math.ceil((picks_made_total + 1) / num_teams)
    # For opportunity cost simulation, use picks between your current pick
    # and your NEXT pick in the following round. Regardless of slot, other
    # teams make 2*(num_teams-1) picks between your two consecutive round picks.
    # This correctly models how depleted each position will be when you pick again.
    picks_until_next = 2 * (num_teams - 1)

    effective_backup_needs = dict(backup_needs)

    flex_slot_counts = league_context.get("flex_slot_counts", {})

    def get_flex_demand_for_pos(pos, _dc=drafted_count, _dco=dedicated_cutoff):
        if _dc and _dco:
            return (_dc.get(pos, 0) - _dco.get(pos, 0)) / num_teams
        return sum(
            count for slot_type, count in flex_slot_counts.items()
            if pos in FLEX_ELIGIBILITY.get(slot_type, set())
        )

    needed_positions = []
    for pos in ["QB", "RB", "WR", "TE"]:
        fd = get_flex_demand_for_pos(pos)
        total = dedicated.get(pos, 0) + effective_backup_needs.get(pos, 0) + fd
        have = picks_by_pos.get(pos, 0)
        if DEV_MODE:
            print(f"  needed check {pos}: have={have}, need={round(total,2)} (dedicated={dedicated.get(pos,0)}, backup={effective_backup_needs.get(pos,0)}, flex={round(fd,2)})")
        if have < total:
            needed_positions.append(pos)

    # Remove positions where no viable players remain with either:
    # - Active-bound redraft value, OR
    # - Meaningful dynasty value (fc_value) at any placement
    # This allows rookies with real dynasty value but no FC redraft data
    # to satisfy urgency even if they'd normally route to taxi.
    viable_active = [v for v in viable if v.get("placement") != "TAXI"]
    needed_positions = [
        pos for pos in needed_positions
        if _has_active_redraft_viable(pos, viable_active)
        or any(
            v["position"] == pos and v["player"].get("fc_value", 0) > 0
            for v in viable
        )
    ]

    if not needed_positions:
        return None, {}
        
    # Split viable into active-bound and taxi-bound players.
    # Only use active-bound players for urgency — taxi stashes don't fill
    # active roster needs and recommending them creates an infinite loop.
    taxi_slots_total = league_context.get("taxi_slots_total", 0) or 0
    if taxi_slots_total > 0:
        viable_active = [v for v in viable if v.get("placement") != "TAXI"]
        viable_taxi = [v for v in viable if v.get("placement") == "TAXI"]
    else:
        viable_active = viable
        viable_taxi = []

    if DEV_MODE:
        print(f"  viable_active: {len(viable_active)}, viable_taxi: {len(viable_taxi)}")
    # Get best available ACTIVE player at each position right now
    best_now = {}
    for pos in needed_positions:
        best = next((v for v in viable_active if v["position"] == pos), None)
        best_now[pos] = best["vorp"] if best else 0

    # Simulate N picks happening (top N players by VORP get drafted)
    # Use N = num_teams as approximation of picks until your next turn
    top_n_players = [v["player"].get("full_name") for v in viable_active[:picks_until_next]]
    viable_after = [v for v in viable_active if v["player"].get("full_name") not in top_n_players]

    # Get best available ACTIVE player at each position after N picks
    best_after = {}
    for pos in needed_positions:
        best = next((v for v in viable_after if v["position"] == pos), None)
        best_after[pos] = best["vorp"] if best else 0

    urgency_scores = {}
    for pos in needed_positions:
        # Opportunity cost: value lost by waiting.
        # Can be negative if best_now is already below replacement —
        # but the drop still matters (going from -251 to -449 is real loss).
        opportunity_cost = max(0, best_now[pos] - best_after[pos])

        # Scarcity ratio: slots needed vs positive VORP players available
        # Include fractional flex slot demand — a position eligible for FLEX
        # slots has additional effective demand beyond dedicated + backup slots.
        # Same logic as calculate_replacement_levels flex simulation.
        # Use competition-based flex demand from replacement level simulation.
        # This reflects how many flex slots each position actually wins in practice,
        # not just eligibility. TE may be eligible for SUPER_FLEX but rarely wins it.
        if drafted_count and dedicated_cutoff:
            num_teams = league_context.get("num_teams", 12)
            flex_demand = (drafted_count.get(pos, 0) - dedicated_cutoff.get(pos, 0)) / num_teams
        else:
            # Fallback to eligibility count if draft data not available
            flex_demand = sum(
                count for slot_type, count in flex_slot_counts.items()
                if pos in FLEX_ELIGIBILITY.get(slot_type, set())
            )
        slots_needed = max(0,
            dedicated.get(pos, 0) +
            effective_backup_needs.get(pos, 0) +
            flex_demand -
            picks_by_pos.get(pos, 0)
        )
        # Count all available players, not just positive VORP ones.
        # A position with only negative VORP players is still scarce —
        # the pool is depleted and getting worse each round.
        all_players_at_pos = len([v for v in viable_active if v["position"] == pos])
        positive_vorp_players = len([v for v in viable_active if v["position"] == pos and v["vorp"] > 0])

        if all_players_at_pos == 0:
            # No players left at this position at all
            scarcity_ratio = float('inf')
        elif positive_vorp_players == 0:
            # Only negative VORP players remain — use all players for scarcity
            scarcity_ratio = slots_needed / all_players_at_pos
        else:
            scarcity_ratio = slots_needed / positive_vorp_players

        # Reduce urgency for backup-only slots — any player fills them
        dedicated_filled = picks_by_pos.get(pos, 0) >= dedicated.get(pos, 0)
        backup_multiplier = 0.3 if dedicated_filled else 1.0
        if DEV_MODE:
            print(f"  {pos}: dedicated_filled={dedicated_filled}, picks={picks_by_pos.get(pos,0)}, dedicated={dedicated.get(pos,0)}, backup_mult={backup_multiplier}")

        urgency_scores[pos] = opportunity_cost * (1 + scarcity_ratio) * backup_multiplier

        if DEV_MODE:
            print(f"  {pos}: opp_cost={round(opportunity_cost)}, scarcity={round(scarcity_ratio,3)}, backup_mult={backup_multiplier}, urgency={round(urgency_scores[pos])}, slots_needed={round(slots_needed,2)}, flex_demand={round(flex_demand,2)}, pos_vorp_players={positive_vorp_players}")

    if not urgency_scores:
        return None, {}

    most_urgent_pos = max(urgency_scores, key=lambda p: urgency_scores[p])

    if DEV_MODE:
        print(f"_calculate_urgency:")
        print(f"  my_draft_slot: {my_draft_slot}, num_teams: {num_teams}, picks_until_next: {picks_until_next}")
        print(f"  needed_positions: {needed_positions}")
        print(f"  picks_by_pos: {picks_by_pos}")
        print(f"  urgency_scores: {urgency_scores}")
        print(f"  most_urgent_pos: {most_urgent_pos}")

    return most_urgent_pos, urgency_scores

def _bpa_decision_v2(best_overall, best_needed, urgency_scores, viable=None):
    """
    Shared BPA decision scoring for both dynasty and redraft leagues.

    Scores every position's best-VORP player by vorp * urgency^URGENCY_MODIFIER.
    Positive-VORP players are scored directly. Negative-VORP players are
    scored by dividing by urgency instead of multiplying, so higher urgency
    still makes a below-replacement pick relatively less bad (closer to
    zero) without ever letting it cross into positive territory and beat
    a real positive-VORP player — a negative score can never outscore a
    non-negative one.
    """
    if not best_overall:
        return None, None, 0
    if not best_needed:
        return None, best_overall["player"], 0

    position_best = {}
    if viable:
        for v in viable:
            pos = v["position"]
            if pos not in position_best or v["vorp"] > position_best[pos]["vorp"]:
                position_best[pos] = v

    overall_pos = best_overall["position"]
    needed_pos = best_needed["position"]
    overall_urgency = urgency_scores.get(overall_pos, 1)
    needed_urgency = urgency_scores.get(needed_pos, 1)

    def calc_score(vorp, urgency):
        if vorp >= 0:
            return vorp * (urgency ** URGENCY_MODIFIER)
        else:
            safe_urgency = max(urgency, 0.01)
            return vorp / (safe_urgency ** URGENCY_MODIFIER)

    overall_score = calc_score(best_overall["vorp"], overall_urgency)
    needed_score = calc_score(best_needed["vorp"], needed_urgency)

    # Only positions with a real computed urgency (i.e. still actually
    # needed on this roster — present in urgency_scores) are eligible to
    # win the override slot. A fully satisfied position (starter + backup
    # already filled) isn't in urgency_scores at all and defaults to a
    # placeholder urgency of 1 via .get(pos, 1) — that placeholder is not a
    # real need signal, so it must never be allowed to outscore a position
    # that's genuinely still needed just because its league-wide VORP is
    # high. That scenario is exactly what the separate trade_bait signal
    # is for, not a MANDATORY override.
    best_pos = needed_pos
    best_score = needed_score
    best_v = best_needed
    if overall_pos in urgency_scores and overall_score > best_score:
        best_pos = overall_pos
        best_score = overall_score
        best_v = best_overall
    for pos, v in position_best.items():
        if pos not in urgency_scores:
            continue
        score = calc_score(v["vorp"], urgency_scores[pos])
        if score > best_score:
            best_score = score
            best_pos = pos
            best_v = v

    if DEV_MODE:
        all_scores = sorted(
            [(position_best[pos]["player"].get("full_name"), pos,
              round(position_best[pos]["vorp"]),
              round(urgency_scores.get(pos, 0)),
              round(calc_score(position_best[pos]["vorp"], urgency_scores.get(pos, 0))),
              position_best[pos]["player"].get("fc_redraft_value", 0),
              position_best[pos].get("placement", "?"))
             for pos in position_best if pos in urgency_scores],
            key=lambda x: x[4], reverse=True
        )[:5]
        print(f"_bpa_decision_v2:")
        print(f"  top scores: {all_scores}")
        print(f"  best_needed: {best_needed['player'].get('full_name')} ({needed_pos}), vorp={round(best_needed['vorp'])}, urgency={round(needed_urgency)}, score={round(needed_score)}")
        print(f"  winner: {best_v['player'].get('full_name')} ({best_pos}), score={round(best_score)}")
        for pos, v in position_best.items():
            urgency = urgency_scores.get(pos, 1)
            sc = calc_score(v["vorp"], urgency)
            print(f"  position_best {pos}: {v['player'].get('full_name')}, vorp={round(v['vorp'])}, urgency={round(urgency)}, score={round(sc)}, placement={v.get('placement')}")

    if best_v["player"].get("full_name") != best_needed["player"].get("full_name"):
        gap = best_v["vorp"] - best_needed["vorp"]
        return best_v["player"], best_needed["player"], gap

    return None, best_needed["player"], 0

def _find_candidates(viable, most_needed_pos, picks_by_pos, league_context, drafted_count=None, dedicated_cutoff=None, sim_taxi=None):
    """
    Find the two key players for the BPA decision:
      - best_needed: highest VORP player at the most needed position
      - best_overall: highest VORP player ignoring at-capacity positions

    These two players are compared against the BPA threshold to decide
    whether to recommend positional need or pure value.

    Args:
        viable:          list of {player, vorp, value, position} sorted
                         by vorp descending
        most_needed_pos: string position from _calculate_scarcity
        picks_by_pos:    dict of {pos: count} from _count_picks_by_pos
        league_context:  dict from build_league_context

    Returns:
        best_needed:  viable dict for best player at needed position,
                      or None if no players available there
        best_overall: viable dict for best player ignoring full positions,
                      or None if viable is empty
    """
    dedicated = league_context.get("dedicated_slots", {})
    backup_needs = league_context.get("backup_needs", {})

    effective_backup_needs = dict(backup_needs)

    # Only consider active-bound players for positional need decisions
    taxi_slots_total = league_context.get("taxi_slots_total", 0) or 0
    if taxi_slots_total > 0:
        viable_active = [v for v in viable if v.get("placement") != "TAXI"]
    else:
        viable_active = viable
        
    flex_slot_counts = league_context.get("flex_slot_counts", {})
    at_capacity_positions = [
        pos for pos in ["QB", "RB", "WR", "TE"]
        if picks_by_pos.get(pos, 0) >= (
            dedicated.get(pos, 0) +
            effective_backup_needs.get(pos, 0) +
            sum(count for slot_type, count in flex_slot_counts.items()
                if pos in FLEX_ELIGIBILITY.get(slot_type, set()))
        )
    ]

    # best_needed = highest VORP player across ALL needed positions.
    # Scarcity determines which position to recommend when BPA doesn't fire,
    # but the BPA threshold comparison should use the best available needed
    # player regardless of position — otherwise BPA fires too easily when
    # the most scarce position has low VORP players.
    def get_flex_demand_for_pos(pos, _dc=drafted_count, _dco=dedicated_cutoff):
        if _dc and _dco:
            num_teams_local = league_context.get("num_teams", 12)
            return (_dc.get(pos, 0) - _dco.get(pos, 0)) / num_teams_local
        return sum(
            count for slot_type, count in flex_slot_counts.items()
            if pos in FLEX_ELIGIBILITY.get(slot_type, set())
        )

    needed_positions = []
    for pos in ["QB", "RB", "WR", "TE"]:
        fd = get_flex_demand_for_pos(pos)
        total = dedicated.get(pos, 0) + effective_backup_needs.get(pos, 0) + fd
        have = picks_by_pos.get(pos, 0)
        if DEV_MODE:
            print(f"  needed check {pos}: have={have}, need={round(total,2)} (dedicated={dedicated.get(pos,0)}, backup={effective_backup_needs.get(pos,0)}, flex={round(fd,2)})")
        if have < total:
            needed_positions.append(pos)
    # Best player at any needed position by VORP
    best_needed_overall = next(
        (v for v in viable_active if v["position"] in needed_positions),
        None
    )
    best_needed_scarce = next(
        (v for v in viable_active if v["position"] == most_needed_pos),
        None
    ) if most_needed_pos else None

    # If no active-bound player at most needed pos, allow best dynasty-value
    # taxi player there — urgency overrides normal taxi routing for needed positions
    if not best_needed_scarce and most_needed_pos:
        best_needed_scarce = next(
            (v for v in viable if v["position"] == most_needed_pos),
            best_needed_overall
        )

    best_needed_scarce = best_needed_scarce or best_needed_overall

    # Use best_needed_overall for BPA threshold comparison,
    # best_needed_scarce as the actual recommendation when BPA doesn't fire
    # Best player overall by VORP — defined here so it's available for comparison below
    best_overall = viable_active[0] if viable_active else None

    # Use scarcity-based pick only if it's within reasonable range of best overall need.
    # If the scarce position player is dramatically worse by VORP, use the overall best instead.
    # Threshold: if best_needed_overall is more than 50% better VORP than best_needed_scarce,
    # scarcity is being overridden by raw value gap — take the better player.
    # best_needed is always the scarce position player.
    # BPA in the decision function handles whether to override with best_overall.
    best_needed = best_needed_scarce

    if DEV_MODE:
        print(f"_find_candidates:")
        print(f"  at_capacity_positions: {at_capacity_positions}")
        print(f"  dedicated: {dedicated}")
        print(f"  effective_backup_needs: {effective_backup_needs}")
        print(f"  picks_by_pos: {picks_by_pos}")
        print(f"  best_needed (scarce): {best_needed_scarce['player'].get('full_name') if best_needed_scarce else None}, vorp: {round(best_needed_scarce['vorp']) if best_needed_scarce else None}")
        print(f"  best_needed (final): {best_needed['player'].get('full_name') if best_needed else None}, vorp: {round(best_needed['vorp']) if best_needed else None}")
        print(f"  best_needed (overall): {best_needed_overall['player'].get('full_name') if best_needed_overall else None}, vorp: {round(best_needed_overall['vorp']) if best_needed_overall else None}")
        print(f"  best_overall: {best_overall['player'].get('full_name') if best_overall else None}, vorp: {round(best_overall['vorp']) if best_overall else None}")
        # Top 5 by VORP at each position — permanent tuning log
        for pos in ["QB", "RB", "WR", "TE"]:
            top = [(v['player'].get('full_name'), round(v['vorp'])) for v in viable if v['position'] == pos][:5]
            if top:
                print(f"  top {pos}: {top}")
    return best_needed, best_overall, best_needed_overall


def _count_picks_by_pos(sim_active, league_context):
    """
    Count how many players at each position are on the active roster.

    Every drafted player counts toward positional totals regardless of value.
    A drafted player occupies a roster spot whether they are elite or
    developmental — value thresholds are used in taxi routing and BPA
    decisions, not here.

    Args:
        sim_active:     dict of {player_id: player} on active roster
        league_context: dict from build_league_context

    Returns:
        dict: {pos: count} for QB, RB, WR, TE
    """
    # Count all drafted players by position.
    # Every drafted player occupies a roster spot regardless of value —
    # thresholds are used in taxi routing and BPA decisions, not here.
    picks_by_pos = {}
    for p in sim_active.values():
        pos = p.get("position", "?")
        if pos in ["QB", "RB", "WR", "TE"]:
            picks_by_pos[pos] = picks_by_pos.get(pos, 0) + 1

    return picks_by_pos


def _filter_viable(sorted_vorp, sim_active, sim_taxi, league_context):
    """
    Filter VORP-sorted players to those who would make the roster.
    Returns viable list with placement tag added to each entry.
    """
    viable = []
    for v in sorted_vorp:
        player = v["player"]
        # Treat players with rookie_year=2026 and no years_exp as years_exp=0
        years_exp = player.get("years_exp")
        if years_exp is None:
            rookie_year = player.get("metadata", {}).get("rookie_year")
            years_exp = 0 if rookie_year == "2026" else 99

        candidate = {
            "id": player.get("sleeper_id") or player.get("full_name"),
            "name": player.get("full_name"),
            "position": player.get("position"),
            "dynasty_value": player.get("fc_value", 0),
            "redraft_value": player.get("fc_redraft_value", 0) or 0,
            "redraft_proxy": max(0, (1000 - (player.get("search_rank") or 1000)) * 10),
            "years_exp": years_exp,
            "overall_rank": player.get("fc_overall_rank", 999),
        }
        placement, _ = simulate_placement(candidate, sim_active, sim_taxi, league_context)
        if placement != "CUT":
            viable.append({**v, "placement": placement})
    return viable


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
    
    # Check if player should go to taxi based on redraft value threshold,
    # even if active roster has space. Low redraft value + taxi eligible =
    # developmental stash, not active roster contributor.
    taxi_thresholds = TAXI_THRESHOLDS
    pos = candidate.get("position", "?")
    redraft_val = candidate.get("redraft_value", 0) or candidate.get("redraft_proxy", 0) or 0
    below_threshold = redraft_val < taxi_thresholds.get(pos, 100)

    # Rank within this position only (not the whole roster) — STARTER is
    # decided against that position's own dedicated slot count. Flex slots
    # aren't modeled here; this is a rough simulation, not the authoritative
    # starter calculation (see calculate_starter_ids for that).
    dedicated_slots = league_context.get("dedicated_slots", {})
    same_pos = [p for p in all_active if p.get("position") == pos]
    pos_rank = next(
        i + 1 for i, p in enumerate(same_pos)
        if p.get("id") == candidate.get("id")
    )
    is_starter = pos_rank <= dedicated_slots.get(pos, 0)

    if candidate_rank <= active_capacity:
        # Candidate fits on active roster
        if len(sim_active) < active_capacity:
            # Active roster not full, no cuts needed
            # But if below redraft threshold and taxi eligible, route to taxi
            if taxi_eligible and below_threshold and len(sim_taxi) < taxi_slots_total:
                return "TAXI", None
            if is_starter:
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

def calculate_bpa(available, league_context, all_players=None):
    """
    Calculate the Best Player Available recommendation.

    Determines whether to recommend the highest-value player on the board
    (BPA) or the best player at the most urgently needed position, based
    on the VORP gap between them vs the BPA threshold.

    Flow:
      1. Score all available players by VORP
      2. Build simulated roster state from existing picks
      3. Filter to only players who would make the roster (not CUT)
      4. Count how many meaningful players we have at each position
      5. Calculate positional scarcity scores
      6. Find best_needed and best_overall candidates
      7. Dynasty/redraft-specific decision on which to recommend

    Args:
        available:      dict of players still on the board
        league_context: dict from build_league_context
        all_players:    full player pool for replacement level calculation

    Returns:
        (bpa_player, suggested_pick, gap) where:
          bpa_player    — non-None when BPA overrides positional need;
                          becomes the MANDATORY recommendation in the prompt
          suggested_pick — the recommended player (always non-None if possible)
          gap           — VORP difference between best_overall and best_needed
    """
    is_dynasty = league_context.get("is_dynasty", True)
    threshold = league_context.get("bpa_threshold", 1000)

    if DEV_MODE:
        print(f"calculate_bpa: is_dynasty={is_dynasty}, threshold={threshold}")

    # Step 1: Score all available players by VORP
    vorp_players, replacement, drafted_count, dedicated_cutoff = calculate_vorp(available, league_context, all_players)
    
    if not vorp_players:
        return None, None, None, None

    # Step 2: Build simulated roster state from all picks made so far
    all_picks = (
        league_context.get("my_picks_this_draft", []) +
        league_context.get("my_existing_roster", [])
    )
    sim_active, sim_taxi = _build_sim_state(all_picks, league_context)

    if DEV_MODE:
        print(f"  sim_active: {len(sim_active)}, sim_taxi: {len(sim_taxi)}")

    # Step 3: Filter to players who would actually make the roster
    sorted_vorp = sorted(
        [v for v in vorp_players if v["position"] not in ["K", "DEF"]],
        key=lambda x: x["vorp"],
        reverse=True
    )
    viable = _filter_viable(sorted_vorp, sim_active, sim_taxi, league_context)

    if DEV_MODE:
        print(f"  viable count: {len(viable)}")
        print(f"  top 5 viable: {[(v['player'].get('full_name'), v['position'], round(v['vorp'])) for v in viable[:5]]}")

    if not viable:
        if vorp_players:
            best = max(vorp_players, key=lambda x: x["vorp"])
            return None, best["player"], 0, None
        return None, None, None, None

    # Step 4: Count meaningful players at each position
    picks_by_pos = _count_picks_by_pos(sim_active, league_context)

    # Step 5: Calculate positional urgency (opportunity cost × scarcity)
    if DEV_MODE:
        print(f"  drafted_count before urgency: {drafted_count}")
        print(f"  dedicated_cutoff before urgency: {dedicated_cutoff}")
    most_needed_pos, urgency_scores = _calculate_urgency(
        viable, picks_by_pos, league_context, drafted_count, dedicated_cutoff, sim_taxi
    )

    # Step 6: Find the two key candidates
    best_needed, best_overall, best_needed_overall = _find_candidates(
        viable, most_needed_pos, picks_by_pos, league_context, drafted_count, dedicated_cutoff, sim_taxi
    )

    # Scale threshold based on how many players we have over dedicated slots
    # at the SAME position as best_overall. Prevents BPA from spamming one position.
    # Each player owned beyond dedicated slots doubles the required gap.
    dedicated = league_context.get("dedicated_slots", {})
    if best_overall:
        bpa_pos = best_overall["position"]
        owned = picks_by_pos.get(bpa_pos, 0)
        dedicated_slots = dedicated.get(bpa_pos, 0)
        backup = league_context.get("backup_needs", {}).get(bpa_pos, 0)
        
        if owned >= dedicated_slots and backup > 0:
            # In backup territory — scale threshold up significantly.
            # Backup slots are much less urgent than starter slots.
            # BPA needs a much larger gap to justify taking a backup over a starter need.
            over_dedicated = owned - dedicated_slots
            threshold = threshold * (5 + (0.5 * over_dedicated))
        

    if DEV_MODE:
        print(f"  scaled threshold: {threshold}")

    # Check for trade bait — best dynasty value AND best redraft value at full positions.
    trade_bait_players = []

    def is_at_capacity(pos):
        return picks_by_pos.get(pos, 0) >= (
            dedicated.get(pos, 0) +
            league_context.get("backup_needs", {}).get(pos, 0) +
            (drafted_count.get(pos, 0) - dedicated_cutoff.get(pos, 0)) / league_context.get("num_teams", 12)
        )

    # If no urgency scores — all active needs met or no active-bound redraft-viable
    # players remain. Take best dynasty value regardless of placement.
    if not urgency_scores:
        # All active needs met or no active-bound redraft-viable players remain.
        # Take best dynasty value at a needed position, skipping at-capacity positions.
        needed_any = [
            pos for pos in ["QB", "RB", "WR", "TE"]
            if picks_by_pos.get(pos, 0) < (
                dedicated.get(pos, 0) +
                league_context.get("backup_needs", {}).get(pos, 0) +
                (drafted_count.get(pos, 0) - dedicated_cutoff.get(pos, 0)) / league_context.get("num_teams", 12)
            )
        ]
        best_fallback = next(
            (v for v in viable if v["position"] in needed_any),
            viable[0] if viable else None
        )
        if DEV_MODE:
            print(f"  no urgency scores — taking best needed dynasty value: {best_fallback['player'].get('full_name') if best_fallback else None}")
        return None, best_fallback["player"] if best_fallback else None, 0, trade_bait_players

    # Step 7: BPA decision (identical scoring for dynasty and redraft)
    bpa_player, suggested_pick, gap = _bpa_decision_v2(best_overall, best_needed, urgency_scores, viable)

    # Trade bait — only fires when the top player on the board is NOT the recommendation.
    # If the best player is already being recommended, there is no trade bait scenario.
    top_player = viable[0] if viable else None
    if top_player and best_needed and top_player["player"].get("full_name") != (best_needed["player"].get("full_name") if best_needed else None):
        trade_bait_players.append({
            "name": top_player["player"].get("full_name"),
            "position": top_player["position"],
            "type": "dynasty" if is_dynasty else "redraft",
            "player": top_player["player"]
        })
        if DEV_MODE:
            print(f"  trade_bait: {top_player['player'].get('full_name')} ({top_player['position']}) — top player not recommended")


    return bpa_player, suggested_pick, gap, trade_bait_players

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