import json
from llm_client import get_completion

SYSTEM_PROMPT = """You are an expert dynasty fantasy football draft assistant. 
You specialize in dynasty leagues with superflex/2QB formats.
You reason about long-term player value, age curves, and positional scarcity.
You give concise, confident recommendations with clear reasoning.
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
        enriched.append({
            "id": pid,
            "position": player.get("position", "?"),
            "value": player.get("fc_value", 0) if isinstance(player.get("fc_value"), int) else 0
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

def build_prompt(picks, available, my_roster, league_context, pick_number):
    top_available = sorted(
        [p for p in available.values() if "fc_overall_rank" in p],
        key=lambda x: x["fc_overall_rank"]
    )[:20]

    available_summary = []
    for p in top_available:
        available_summary.append({
            "name": p.get("full_name"),
            "position": p.get("position"),
            "age": p.get("fc_age"),
            "dynasty_value": p.get("fc_value"),
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

    prompt = f"""You are advising on pick {pick_number} in a dynasty rookie draft.

LEAGUE CONTEXT:
{json.dumps(league_context, indent=2)}

MY CURRENT ROSTER:
{json.dumps(my_players, indent=2)}

TOP 20 AVAILABLE PLAYERS BY DYNASTY VALUE:
{json.dumps(available_summary, indent=2)}

TOTAL PICKS MADE SO FAR: {len(picks)}

IMPORTANT ROSTER CONSTRUCTION NOTES:
- This league has {te_slots} dedicated TE slot(s). {"TE is low priority unless elite." if te_slots == 0 else "TE has some value but is not premium."}
- This league has {qb_slots} QB-eligible slots including SUPER_FLEX. QB is elevated in value.
- Only recommend a TE if they are clearly the best available player by a significant margin.
- Prioritize QB, RB, and WR unless a TE represents exceptional value.
- You have {taxi_open} open taxi squad slots. Developmental rookies can be stashed there for up to {league_context.get("taxi_years")} years.
- You have {picks_remaining} picks remaining in this draft including this one.
- {"Taxi space is available so developmental stashes are viable." if taxi_open > 0 else "Taxi is full. Only draft players ready to contribute soon."}

Based on the available players, my roster construction, and dynasty value principles,
recommend who I should draft with this pick. For alternatives, provide at least 1 player
from each position (QB, RB, WR, TE) and no more than 2 from any single position.

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

def build_depth_chart(sim_active, starter_ids, players, league_detail):
    roster_positions = league_detail.get("roster_positions", [])
    position_starter_counts = {}
    for pos in roster_positions:
        if pos in {"QB", "RB", "WR", "TE", "K", "DEF"}:
            position_starter_counts[pos] = position_starter_counts.get(pos, 0) + 1

    depth_chart = {}
    for pid, p in sim_active.items():
        pos = p.get("position", "?")
        if pos not in depth_chart:
            depth_chart[pos] = []
        depth_chart[pos].append({
            "id": pid,
            "name": p.get("name"),
            "value": p.get("value", 0),
            "age": p.get("age"),
            "years_exp": p.get("years_exp"),
            "on_ir": p.get("on_ir", False),
            "is_starter": pid in starter_ids
        })

    for pos in depth_chart:
        depth_chart[pos].sort(key=lambda x: x["value"], reverse=True)

    return depth_chart, position_starter_counts

