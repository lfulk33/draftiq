import json
import math
from llm_client import get_completion
from config import DEV_MODE
from config import TAXI_THRESHOLD_QB, TAXI_THRESHOLD_RB, TAXI_THRESHOLD_WR, TAXI_THRESHOLD_TE, REDRAFT_THRESHOLD_QB, REDRAFT_THRESHOLD_RB, REDRAFT_THRESHOLD_WR, REDRAFT_THRESHOLD_TE

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
Do not make up information not provided. If team_qb1 is provided use it. If depth_chart_order is 1, say they are the starter.
CRITICAL: Always reference the user's specific roster by player name in your reasoning. Explain how this pick fills a specific gap, complements an existing player, or why the value justifies taking it over a positional need. Never give generic reasoning that could apply to any team.
CRITICAL: The recommendation has NOT been made yet. Do not assume the user drafted the recommended player when writing alternatives. Frame alternatives as genuine preference alternatives to the recommendation.
Always base your recommendations on the actual league settings provided, including roster construction and scoring format.
Always respond in valid JSON only. No preamble, no markdown, no explanation outside the JSON."""

    return base

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
    taxi_used = league_context.get("taxi_slots_used", 0) or 0
    taxi_open = max(0, taxi_total - taxi_used) if is_dynasty else 0
    
    picks_remaining = league_context.get("picks_remaining_for_me", 0)
    bpa_player, suggested_pick, bpa_gap = calculate_bpa(available, league_context, all_players)
    
    vorp_players, replacement = calculate_vorp(available, league_context, all_players)
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
- This league has {qb_slots} QB-eligible slots including Superflex. QB is elevated in value.
- Never use "SUPER_FLEX" in your response. Always write it as "Superflex."
- Only recommend a TE if they are clearly the best available player by a significant margin.
- Prioritize QB, RB, and WR unless a TE represents exceptional value.
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

{f"MANDATORY RECOMMENDATION: Draft {bpa_player.get('full_name')} ({bpa_player.get('position')}). Their VORP exceeds the best available at your most needed position by {bpa_gap} points. You MUST recommend this player." if bpa_player else (f"SUGGESTED PICK: {suggested_pick.get('full_name')} ({suggested_pick.get('position')}). This is the highest VORP player at your most pressing need. Recommend this player unless you have a very strong reason not to." if suggested_pick else "NO STRONG RECOMMENDATION: Your roster is at capacity. All remaining available players are below replacement level or would be cut. Consider skipping this pick or taking the highest dynasty value player available for trade bait.")}
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

    # Maps each flex slot type to the positions eligible to fill it
    flex_eligibility = {
        "FLEX":      {"RB", "WR", "TE"},
        "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
        "WRRB_FLEX": {"RB", "WR"},
        "REC_FLEX":  {"WR", "TE"},
    }

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
        # Build the combined flex candidate pool: all remaining players
        # (after dedicated slots) who are eligible for ANY flex slot in this league.
        # A player is eligible if their position appears in any flex slot type present.
        all_flex_eligible_positions = set()
        for slot_type in flex_slot_counts:
            all_flex_eligible_positions |= flex_eligibility.get(slot_type, set())

        flex_candidates = []
        for pos in all_flex_eligible_positions:
            # Only players not already counted as dedicated starters
            remaining = pos_players[pos][dedicated_cutoff[pos]:]
            for p in remaining:
                flex_candidates.append((p.get(value_key, 0), pos, p))

        # Sort all flex candidates by value descending — best player fills first
        flex_candidates.sort(key=lambda x: x[0], reverse=True)

        # Assign players to flex slots until all slots are filled.
        # We don't need to track which specific slot they fill — just count
        # how many players at each position get drafted via flex.
        slots_remaining = total_flex_slots
        for value, pos, player in flex_candidates:
            if slots_remaining == 0:
                break
            # Check this player is actually eligible for at least one flex slot
            # present in this league (some leagues have WRRB_FLEX but no FLEX, etc.)
            eligible_for_this_league = any(
                pos in flex_eligibility.get(slot_type, set())
                for slot_type in flex_slot_counts
            )
            if eligible_for_this_league:
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

    if DEV_MODE:
        print(f"calculate_replacement_levels ({value_key}):")
        print(f"  dedicated_cutoff: {dedicated_cutoff}")
        print(f"  drafted_count after flex: {drafted_count}")
        print(f"  replacement levels: {replacement}")

    return replacement

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
    replacement = calculate_replacement_levels(league_context, player_pool, value_key)

    if DEV_MODE:
        print(f"calculate_vorp ({'dynasty' if is_dynasty else 'redraft'}):")
        print(f"  available size: {len(available)}, pool size: {len(player_pool)}")
        print(f"  replacement levels: {replacement}")

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

    return vorp_players, replacement

def _build_sim_state(all_picks, league_context):
    """
    Build a simulated roster state from all picks made so far.

    Routes each player to sim_active or sim_taxi based on their redraft value
    relative to positional taxi thresholds. In redraft leagues, taxi_slots_total
    is 0 so all players go to sim_active unconditionally.

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

    # Taxi thresholds: players below these redraft values are taxi candidates.
    # Only relevant when taxi_slots_total > 0 (dynasty leagues).
    taxi_thresholds = {
        "QB": TAXI_THRESHOLD_QB,
        "RB": TAXI_THRESHOLD_RB,
        "WR": TAXI_THRESHOLD_WR,
        "TE": TAXI_THRESHOLD_TE,
    }

    sim_active = {}
    sim_taxi = {}

    for p in all_picks:
        pid = p.get("id", p.get("name"))
        if not pid:
            continue

        pos = p.get("position", "?")
        redraft_val = p.get("redraft_value", 0) or 0
        pos_threshold = taxi_thresholds.get(pos, 100)

        # A player is taxi-eligible if they are a rookie (years_exp=0)
        # or the league allows veterans on taxi
        taxi_eligible = p.get("years_exp", 99) == 0 or taxi_allow_vets == 1

        if redraft_val >= pos_threshold:
            # Above taxi threshold — proven redraft value, goes to active roster
            sim_active[pid] = p
        elif taxi_eligible and len(sim_taxi) < taxi_slots_total:
            # Below threshold, taxi eligible, taxi not full — goes to taxi
            sim_taxi[pid] = p
        else:
            # Below threshold but not taxi eligible, or taxi full —
            # goes to active bench regardless (veteran with low value)
            sim_active[pid] = p

    return sim_active, sim_taxi


   
def _calculate_scarcity(viable, picks_by_pos, league_context):
    """
    Determine the most urgently needed position using a scarcity score
    that combines supply/demand with value drop-off.

    Scarcity score for each position =
        (slots_still_needed / players_still_available) * (1 + drop_off_ratio)

    Where drop_off_ratio = (best_value - avg_rest) / (avg_rest + 1)

    This correctly captures two dimensions of urgency:
      1. Supply/demand: if you need 2 QBs and only 3 are left, that's
         more urgent than needing 2 WRs with 20 left.
      2. Value drop-off: if Brock Bowers is available and the next TE
         is worth half as much, TE scarcity score spikes — grab him now.

    Works identically for dynasty and redraft. The underlying values
    differ (fc_value vs fc_redraft_value) but the math is the same.

    Args:
        viable:         list of {player, vorp, value, position} — players
                        who would make the roster, sorted by vorp desc
        picks_by_pos:   dict of {pos: count} from _count_picks_by_pos
        league_context: dict from build_league_context

    Returns:
        most_needed_pos: string position with highest scarcity score,
                         or None if all positions are at capacity
        position_scarcity: dict of {pos: score} for debugging
    """
    is_dynasty = league_context.get("is_dynasty", True)
    dedicated = league_context.get("dedicated_slots", {})
    backup_needs = league_context.get("backup_needs", {})
    has_superflex = league_context.get("has_superflex", False)

    # In redraft, only count QB backups if the league has a Superflex slot.
    # A backup QB has no lineup value in a 1-QB league.
    effective_backup_needs = dict(backup_needs)
    if not is_dynasty and not has_superflex:
        effective_backup_needs["QB"] = 0

    # Positions where we still need more players
    needed_positions = [
        pos for pos in ["QB", "RB", "WR", "TE"]
        if picks_by_pos.get(pos, 0) < dedicated.get(pos, 0) + effective_backup_needs.get(pos, 0)
    ]

    if not needed_positions:
        return None, {}

    # Count remaining viable players and their values by position
    viable_by_pos = {}
    viable_values_by_pos = {}
    for v in viable:
        pos = v["position"]
        val = v["player"].get(
            "fc_value" if is_dynasty else "fc_redraft_value", 0
        ) or 0
        viable_by_pos[pos] = viable_by_pos.get(pos, 0) + 1
        viable_values_by_pos.setdefault(pos, []).append(val)

    position_scarcity = {}
    for pos in needed_positions:
        slots_needed = (
            dedicated.get(pos, 0) +
            effective_backup_needs.get(pos, 0) -
            picks_by_pos.get(pos, 0)
        )
        players_left = viable_by_pos.get(pos, 0)
        values = viable_values_by_pos.get(pos, [0])

        if players_left == 0:
            # No players left at this position — maximally scarce
            position_scarcity[pos] = float('inf')
            continue

        # Supply/demand ratio: how many slots per available player
        supply_ratio = slots_needed / players_left

        # Value drop-off: how much better is the best player vs the rest
        # High drop-off means "grab him now or regret it"
        best_val = max(values) if values else 0
        rest_vals = values[1:] if len(values) > 1 else [0]
        avg_rest = sum(rest_vals) / len(rest_vals) if rest_vals else 0
        dropoff = (best_val - avg_rest) / (avg_rest + 1)  # +1 avoids division by zero

        position_scarcity[pos] = supply_ratio * (1 + dropoff)

    if not position_scarcity:
        return None, {}

    most_needed_pos = max(position_scarcity, key=lambda p: position_scarcity[p])

    if DEV_MODE:
        print(f"_calculate_scarcity:")
        print(f"  needed_positions: {needed_positions}")
        print(f"  picks_by_pos: {picks_by_pos}")
        print(f"  position_scarcity: {position_scarcity}")
        print(f"  most_needed_pos: {most_needed_pos}")

    return most_needed_pos, position_scarcity