def get_claude_roster_recommendation(rookie, sim_active, sim_taxi, starter_ids, league_detail, players, reserve_ids):
    depth_chart, position_starter_counts = build_depth_chart(sim_active, starter_ids, players, league_detail)

    taxi_slots_total = league_detail["settings"].get("taxi_slots", 0)
    taxi_years = league_detail["settings"].get("taxi_years", 3)
    taxi_allow_vets = league_detail["settings"].get("taxi_allow_vets", 0)
    roster_positions = league_detail.get("roster_positions", [])
    roster_max = len(roster_positions) + len(reserve_ids)

    open_taxi = taxi_slots_total - len(sim_taxi)
    roster_count = len(sim_active)
    roster_over = max(0, roster_count - roster_max)
    taxi_eligible = rookie["years_exp"] == 0 and taxi_allow_vets == 0

    taxi_summary = [
        {"name": p["name"], "position": p["position"], "value": p["value"], "years_exp": p["years_exp"]}
        for p in sorted(sim_taxi.values(), key=lambda x: x["value"])
    ]

    rookie_position_depth = depth_chart.get(rookie["position"], [])
    starter_count_at_pos = position_starter_counts.get(rookie["position"], 0)

    prompt = f"""You are a dynasty fantasy football roster management expert.

A player has just been drafted and you need to recommend exactly where they go on the roster and what cascading moves are needed.

DRAFTED PLAYER:
{json.dumps({
    "name": rookie["name"],
    "position": rookie["position"],
    "age": rookie["age"],
    "dynasty_value": rookie["value"],
    "years_exp": rookie["years_exp"],
    "taxi_eligible": taxi_eligible
}, indent=2)}

THEIR POSITION DEPTH CHART (your current roster at {rookie["position"]}, sorted by dynasty value):
{json.dumps(rookie_position_depth, indent=2)}

STARTERS AT {rookie["position"]}: {starter_count_at_pos}

FULL ROSTER SUMMARY BY POSITION:
{json.dumps({pos: [{"name": p["name"], "value": p["value"], "is_starter": p["is_starter"], "on_ir": p["on_ir"]} for p in pos_players] for pos, pos_players in depth_chart.items()}, indent=2)}
MANDATORY CUT CANDIDATE (if a cut is required):
{json.dumps(
    sorted(
        [{"name": p["name"], "position": p["position"], "value": p["value"], "location": "active_bench"} 
         for p in sim_active.values() 
         if p["id"] not in starter_ids and not p.get("on_ir")] +
        [{"name": p["name"], "position": p["position"], "value": p["value"], "years_exp": p["years_exp"], "location": "taxi"} 
         for p in sim_taxi.values()],
        key=lambda x: x["value"]
    )[0] if sim_active or sim_taxi else {},
    indent=2
)}
THIS IS THE PLAYER TO CUT if a cut is needed. Do not cut anyone else.

CURRENT TAXI SQUAD:
{json.dumps(taxi_summary, indent=2)}

ROSTER SITUATION:
- Current active roster count: {roster_count}
- Roster max (including IR): {roster_max}
- Roster over limit by: {roster_over}
- NOTE: Taxi is a SEPARATE squad from the active roster. Placing a player on taxi does NOT affect the active roster count. Never recommend cutting an active roster player just because a player is going to taxi.
- The ONLY time a cut is needed for a TAXI move is when the taxi squad itself is FULL (0 open slots). In that case follow the taxi-full rules below.
- If taxi has open slots, no cuts of any kind are needed regardless of active roster count.
- NOTE: If this player goes to ACTIVE_BENCH and roster is over limit, recommend exactly ONE cut.
- NOTE: If roster is at or under the limit after placing this player, no cuts are needed.
- Open taxi slots: {open_taxi}
- Taxi years allowed: {taxi_years}
- Taxi eligibility: {"Rookie only (years_exp = 0)" if taxi_allow_vets == 0 else "Veterans allowed"}

LEAGUE ROSTER CONSTRUCTION:
{json.dumps(roster_positions, indent=2)}

REASONING GUIDELINES:
- Consider dynasty value AND positional depth. A player ranked 4th at their position in a 2-starter league will rarely play.
- For each position, count the starters (shown as is_starter=true in the depth chart). The player immediately after the last starter is the PRIMARY BACKUP who would start on bye weeks or injuries.
- MANDATORY RULE: If this player's dynasty value exceeds the PRIMARY BACKUP at their position (the player immediately after the last starter), you MUST recommend ACTIVE_BENCH. Taxi eligibility, open taxi slots, and roster limit do not override this rule. A player who is better than your primary backup will contribute when the starter is injured or on bye and must be on the active roster.
- If the roster is over the limit and the player must go to ACTIVE_BENCH, recommend exactly ONE cut from the MANDATORY CUT CANDIDATE.
- TAXI is only for players who rank below the primary backup at their position.
- If the player goes to TAXI and taxi is full: 
  Step 1: Cut the player identified in MANDATORY CUT CANDIDATE above.
  Step 2: If the mandatory cut came from the active bench (location = "active_bench"), taxi is still full. You MUST also promote the taxi player with the fewest remaining taxi years (taxi_years_allowed minus years_exp) to active bench. Use dynasty value as tiebreaker. This promotion frees the taxi slot for the incoming player. Include BOTH the cut AND the promotion as cascading moves.
  Step 3: If the mandatory cut came from taxi (location = "taxi"), the slot is now open directly. No promotion needed.
- If the player goes to ACTIVE_BENCH and roster is over limit: cut the player identified in MANDATORY CUT CANDIDATE above. Do not choose a different player to cut.
- If the player goes to ACTIVE_BENCH and roster is over limit, recommend cutting the lowest value non-starter non-IR active roster player. Do not touch the taxi squad for this cut.
- If the player goes to ACTIVE_BENCH and roster is at or under the limit, no cuts are needed.
- If the player goes to TAXI and taxi has open slots, no cuts are needed regardless of active roster count.
- Do not attempt to solve the entire roster situation in one move. Each drafted player triggers exactly one cut if needed.
- If the player goes to active roster and bumps someone to taxi eligibility, note that too.
- Consider age curves: young high-value players deserve roster spots even if not immediate contributors.
- IR players count toward roster construction but cannot be cut for roster management.

Respond with this exact JSON structure:
{{
    "rookie_action": "STARTER" or "ACTIVE_BENCH" or "TAXI" or "CUT",
    "reasoning": "2-3 sentences explaining the decision considering depth chart, dynasty value, and taxi eligibility",
    "cascading_moves": [
        {{
            "player_name": "Full Name",
            "action": "CUT" or "TAXI" or "PROMOTE_TO_BENCH",
            "reason": "One sentence explanation"
        }}
    ]
}}

If no cascading moves are needed, return an empty array for cascading_moves.
"""
    response = get_completion(prompt, system="You are a dynasty fantasy football expert. Always respond in valid JSON only. No preamble, no markdown.")
    try:
        clean = response.strip().removeprefix("```json").removesuffix("```").strip()
        # Find the first complete JSON object only
        decoder = json.JSONDecoder()
        result, _ = decoder.raw_decode(clean)
        return result
    except (json.JSONDecodeError, ValueError):
        # Try extracting just the first { } block
        start = clean.find("{")
        end = clean.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(clean[start:end])
        raise

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
            "value": p.get("fc_value", 0) if isinstance(p.get("fc_value"), int) else 0,
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
        rec = get_claude_roster_recommendation(
            rookie, sim_active, sim_taxi, starter_ids,
            league_detail, players, reserve_ids
        )

        recommendations.append({
            "player": rookie["name"],
            "position": rookie["position"],
            "action": rec.get("rookie_action", "UNKNOWN"),
            "reasoning": rec.get("reasoning", ""),
            "cascading_moves": rec.get("cascading_moves", []),
            "severity": {
                "STARTER": "success",
                "ACTIVE_BENCH": "info",
                "TAXI": "info",
                "CUT": "error"
            }.get(rec.get("rookie_action", ""), "info")
        })

        # Update sim state based on Claude's decision
        action = rec.get("rookie_action")
        if action == "TAXI":
            sim_taxi[rookie["id"]] = rookie
        elif action in ("STARTER", "ACTIVE_BENCH"):
            sim_active[rookie["id"]] = rookie
        elif action == "CUT":
            pass  # stays out of sim_active

        # Apply cascading moves to sim state
        for move in rec.get("cascading_moves", []):
            move_name = move.get("player_name", "")
            move_action = move.get("action", "")
            matched = next((p for p in list(sim_active.values()) + list(sim_taxi.values()) if p["name"] == move_name), None)
            if matched:
                if move_action == "CUT":
                    sim_active.pop(matched["id"], None)
                    sim_taxi.pop(matched["id"], None)
                elif move_action == "TAXI":
                    sim_taxi[matched["id"]] = matched
                    sim_active.pop(matched["id"], None)
                elif move_action == "PROMOTE_TO_BENCH":
                    sim_active[matched["id"]] = matched
                    sim_taxi.pop(matched["id"], None)

    return recommendations, sim_active, sim_taxi