def _bpa_decision_redraft(best_overall, best_needed, threshold):
    """
    BPA decision for redraft leagues.

    Simple comparison: if the best overall player's VORP exceeds the best
    player at the needed position by more than the threshold, recommend
    best_overall (pure value). Otherwise recommend best_needed (positional).

    No taxi logic needed — redraft has no taxi squad.

    Args:
        best_overall: viable dict for highest VORP player ignoring capacity
        best_needed:  viable dict for highest VORP player at needed position
        threshold:    BPA_THRESHOLD_REDRAFT from league_context

    Returns:
        (bpa_player, suggested_pick, gap) where:
          bpa_player is non-None only when BPA overrides positional need
          suggested_pick is always the recommended player
          gap is the VORP difference between best_overall and best_needed
    """
    if not best_overall:
        return None, None, None
    if best_needed is None:
        return None, best_overall["player"], 0

    gap = best_overall["vorp"] - best_needed["vorp"]

    # BPA fires only when the value gap is significant AND they are different players
    if (gap > threshold and
            best_overall["player"].get("full_name") != best_needed["player"].get("full_name")):
        return best_overall["player"], best_needed["player"], gap

    return None, best_needed["player"], gap


def _bpa_decision_dynasty(best_overall, best_needed, viable, sim_taxi,
                           league_context, threshold, picks_by_pos=None):
    """
    BPA decision for dynasty leagues.

    More complex than redraft because of taxi squads. A high-VORP rookie
    below the redraft threshold might be worth taking as a taxi stash even
    if they would not make the active roster — that's a dynasty-specific
    concept with no redraft equivalent.

    Decision tree:
      1. If taxi is full: only consider players with real redraft value
         (they must make the active roster). Pick best at needed position
         unless BPA gap exceeds threshold.
      2. If taxi has space: consider taxi stashes (high dynasty value,
         low redraft value, years_exp=0) as legitimate picks. If the
         best taxi stash has higher VORP than the best needed player,
         recommend the stash.
      3. Standard BPA: compare best_overall vs best_needed against threshold.

    Args:
        best_overall:   viable dict for highest VORP player ignoring capacity
        best_needed:    viable dict for highest VORP player at needed position
        viable:         full viable list for taxi stash search
        sim_taxi:       current taxi squad state
        league_context: dict from build_league_context
        threshold:      BPA_THRESHOLD_DYNASTY from league_context

    Returns:
        (bpa_player, suggested_pick, gap) — same contract as _bpa_decision_redraft
    """
    taxi_slots_total = league_context.get("taxi_slots_total", 0) or 0
    taxi_allow_vets = league_context.get("taxi_allow_vets", 0)
    taxi_full = len(sim_taxi) >= taxi_slots_total
    picks_by_pos = picks_by_pos or {}
    dedicated = league_context.get("dedicated_slots", {})
    backup_needs = league_context.get("backup_needs", {})
    has_superflex = league_context.get("has_superflex", False)
    effective_backup_needs = dict(backup_needs)

    taxi_thresholds = {
        "QB": TAXI_THRESHOLD_QB,
        "RB": TAXI_THRESHOLD_RB,
        "WR": TAXI_THRESHOLD_WR,
        "TE": TAXI_THRESHOLD_TE,
    }

    def has_redraft_value(v):
        """Player has enough redraft value to contribute to active roster."""
        pos = v["position"]
        fc_redraft = v["player"].get("fc_redraft_value")
        if fc_redraft is None:
            return False
        return fc_redraft >= taxi_thresholds.get(pos, 100)

    # Split viable into players with active roster value vs taxi-only players
    redraft_viable = [v for v in viable if has_redraft_value(v)]
    dynasty_viable = [v for v in viable if not has_redraft_value(v)]

    if DEV_MODE:
        print(f"_bpa_decision_dynasty:")
        print(f"  taxi_full: {taxi_full}, taxi: {len(sim_taxi)}/{taxi_slots_total}")
        print(f"  redraft_viable top 3: {[(v['player'].get('full_name'), round(v['vorp'])) for v in redraft_viable[:3]]}")
        print(f"  dynasty_viable top 3: {[(v['player'].get('full_name'), round(v['vorp'])) for v in dynasty_viable[:3]]}")

    if taxi_full:
        # Taxi is full — can only pick players who make the active roster.
        # Dynasty rookies with no redraft value would just be cut.
        if redraft_viable:
            best_redraft_needed = next(
                (v for v in redraft_viable if v["position"] == best_needed["position"])
                if best_needed else iter([]),
                None
            )
            best_redraft_overall = redraft_viable[0]
            if best_redraft_needed is None:
                return None, best_redraft_overall["player"], 0
            gap = best_redraft_overall["vorp"] - best_redraft_needed["vorp"]
            if gap > threshold and best_redraft_overall["position"] != best_needed["position"] if best_needed else False:
                return best_redraft_overall["player"], best_redraft_needed["player"], gap
            return None, best_redraft_needed["player"], 0
        elif dynasty_viable:
            # No redraft value left — only worth recommending if the player
            # has better dynasty value than the worst current taxi player,
            # AND their position isn't already at capacity.
            at_capacity_positions = [
                pos for pos in ["QB", "RB", "WR", "TE"]
                if picks_by_pos.get(pos, 0) >= dedicated.get(pos, 0) + effective_backup_needs.get(pos, 0)
            ]

            # Find the worst taxi player's dynasty value — the bar to clear
            worst_taxi_value = min(
                (p.get("dynasty_value", 0) for p in sim_taxi.values()),
                default=0
            )

            # Only recommend players who beat the worst taxi player
            # AND aren't at a position already at capacity
            upgrades = [
                v for v in dynasty_viable
                if v["player"].get("fc_value", 0) > worst_taxi_value
                and v["position"] not in at_capacity_positions
            ]

            if DEV_MODE:
                print(f"  taxi_full dynasty branch - at_capacity: {at_capacity_positions}")
                print(f"  worst_taxi_value: {worst_taxi_value}")
                print(f"  upgrades: {[(v['player'].get('full_name'), v['position'], v['player'].get('fc_value')) for v in upgrades[:3]]}")

            if upgrades:
                return None, upgrades[0]["player"], 0

            # No taxi upgrade available — just take the best available player
            # at a needed position, even if below replacement level.
            # In late rounds of a startup draft you always need to make a pick.
            non_capacity_dynasty = [
                v for v in dynasty_viable
                if v["position"] not in at_capacity_positions
            ]
            if non_capacity_dynasty:
                return None, non_capacity_dynasty[0]["player"], 0
            # All positions at capacity — take best dynasty value regardless
            return None, dynasty_viable[0]["player"], 0
        return None, viable[0]["player"] if viable else None, 0

    else:
        # Taxi has space — taxi stashes are legitimate picks.
        # Only consider actual taxi-eligible players as stashes
        actual_taxi_stashes = [
            v for v in dynasty_viable
            if v["player"].get("years_exp", 99) == 0 or taxi_allow_vets == 1
        ]

        if not best_needed:
            # All positions at capacity — take best overall or best taxi stash
            if actual_taxi_stashes and actual_taxi_stashes[0]["vorp"] > (best_overall["vorp"] if best_overall else 0):
                return None, actual_taxi_stashes[0]["player"], 0
            return None, best_overall["player"] if best_overall else None, 0

        # Check if a taxi stash has higher VORP than the needed position pick.
        # Exclude positions already at capacity from taxi stash consideration —
        # no point stashing a 9th QB when you already have 8.
        at_capacity_positions = [
            pos for pos in ["QB", "RB", "WR", "TE"]
            if picks_by_pos.get(pos, 0) >= dedicated.get(pos, 0) + effective_backup_needs.get(pos, 0)
        ]
        if DEV_MODE:
            print(f"  taxi stash at_capacity check: {at_capacity_positions}")
            print(f"  picks_by_pos: {picks_by_pos}")
            print(f"  dedicated: {dedicated}")
            print(f"  effective_backup_needs: {effective_backup_needs}")
            print(f"  actual_taxi_stashes: {[(v['player'].get('full_name'), v['position'], round(v['vorp'])) for v in actual_taxi_stashes[:5]]}")
        viable_taxi_stashes = [
            v for v in actual_taxi_stashes
            if v["position"] not in at_capacity_positions
        ]
        if viable_taxi_stashes and viable_taxi_stashes[0]["vorp"] > best_needed["vorp"]:
            return None, viable_taxi_stashes[0]["player"], 0

        if not best_overall:
            return None, best_needed["player"], 0

        gap = best_overall["vorp"] - best_needed["vorp"]
        if gap > threshold and best_overall["position"] != best_needed["position"]:
            return best_overall["player"], best_needed["player"], gap

        return None, best_needed["player"], gap


def _find_candidates(viable, most_needed_pos, picks_by_pos, league_context):
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
    is_dynasty = league_context.get("is_dynasty", True)
    dedicated = league_context.get("dedicated_slots", {})
    backup_needs = league_context.get("backup_needs", {})
    has_superflex = league_context.get("has_superflex", False)

    effective_backup_needs = dict(backup_needs)
    if not is_dynasty and not has_superflex:
        effective_backup_needs["QB"] = 0

    # Positions where we have enough players — exclude from BPA consideration
    # so we don't recommend a 4th WR when RB is empty
    at_capacity_positions = [
        pos for pos in ["QB", "RB", "WR", "TE"]
        if picks_by_pos.get(pos, 0) >= dedicated.get(pos, 0) + effective_backup_needs.get(pos, 0)
    ]

    # Best player at the most urgently needed position
    best_needed = None
    if most_needed_pos:
        best_needed = next(
            (v for v in viable if v["position"] == most_needed_pos),
            None
        )
        # Fall back to any needed position if most_needed_pos has no players
        if best_needed is None:
            needed_positions = [
                pos for pos in ["QB", "RB", "WR", "TE"]
                if picks_by_pos.get(pos, 0) < dedicated.get(pos, 0) + effective_backup_needs.get(pos, 0)
            ]
            best_needed = next(
                (v for v in viable if v["position"] in needed_positions),
                None
            )

    # Best player overall by VORP — no position exclusions.
    # The BPA threshold handles over-drafting naturally:
    # as a position fills up, the gap between best_overall and best_needed
    # shrinks and BPA stops firing without needing hard exclusions.
    best_overall = viable[0] if viable else None

    if DEV_MODE:
        print(f"_find_candidates:")
        print(f"  at_capacity_positions: {at_capacity_positions}")
        print(f"  best_needed: {best_needed['player'].get('full_name') if best_needed else None}, vorp: {round(best_needed['vorp']) if best_needed else None}")
        print(f"  best_overall: {best_overall['player'].get('full_name') if best_overall else None}, vorp: {round(best_overall['vorp']) if best_overall else None}")
        # Top 5 by VORP at each position — permanent tuning log
        for pos in ["QB", "RB", "WR", "TE"]:
            top = [(v['player'].get('full_name'), round(v['vorp'])) for v in viable if v['position'] == pos][:5]
            if top:
                print(f"  top {pos}: {top}")
    return best_needed, best_overall


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
    is_dynasty = league_context.get("is_dynasty", True)

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
    Filter the VORP-sorted player list down to only players who would
    actually make the roster — i.e. not be immediately cut.

    Runs each candidate through simulate_placement against the current
    sim state. Players who would be CUT are excluded. This prevents the
    BPA logic from recommending players who have no roster spot.

    Identical for dynasty and redraft — simulate_placement handles the
    mode differences internally based on taxi_slots_total.

    Args:
        sorted_vorp:    list of {player, vorp, value, position} sorted by
                        vorp descending, from calculate_vorp
        sim_active:     dict of {player_id: player} on active roster
        sim_taxi:       dict of {player_id: player} on taxi squad
        league_context: dict from build_league_context

    Returns:
        list of viable player dicts in same format as sorted_vorp,
        with CUT players removed
    """
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
        placement, _ = simulate_placement(candidate, sim_active, sim_taxi, league_context)
        if placement != "CUT":
            viable.append(v)
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
    vorp_players, replacement = calculate_vorp(available, league_context, all_players)
    
    if not vorp_players:
        return None, None, None

    # Step 2: Build simulated roster state from all picks made so far
    all_picks = (
        league_context.get("my_picks_this_draft", []) +
        league_context.get("my_existing_roster", [])
    )
    sim_active, sim_taxi = _build_sim_state(all_picks, league_context)

    if DEV_MODE:
        print(f"  sim_active: {len(sim_active)}, sim_taxi: {len(sim_taxi)}")
        print(f"  sim_active players: {[(p.get('name'), p.get('position'), p.get('redraft_value')) for p in sim_active.values()]}")
        print(f"  sim_taxi players: {[(p.get('name'), p.get('position'), p.get('redraft_value')) for p in sim_taxi.values()]}")

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
        # No viable picks — all remaining players would be cut
        # Return highest VORP player anyway so Claude can explain the situation
        if vorp_players:
            best = max(vorp_players, key=lambda x: x["vorp"])
            return None, best["player"], 0
        return None, None, None

    # Step 4: Count meaningful players at each position
    picks_by_pos = _count_picks_by_pos(sim_active, league_context)

    # Step 5: Calculate positional scarcity
    most_needed_pos, position_scarcity = _calculate_scarcity(
        viable, picks_by_pos, league_context
    )

    # Step 6: Find the two key candidates
    best_needed, best_overall = _find_candidates(
        viable, most_needed_pos, picks_by_pos, league_context
    )

    # Scale threshold based on how many players we have over dedicated slots
    # at the SAME position as best_overall. Prevents BPA from spamming one position.
    # Each player owned beyond dedicated slots doubles the required gap.
    dedicated = league_context.get("dedicated_slots", {})
    if best_overall:
        bpa_pos = best_overall["position"]
        owned = picks_by_pos.get(bpa_pos, 0)
        slots = dedicated.get(bpa_pos, 0)
        if owned > slots:
            # Linear scaling: each extra player adds 50% to the threshold.
            # 1 over: 1.5x, 2 over: 2x, 3 over: 2.5x, 4 over: 3x
            # Much gentler than exponential but still meaningful protection.
            threshold = threshold * (1 + (0.5 * (owned - slots)))

    if DEV_MODE:
        print(f"  scaled threshold: {threshold}")

    # Step 7: Mode-specific BPA decision
    if is_dynasty:
        return _bpa_decision_dynasty(
            best_overall, best_needed, viable, sim_taxi, league_context, threshold,
            picks_by_pos=picks_by_pos
        )
    else:
        return _bpa_decision_redraft(best_overall, best_needed, threshold)

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