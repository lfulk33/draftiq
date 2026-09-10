import json
import math
import random
from datetime import date
import adp_client
from llm_client import get_completion
from config import DEV_MODE
from config import TAXI_THRESHOLD_QB, TAXI_THRESHOLD_RB, TAXI_THRESHOLD_WR, TAXI_THRESHOLD_TE, REDRAFT_THRESHOLD_QB, REDRAFT_THRESHOLD_RB, REDRAFT_THRESHOLD_WR, REDRAFT_THRESHOLD_TE, URGENCY_MODIFIER, DEFAULT_MODEL, TE_FLEX_ONLY_VALUE_DISCOUNT, SALARY_COMFORTABLE_PER_SLOT

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
    today = date.today().strftime("%B %-d, %Y")
    base = f"""You are an expert {mode_label} fantasy football analyst and draft advisor for the 2026 NFL season.
Today's date is {today}. Your own training data has a cutoff before this date — treat any belief you have about a player's team, role, age, experience, or teammates as potentially outdated. The player data provided below is the current, correct source of truth as of today; when it conflicts with what you already "know," the provided data wins, always.
{analyst_focus}
Use the team, team_qb1, depth_chart_order, and other teammate fields provided to mention specific offensive context, target share, and role clarity.
Write like a fantasy analyst on a podcast — specific, enthusiastic, and grounded in real situation awareness.
{mode_instruction}
Never use the word "VORP" anywhere in your response, not even in phrases like "VORP gap" or "VORP advantage." Instead say things like "biggest gap on the board", "best value available", "most underpriced player at this position", or "the board falls off a cliff after him."
Never mention raw numerical values like dynasty value scores, redraft values, or ranking numbers. Translate those into plain language instead — "one of the top assets at the position", "elite value at this pick", "consensus top-5 at his position", "clear drop-off after him on the board."
You may use widely established player nicknames and abbreviations only when they are universally recognized in fantasy football — "JSN" for Jaxon Smith-Njigba, "CMC" for Christian McCaffrey. Never invent abbreviations or use the wrong initials. When in doubt, use the full name.
A player's years_exp determines rookie/veteran status in both directions: years_exp of 1 or more means veteran, even if young — never call them a rookie. years_exp of exactly 0 means rookie — never call them a veteran, "proven," "established," or otherwise imply NFL experience they don't have, even if they profile as NFL-ready.
Never abbreviate team names — always use the full city or team abbreviation as provided in the player data (e.g. "NYG", "SF", "LAR").
CRITICAL: Players change teams via trades, free agency, and releases. The "team" field in the player data is their CURRENT team as of right now — always use it, even if it contradicts a team you associate with that player from past seasons or general knowledge. Never state or imply a player's team without checking the provided "team" field first.
CRITICAL: Never name a specific teammate, backfield-mate, or "alongside X" pairing unless X appears in that exact player's own team_qb1/team_rb1/team_wr1/team_te1 fields, or is explicitly listed elsewhere in the provided data for that player. Do not invent or assume who else is on a player's team from your own knowledge — rosters change, and a name you associate with a team may no longer play there or may never have played there at all.
Do not make up information not provided. If team_qb1 is provided use it. If depth_chart_order is 1, say they are the starter — describe this in plain English ("he's QB1", "he's the starter", "he's third on the depth chart"), never by writing the literal field name.
CRITICAL: If depth_chart_order is 2 or higher, this player is currently listed BEHIND someone else at his position on his own team — say so plainly ("he's the backup," "he's currently RB2," "third on the depth chart") and do NOT call him a starter, bell-cow, workhorse, lead back, or three-down back, even if you recall him having that role in a past season. A player's role can change year to year via injury, competition, or scheme changes — depth_chart_order as provided is the current, correct source of truth and overrides any role you associate with him from memory. If you want to frame him as a value/trade/flex play specifically because he's a backup, that's fine — just don't contradict the depth chart while doing it.
CRITICAL: Never write a raw JSON field/variable name in your response — not "depth_chart_order", not "years_exp", not any other key from the data you were given. Always translate data into plain, natural language. If you catch yourself about to type an underscore_separated_name, stop and rephrase it as a normal sentence instead.
CRITICAL: When comparing two players' ages (e.g. "X years younger/older than Y"), subtract their exact provided ages precisely before writing the comparison. Do not estimate or round the gap — state the real difference (e.g. two players at 26.8 and 30.8 are four years apart, not one).
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

    # Precomputed have/total/filled per position — matches the frontend's own
    # Roster Needs widget exactly (dedicated slots + backup slots vs actual
    # drafted count). Given to Claude directly so it never has to reconstruct
    # this arithmetic itself from separate JSON blocks, which is exactly
    # where it has previously gotten it wrong (e.g. claiming "zero TE depth"
    # when a backup TE was already rostered).
    roster_construction_detail = league_context.get("roster_construction_detail", {})
    backup_needs_map = league_context.get("backup_needs", {})
    all_drafted = league_context.get("my_picks_this_draft", []) + league_context.get("my_existing_roster", [])
    roster_needs_summary = {}
    for pos in ["QB", "RB", "WR", "TE"]:
        dedicated_slots_count = roster_construction_detail.get(pos, {}).get("dedicated_slots", 0)
        backup = backup_needs_map.get(pos, 0)
        total_need = dedicated_slots_count + backup
        have = sum(1 for p in all_drafted if p.get("position") == pos)
        remaining = max(0, total_need - have)
        status = "FILLED — do not draft another here unless the value is truly exceptional" if remaining == 0 else f"NEEDS {remaining} MORE"
        roster_needs_summary[pos] = f"{have}/{total_need} ({status})"

    bpa_player, suggested_pick, bpa_gap, trade_bait_players, _, _, _, _, _, _ = calculate_bpa(available, league_context, all_players)
    final_pick = bpa_player or suggested_pick
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
    vorp_players = _apply_salary_adjustment(vorp_players, league_context)
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

ROSTER NEEDS SUMMARY (have / dedicated+backup slots needed — already computed, trust this completely):
{json.dumps(roster_needs_summary, indent=2)}
CRITICAL: This summary already counts every player you have at each position, including backups and flex-only players. Never state or imply a position has "no depth," "no backup," or is a gap if this summary shows it FILLED — that is a direct contradiction of data you were given. If a position shows FILLED, only recommend adding another there for truly exceptional value or as a trade asset (use the trade_bait field for that, not the main recommendation).

NOTE: Use the ROSTER CONSTRUCTION DETAIL above to determine how more players break down between dedicated and flex slots. NEVER reference "starter_needs" by name. NEVER add dedicated slots and flex slots together into a single number. Always state them separately, e.g. "2 dedicated RB slots plus 2 flex slots eligible for RB." Do not say "4 RB slots" or "4 flex-eligible slots."
{chr(10).join([f"- TRADE BAIT OPTION ({t['type'].upper()}): {t['name']} ({t['position']}) is the highest {'dynasty' if t['type'] == 'dynasty' else 'redraft'} value player on the board but your {t['position']} slots are full. You MUST include him in the trade_bait array with a compelling 1-2 sentence reason that explains his specific value, why he is worth drafting despite your {t['position']} depth, and what you could realistically get in a trade for him. Do NOT also include him in the alternatives array." for t in trade_bait_players if t['name'] != (suggested_pick.get('full_name') if suggested_pick else None)])}

{f'THE RECOMMENDATION HAS ALREADY BEEN DECIDED BY THE SCORING SYSTEM: {final_pick.get("full_name")} ({final_pick.get("position")}). This is not a suggestion for you to weigh — it is the answer. Your only job is to write "reasoning", "positional_note", and "upside" that explain, in specific and compelling terms, why this player and this pick make sense right now — using their real team, role, teammates, age, and how they fit this exact roster. Do NOT recommend a different player. Do NOT let ROSTER CONSTRUCTION DETAIL, PLAYERS ALREADY DRAFTED, or your own read of positional need change the recommendation — those are context for your prose, not new inputs to a decision that has already been made. The "recommendation" field in your JSON response MUST be exactly "{final_pick.get("full_name")}."' if final_pick else "NO STRONG RECOMMENDATION: Your roster is at capacity. All remaining available players are below replacement level or would be cut. Pick the single best trade-bait-caliber player as your recommendation, framed around his trade value rather than a roster need."}
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
    return prompt, final_pick

def get_recommendation(picks, available, my_roster, league_context, pick_number, all_players=None):
    prompt, final_pick = build_prompt(picks, available, my_roster, league_context, pick_number, all_players)
    is_dynasty = league_context.get("is_dynasty", True)
    response = get_completion(prompt, model_key=DEFAULT_MODEL, system=get_system_prompt(is_dynasty))

    try:
        rec = json.loads(response)
    except json.JSONDecodeError:
        clean = response.strip().removeprefix("```json").removesuffix("```").strip()
        rec = json.loads(clean)

    # The scoring system decides which player, not Claude — force the
    # recommendation to match regardless of what Claude wrote. Claude's job
    # is the reasoning/positional_note/upside prose, never the pick itself.
    if final_pick:
        rec["recommendation"] = final_pick.get("full_name")
        rec["position"] = final_pick.get("position")
        salary_cap = league_context.get("salary_cap")
        if salary_cap:
            rec["salary"] = salary_cap["salaries"].get(final_pick.get("player_id"))

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

    # Enrich alternatives with team (and salary, for salary-cap leagues)
    salary_cap = league_context.get("salary_cap")
    for alt in rec.get("alternatives", []):
        alt_matched = next(
            (p for p in available.values() if p.get("full_name") == alt.get("name")),
            None
        )
        alt["team"] = alt_matched.get("team") if alt_matched else None
        if salary_cap and alt_matched:
            alt["salary"] = salary_cap["salaries"].get(alt_matched.get("player_id"))

    # The whole draft is either dynasty or redraft — never a per-entry choice.
    # Claude sometimes fabricates a trade_bait entry not present in the
    # server-computed hint (e.g. when the recommended player is itself the
    # trade bait candidate), and has no anchor for "type" in that case.
    # Force it to the actual league mode rather than trust whatever it wrote.
    for tb in rec.get("trade_bait", []):
        tb["type"] = "dynasty" if is_dynasty else "redraft"
        if salary_cap:
            tb_matched = next(
                (p for p in available.values() if p.get("full_name") == tb.get("name")),
                None
            )
            if tb_matched:
                tb["salary"] = salary_cap["salaries"].get(tb_matched.get("player_id"))

    return rec

def _effective_value(player, value_key, league_context):
    """
    Value used for VORP/replacement-level purposes. Identical to the raw
    value_key field except for TE in a league with zero dedicated TE slots,
    where it's discounted by TE_FLEX_ONLY_VALUE_DISCOUNT (see config.py for
    why — FantasyCalc has no way to price TE for a league where it's purely
    flex-competitive, not a required position).
    """
    raw = player.get(value_key, 0) or 0
    if player.get("position") == "TE" and league_context.get("dedicated_slots", {}).get("TE", 0) == 0:
        return raw * (1 - TE_FLEX_ONLY_VALUE_DISCOUNT)
    return raw


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
            key=lambda x: _effective_value(x, value_key, league_context),
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
        #
        # Cross-position comparability (e.g. QB vs RB/WR/TE for a SUPER_FLEX
        # slot) is handled upstream now: for redraft leagues, `value_key`
        # values are already real-points-equivalent (see
        # historical_stats.apply_real_points_translation), so a plain value
        # comparison here is already on a fair, cross-position-comparable
        # scale. No position-specific special-casing needed in this step.
        for slot_type, count in flex_slot_counts.items():
            eligible_positions = FLEX_ELIGIBILITY.get(slot_type, set())
            slots_for_this_type = count * num_teams

            # Build candidate pool for this specific slot type
            flex_candidates = []
            for pos in eligible_positions:
                remaining = pos_players[pos][drafted_count[pos]:]
                for p in remaining:
                    flex_candidates.append((_effective_value(p, value_key, league_context), pos, p))

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
            replacement[pos] = _effective_value(players_at_pos[cutoff], value_key, league_context)
        elif players_at_pos:
            # Everyone at this position is already drafted — use the last player
            replacement[pos] = _effective_value(players_at_pos[-1], value_key, league_context)
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
        val = _effective_value(p, value_key, league_context)

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

def _team_position_counts(roster_id, all_picks, players, rosters_by_id=None):
    """
    How many players a given roster currently has at each skill position —
    this draft's picks plus any pre-existing roster (dynasty carryover).
    Redraft leagues start every roster empty, so all_picks alone covers it;
    dynasty leagues need the existing roster too, hence rosters_by_id.
    """
    counts = {"QB": 0, "RB": 0, "WR": 0, "TE": 0}
    seen_ids = set()
    for p in all_picks or []:
        if p.get("roster_id") != roster_id or not p.get("player_id") or p.get("is_keeper"):
            continue
        pid = p["player_id"]
        pos = players.get(pid, {}).get("position")
        if pos in counts:
            counts[pos] += 1
        seen_ids.add(pid)
    roster = (rosters_by_id or {}).get(roster_id)
    if roster:
        for pid in (roster.get("players") or []):
            if pid in seen_ids:
                continue
            pos = players.get(pid, {}).get("position")
            if pos in counts:
                counts[pos] += 1
    return counts


def _excluded_from_best(player, strict_starter_health):
    """
    True if this player shouldn't be allowed to represent "the best
    option" for a position — either a manual backup_only override (see
    player_overrides.py; applies in every league, deliberate and
    individually verified, e.g. a Best Ball skeptical-injury-timeline
    call) or, only when strict_starter_health is on (Chopped-only), any
    current real Sleeper injury_status. The override check is
    unconditional on purpose: a Best Ball league wants ceiling/risk
    tolerance for injuries in general, so it must never blanket-exclude
    every Sleeper-flagged injury — only the specific players actually
    placed on the list.
    """
    if player.get("is_backup_only_override"):
        return True
    return bool(strict_starter_health and player.get("injury_status"))


def _best_healthy_first(pool, strict_starter_health, pos=None):
    """
    Best player in `pool` (already VORP-sorted descending), preferring a
    non-excluded one (see _excluded_from_best) — same reasoning as the
    best_now/best_after health gate in _calculate_urgency, applied here to
    the actual candidate selection (best_overall/best_needed/position_best)
    instead of just the opportunity-cost math. Without this, an injured
    or overridden player with the top raw VORP at his position still wins
    the final recommendation outright — the opportunity-cost fix alone
    only stops him from inflating OTHER candidates' scores, it doesn't
    stop him from winning on his own real VORP.

    Falls back to the unfiltered top if no eligible candidate exists at
    all (never make a position vanish from consideration entirely).
    pos=None means "best overall," not restricted to one position.
    """
    candidates = pool if pos is None else [v for v in pool if v["position"] == pos]
    eligible = [v for v in candidates if not _excluded_from_best(v["player"], strict_starter_health)]
    if eligible:
        return eligible[0]
    return candidates[0] if candidates else None


def _team_open_dedicated_positions(counts, dedicated_slots):
    """Positions where this team hasn't yet filled its dedicated starter slots."""
    return [
        pos for pos in ("QB", "RB", "WR", "TE")
        if counts.get(pos, 0) < dedicated_slots.get(pos, 0)
    ]


def _upcoming_pick_roster_ids(current_pick_number, count, num_teams, slot_to_roster_id):
    """
    Real snake-draft roster order for the next `count` picks, STARTING AT
    current_pick_number itself (the next pick to actually happen — not
    mine, since picks_until_next is defined as the gap before my own next
    turn). Mirrors the forward/reverse round math used elsewhere for
    "picks until my next turn" — same idea, run forward instead of solved
    for one slot. Off-by-one here would either drop a real opponent pick
    from the front of the window or leak my own next pick into it.
    """
    def _slot_for_pick_number(pick_number, teams):
        round_num = (pick_number - 1) // teams + 1
        pos_in_round = (pick_number - 1) % teams + 1
        return pos_in_round if round_num % 2 == 1 else teams - pos_in_round + 1

    roster_ids = []
    for offset in range(count):
        slot = _slot_for_pick_number(current_pick_number + offset, num_teams)
        roster_id = slot_to_roster_id.get(str(slot), slot_to_roster_id.get(slot))
        roster_ids.append(roster_id)
    return roster_ids


def _default_stdev(adp):
    """
    Real ADP variance grows with draft depth. No data source gives us a
    per-player stdev for Sleeper-specific entries (BeatADP) or for anyone
    FFC doesn't cover at all — this is a deliberately soft heuristic
    scaled to draft position rather than pretending zero variance for
    those players.
    """
    return max(3.0, adp * 0.12)


def _simulate_team_aware_best_after(viable_active, adp_map, pick_sequence, all_picks, players, dedicated_slots, rosters_by_id, needed_positions, seed, num_simulations=150, strict_starter_health=False):
    """
    Monte Carlo version of the team-aware "who's taken" simulation. ADP is
    a mean, not a guarantee — a specific team can reach for a player well
    outside generic expectation (one GM just likes Waddle more than the
    market does). A deterministic top-N cutoff treats that as impossible:
    anyone just past the cutoff shows exactly 0 risk, anyone just before
    it shows 100%. Real drafts don't have a cliff there.

    Each run jitters every player's effective ADP by their real stdev
    (from FFC; a draft-depth-scaled default — see _default_stdev — for
    anyone without one, e.g. BeatADP-only matches), sorts by that jittered
    order, and runs the same per-team need-aware pick logic as before on
    it. Averaging best-remaining-VORP per position across many runs turns
    the hard cliff into a smooth probability: a player just outside the
    "expected" cutoff now contributes a real, usually-small, nonzero
    chance of being gone instead of a guaranteed survival.

    Seeded deterministically from the current draft state (not global
    random) so repeated calls against an unchanged board return identical
    numbers — this models uncertainty that's already there, it doesn't
    inject fresh noise on every request.
    """
    rng = random.Random(seed)
    team_counts_base = {
        rid: _team_position_counts(rid, all_picks, players, rosters_by_id)
        for rid in set(pick_sequence) if rid is not None
    }

    totals = {pos: 0.0 for pos in needed_positions}
    for _ in range(num_simulations):
        def _jittered_key(v, _rng=rng):
            entry = adp_map.get(v["player"].get("player_id")) if adp_map else None
            if not entry:
                return 9999 + _rng.random()
            mean = entry["adp"]
            stdev = entry.get("stdev") or _default_stdev(mean)
            return _rng.gauss(mean, stdev)

        pool = sorted(viable_active, key=_jittered_key)
        team_counts = {rid: dict(counts) for rid, counts in team_counts_base.items()}

        for roster_id in pick_sequence:
            if not pool:
                break
            pick = None
            if roster_id is not None:
                open_positions = _team_open_dedicated_positions(team_counts.get(roster_id, {}), dedicated_slots)
                pick = next((v for v in pool if v["position"] in open_positions), None)
            if pick is None:
                pick = pool[0]
            pool.remove(pick)
            if roster_id is not None:
                team_counts.setdefault(roster_id, {})
                team_counts[roster_id][pick["position"]] = team_counts[roster_id].get(pick["position"], 0) + 1

        for pos in needed_positions:
            # A player still gets realistically drafted by other teams in
            # the simulation above (an injured player is a real speculative
            # target) — but he shouldn't be treated as YOUR surviving best
            # option for a starter slot you can't afford to gamble on.
            candidates = [v for v in pool if not _excluded_from_best(v["player"], strict_starter_health)]
            best = next((v for v in candidates if v["position"] == pos), None)
            totals[pos] += best["vorp"] if best else 0

    return {pos: totals[pos] / num_simulations for pos in needed_positions}


def _calculate_urgency(viable, picks_by_pos, league_context, drafted_count=None, dedicated_cutoff=None, sim_taxi=None, sim_active=None, replacement=None, adp_map=None, all_players=None):
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

    # Calculate picks until your next turn from the real snake-draft order.
    # In a standard snake draft, team at slot S (1-indexed) picks at position
    # S in odd (forward) rounds and position (N - S + 1) in even (reverse)
    # rounds. The gap to your next turn alternates between 2*(N-S)+1 and
    # 2*S-1 depending on whether you're currently in a forward or reverse
    # round — it is NOT a fixed constant regardless of slot (a prior version
    # of this code assumed 2*(num_teams-1) always, which both overstates and
    # understates the real gap depending on slot and round parity).
    picks_made_total = league_context.get("picks_made_total", 0)
    if DEV_MODE:
        print(f"  picks_made_total from context: {picks_made_total}")
    picks_made_total = picks_made_total or sum(picks_by_pos.values()) * num_teams
    current_pick_number = picks_made_total + 1

    def _pick_number_for_slot(round_num, slot, teams):
        if round_num % 2 == 1:  # forward round
            return (round_num - 1) * teams + slot
        return (round_num - 1) * teams + (teams - slot + 1)  # reverse round

    current_round_num = (current_pick_number - 1) // num_teams + 1
    next_pick_number = None
    for r in range(current_round_num, current_round_num + 3):
        candidate = _pick_number_for_slot(r, my_draft_slot, num_teams)
        if candidate > current_pick_number:
            next_pick_number = candidate
            break
    picks_until_next = (next_pick_number - current_pick_number) if next_pick_number else num_teams

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
        return None, {}, {}

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

    # Weekly-elimination leagues (see server.CHOPPED_LEAGUE_ID) can't afford
    # a starter who might not play — a player flagged with any current
    # injury status shouldn't be counted as "the best option right now"
    # for starter opportunity-cost purposes, even though he's still a
    # perfectly real backup-tier pick at his own value. This only affects
    # who best_now/best_after treat as the position's top option; it does
    # NOT remove anyone from the actual candidate pool used elsewhere (a
    # flagged player still shows up, still scores on his own real VORP).
    _strict_starter_health = league_context.get("strict_starter_health", False)
    healthy_active = [v for v in viable_active if not _excluded_from_best(v["player"], _strict_starter_health)]

    # Get best available ACTIVE player at each position right now
    best_now = {}
    for pos in needed_positions:
        best = next((v for v in healthy_active if v["position"] == pos), None)
        best_now[pos] = best["vorp"] if best else 0

    # Simulate N picks happening before your next turn. Three tiers, each
    # falling back to the next on missing data rather than ever blocking a
    # recommendation:
    #   1. Team-aware + probabilistic: walk the real draft order, ask what
    #      each specific upcoming team needs (a team sitting at 0 WR takes
    #      the best available WR regardless of generic ADP), and treat
    #      each player's ADP as a mean with real variance rather than a
    #      hard cutoff — a specific team reaching for a player outside
    #      generic expectation is a real, if usually small, possibility,
    #      not a zero. See _simulate_team_aware_best_after. Needs
    #      all_picks, slot_to_roster_id and all_players to identify teams
    #      and their current rosters.
    #   2. Real ADP order (deterministic top-N cutoff) — still assumes
    #      every pick is league-wide-generic, but at least reflects real
    #      human pacing (e.g. TE/backup QB wait well past raw VORP rank)
    #      instead of pure VORP order.
    #   3. Pure VORP order (deterministic top-N cutoff).
    slot_to_roster_id = league_context.get("slot_to_roster_id")
    all_picks = league_context.get("all_picks")
    best_after = None
    if adp_map and slot_to_roster_id and all_picks is not None and all_players:
        try:
            pick_sequence = _upcoming_pick_roster_ids(
                current_pick_number, picks_until_next, num_teams, slot_to_roster_id
            )
            best_after = _simulate_team_aware_best_after(
                viable_active, adp_map, pick_sequence, all_picks, all_players,
                dedicated, league_context.get("rosters_by_id"), needed_positions,
                seed=current_pick_number,
                strict_starter_health=_strict_starter_health
            )
        except Exception as e:
            if DEV_MODE:
                print(f"  team-aware probabilistic simulation failed, falling back to generic ADP order: {e}")
            best_after = None

    if best_after is None:
        if adp_map:
            def _adp_sort_key(v):
                entry = adp_map.get(v["player"].get("player_id"))
                return entry["adp"] if entry else 9999
            viable_by_adp = sorted(viable_active, key=_adp_sort_key)
            top_n_players = [v["player"].get("full_name") for v in viable_by_adp[:picks_until_next]]
        else:
            top_n_players = [v["player"].get("full_name") for v in viable_active[:picks_until_next]]
        viable_after = [v for v in viable_active if v["player"].get("full_name") not in top_n_players]
        viable_after = [v for v in viable_after if not _excluded_from_best(v["player"], _strict_starter_health)]

        # Get best available ACTIVE player at each position after N picks
        best_after = {}
        for pos in needed_positions:
            best = next((v for v in viable_after if v["position"] == pos), None)
            best_after[pos] = best["vorp"] if best else 0

    urgency_scores = {}
    need_scores = {}
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
            # No players left at this position at all. A large finite
            # sentinel rather than float('inf') — infinity survives Python
            # arithmetic fine but isn't valid JSON, and get_recommendation_raw
            # serializes these numbers directly to the frontend. This still
            # sorts as "maximally urgent" against any real scarcity ratio.
            scarcity_ratio = 1000.0
        elif positive_vorp_players == 0:
            # Only negative VORP players remain — use all players for scarcity
            scarcity_ratio = slots_needed / all_players_at_pos
        else:
            scarcity_ratio = slots_needed / positive_vorp_players

        # Reduce urgency for backup-only slots — any player fills them.
        # But headcount alone isn't enough: a "dedicated" slot occupied by a
        # below-replacement player isn't really filled in any way that should
        # lower urgency (e.g. a nominal RB2 who's actually below replacement
        # level is still a real need, not a backup slot). Check that the
        # actual players occupying those dedicated slots — the top N at this
        # position on the roster, N = dedicated slots — clear replacement
        # level. Falls back to pure headcount if roster/replacement data
        # isn't available.
        dedicated_slots_needed = dedicated.get(pos, 0)
        dedicated_filled = picks_by_pos.get(pos, 0) >= dedicated_slots_needed
        dedicated_quality_ok = True
        if dedicated_filled and sim_active and replacement and dedicated_slots_needed > 0:
            value_field = league_context.get("value_type", "dynasty_value")
            starters = sorted(
                (p for p in sim_active.values() if p.get("position") == pos),
                key=lambda p: p.get(value_field, 0) or 0,
                reverse=True
            )[:dedicated_slots_needed]
            dedicated_quality_ok = all(
                (s.get(value_field, 0) or 0) - replacement.get(pos, 0) > 0
                for s in starters
            )
            dedicated_filled = dedicated_filled and dedicated_quality_ok
        backup_multiplier = 0.3 if dedicated_filled else 1.0
        if DEV_MODE:
            print(f"  {pos}: dedicated_filled={dedicated_filled} (quality_ok={dedicated_quality_ok}), picks={picks_by_pos.get(pos,0)}, dedicated={dedicated_slots_needed}, backup_mult={backup_multiplier}")

        # Two independent signals, kept separate rather than blended into
        # one number: opportunity_cost is board-risk (will someone else
        # take the best option here before your next turn?), need_scores is
        # roster need (how badly is this position still unfilled?). Blending
        # them into one urgency figure meant the TE/backup-QB early-round
        # dampening (applied via urgency_modifiers, meant to correct for
        # over-trusting opportunity-cost math early) silently dampened real
        # roster need too, even in round 1 with a position still at zero —
        # need should never be dampened by round, only board-risk should.
        # See _calc_score for how these recombine.
        urgency_scores[pos] = opportunity_cost
        need_scores[pos] = scarcity_ratio * backup_multiplier

        if DEV_MODE:
            print(f"  {pos}: opp_cost={round(opportunity_cost)}, scarcity={round(scarcity_ratio,3)}, backup_mult={backup_multiplier}, need={round(need_scores[pos],3)}, slots_needed={round(slots_needed,2)}, flex_demand={round(flex_demand,2)}, pos_vorp_players={positive_vorp_players}")

    if not urgency_scores:
        return None, {}, {}

    most_urgent_pos = max(urgency_scores, key=lambda p: urgency_scores[p] * (1 + need_scores.get(p, 0)))

    if DEV_MODE:
        print(f"_calculate_urgency:")
        print(f"  my_draft_slot: {my_draft_slot}, num_teams: {num_teams}, picks_until_next: {picks_until_next}")
        print(f"  needed_positions: {needed_positions}")
        print(f"  picks_by_pos: {picks_by_pos}")
        print(f"  urgency_scores: {urgency_scores}")
        print(f"  need_scores: {need_scores}")
        print(f"  most_urgent_pos: {most_urgent_pos}")

    return most_urgent_pos, urgency_scores, need_scores

def _modifier_for(pos, urgency_modifiers):
    if urgency_modifiers and pos in urgency_modifiers:
        return urgency_modifiers[pos]
    return URGENCY_MODIFIER


def _calc_score(vorp, opportunity_cost, need, pos, urgency_modifiers):
    """
    score = vorp * (1 + opportunity_cost)^modifier * (1 + need)
    (or vorp / [...] when vorp is negative — see _bpa_decision_v2's
    docstring for the full rationale on the positive/negative split).
    Module-level so get_recommendation_raw can expose the exact same score
    the real decision is actually based on, not a re-derived approximation.

    opportunity_cost and need are two deliberately independent signals,
    not blended into one "urgency" figure:
      - opportunity_cost: board risk — will someone else take the best
        option here before your next turn?
      - need: roster need — how badly is this position still unfilled,
        scaled down for backup-only slots?
    The early-round dampening (`modifier`, e.g. TE/backup-QB ramping in
    from 0) applies ONLY to opportunity_cost, not to need. That dampening
    exists to correct for real drafters not trusting opportunity-cost math
    early (they don't reach for TE/QB in round 1 even when the math says
    to) — it was never meant to say "ignore how badly you need this
    position in round 1 too." Blending them into one number that gets
    dampened together silently suppressed real, current roster need
    whenever it happened to coincide with an early round — need should
    always count at full strength, regardless of round.

    Using (1 + x) rather than a bare x^modifier for both factors is
    deliberate: opportunity_cost legitimately hits exactly 0 whenever the
    simulation finds waiting costs nothing (routine with an accurate,
    often-small picks_until_next), and a bare x^modifier would multiply
    the score to a flat zero in that case regardless of VORP — verified
    live: a WR with VORP 128 scored exactly 0 this way. (1 + x) guarantees
    graceful fallback to plain VORP when a factor is 0 (matching how
    modifier=0 already falls back to plain VORP), while still scaling up
    correctly as either factor rises. This also removes the need for the
    old safe_urgency floor in the negative branch, since 1+x is always
    >= 1 and can't produce a division blowup.
    """
    modifier = _modifier_for(pos, urgency_modifiers)
    combined = ((1 + opportunity_cost) ** modifier) * (1 + need)
    if vorp >= 0:
        return vorp * combined
    else:
        return vorp / combined


def _eligible_for_override(pos, urgency_scores, starter_needed_positions, blocked_positions):
    """
    Only positions with a real computed urgency (i.e. still actually
    needed on this roster — present in urgency_scores) are eligible to
    win the MANDATORY override slot. A fully satisfied position (starter +
    backup already filled) isn't in urgency_scores at all and defaults to
    a placeholder urgency of 1 elsewhere — that placeholder is not a real
    need signal, so it must never be allowed to outscore a position that's
    genuinely still needed just because its league-wide VORP is high
    (that scenario is what the separate trade_bait signal is for).
    Module-level so get_recommendation_raw can expose exactly why a
    higher-scoring position didn't win, not a re-derived approximation.
    """
    if pos not in urgency_scores:
        return False
    if blocked_positions and pos in blocked_positions:
        # e.g. a backup QB in a non-Superflex league before the FLEX slot
        # is genuinely (not just numerically) filled — see calculate_bpa
        # for the exact condition.
        return False
    if starter_needed_positions and pos not in starter_needed_positions:
        # Backup-tier position (its own starter slot already filled) can't
        # hijack the override while another position still needs a
        # starter — bench depth is always lower priority than a starting
        # lineup slot.
        return False
    return True


def _bpa_decision_v2(best_overall, best_needed, urgency_scores, viable=None, starter_needed_positions=None, urgency_modifiers=None, blocked_positions=None, need_scores=None, strict_starter_health=False):
    """
    Shared BPA decision scoring for both dynasty and redraft leagues.

    Scores every position's best-VORP player via _calc_score: vorp times
    two independent factors — opportunity_cost (board risk, dampened by
    modifier in early rounds for TE/backup-QB) and need (roster need,
    never dampened by round). See _calc_score's docstring for the full
    rationale on why these are kept separate rather than blended, and for
    modifier's exact role.

    Positive-VORP players are scored directly. Negative-VORP players are
    scored by dividing instead of multiplying, so a stronger signal still
    makes a below-replacement pick relatively less bad (closer to zero)
    without ever letting it cross into positive territory and beat a real
    positive-VORP player — a negative score can never outscore a
    non-negative one.

    starter_needed_positions: set of positions whose dedicated starter
    slot(s) aren't filled yet. When non-empty, only those positions are
    eligible to win the override — a backup-tier pick (its own starter
    slot already filled) can't hijack the MANDATORY recommendation while
    some other position doesn't even have a starter yet, no matter its
    raw VORP. Once every position has its starter, this constraint lifts
    and backup-tier positions become eligible again.
    """
    if not best_overall:
        return None, None, 0
    if not best_needed:
        return None, best_overall["player"], 0

    need_scores = need_scores or {}

    # Prefer a healthy player to represent each position in the decision
    # below — an injured/backup-only player still shows up normally as
    # himself elsewhere (alternatives list, his own real score), but he
    # shouldn't be the one winning the actual recommendation on raw VORP
    # alone when a healthy option exists at the same position.
    position_best = {}
    position_best_any_health = {}
    if viable:
        for v in viable:
            pos = v["position"]
            if pos not in position_best_any_health or v["vorp"] > position_best_any_health[pos]["vorp"]:
                position_best_any_health[pos] = v
            if _excluded_from_best(v["player"], strict_starter_health):
                continue
            if pos not in position_best or v["vorp"] > position_best[pos]["vorp"]:
                position_best[pos] = v
    for pos, v in position_best_any_health.items():
        position_best.setdefault(pos, v)

    overall_pos = best_overall["position"]
    needed_pos = best_needed["position"]
    overall_urgency = urgency_scores.get(overall_pos, 0)
    needed_urgency = urgency_scores.get(needed_pos, 0)
    overall_need = need_scores.get(overall_pos, 0)
    needed_need = need_scores.get(needed_pos, 0)

    overall_score = _calc_score(best_overall["vorp"], overall_urgency, overall_need, overall_pos, urgency_modifiers)
    needed_score = _calc_score(best_needed["vorp"], needed_urgency, needed_need, needed_pos, urgency_modifiers)

    # best_needed is only a valid starting baseline if its own position is
    # itself eligible — otherwise it's exactly the case _eligible_for_override
    # exists to prevent (e.g. a backup-tier QB slot with no genuine starter
    # need left) winning by default just because it was never actually
    # checked against the gate the alternatives below are held to. Verified
    # live: Jaxson Dart (QB, both real QB starter slots already filled by
    # Maye/Purdy — only a backup slot remained) was winning outright this
    # way despite RB/WR/TE all still having a completely open starter slot.
    if _eligible_for_override(needed_pos, urgency_scores, starter_needed_positions, blocked_positions):
        best_pos = needed_pos
        best_score = needed_score
        best_v = best_needed
    else:
        best_pos = None
        best_score = float("-inf")
        best_v = None

    if _eligible_for_override(overall_pos, urgency_scores, starter_needed_positions, blocked_positions) and overall_score > best_score:
        best_pos = overall_pos
        best_score = overall_score
        best_v = best_overall
    for pos, v in position_best.items():
        if not _eligible_for_override(pos, urgency_scores, starter_needed_positions, blocked_positions):
            continue
        score = _calc_score(v["vorp"], urgency_scores[pos], need_scores.get(pos, 0), pos, urgency_modifiers)
        if score > best_score:
            best_score = score
            best_pos = pos
            best_v = v

    # Only reachable if literally nothing was eligible (e.g. every position
    # is backup-tier-only right now) — fall back to best_needed rather than
    # returning nothing, since some recommendation beats none.
    if best_v is None:
        best_pos = needed_pos
        best_score = needed_score
        best_v = best_needed

    if DEV_MODE:
        all_scores = sorted(
            [(position_best[pos]["player"].get("full_name"), pos,
              round(position_best[pos]["vorp"]),
              round(urgency_scores.get(pos, 0)),
              round(need_scores.get(pos, 0), 2),
              round(_calc_score(position_best[pos]["vorp"], urgency_scores.get(pos, 0), need_scores.get(pos, 0), pos, urgency_modifiers)),
              position_best[pos]["player"].get("fc_redraft_value", 0),
              position_best[pos].get("placement", "?"))
             for pos in position_best if pos in urgency_scores],
            key=lambda x: x[5], reverse=True
        )[:5]
        print(f"_bpa_decision_v2:")
        print(f"  urgency_modifiers: {urgency_modifiers}")
        print(f"  top scores (name, pos, vorp, opp_cost, need, score, value, placement): {all_scores}")
        print(f"  best_needed: {best_needed['player'].get('full_name')} ({needed_pos}), vorp={round(best_needed['vorp'])}, opp_cost={round(needed_urgency)}, need={round(needed_need,2)}, score={round(needed_score)}")
        print(f"  winner: {best_v['player'].get('full_name')} ({best_pos}), score={round(best_score)}")
        for pos, v in position_best.items():
            opp_cost = urgency_scores.get(pos, 0)
            need = need_scores.get(pos, 0)
            sc = _calc_score(v["vorp"], opp_cost, need, pos, urgency_modifiers)
            print(f"  position_best {pos}: {v['player'].get('full_name')}, vorp={round(v['vorp'])}, opp_cost={round(opp_cost)}, need={round(need,2)}, score={round(sc)}, modifier={round(_modifier_for(pos, urgency_modifiers),3)}, placement={v.get('placement')}")

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
    strict_starter_health = league_context.get("strict_starter_health", False)

    # Best player at any needed position by VORP
    best_needed_overall = _best_healthy_first(
        [v for v in viable_active if v["position"] in needed_positions],
        strict_starter_health
    )
    best_needed_scarce = _best_healthy_first(viable_active, strict_starter_health, most_needed_pos) if most_needed_pos else None

    # If no active-bound player at most needed pos, allow best dynasty-value
    # taxi player there — urgency overrides normal taxi routing for needed positions
    if not best_needed_scarce and most_needed_pos:
        best_needed_scarce = _best_healthy_first(viable, strict_starter_health, most_needed_pos) or best_needed_overall

    best_needed_scarce = best_needed_scarce or best_needed_overall

    # Use best_needed_overall for BPA threshold comparison,
    # best_needed_scarce as the actual recommendation when BPA doesn't fire
    # Best player overall by VORP — defined here so it's available for comparison below
    best_overall = _best_healthy_first(viable_active, strict_starter_health)

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

def _apply_salary_adjustment(vorp_players, league_context):
    """
    POC: adjusts VORP by value-per-dollar efficiency for a salary-cap
    league (league_context["salary_cap"] present — see server.py's
    DSFF_LEAGUE_ID). No-op for every other league.

    Two effects:
      1. Hard affordability gate — a player is dropped entirely if taking
         him would leave less than $1 for every other remaining roster
         slot, regardless of VORP. Not a preference, a real constraint.
      2. Continuous value-per-dollar blend — strength scales with how
         tight the remaining budget is (SALARY_COMFORTABLE_PER_SLOT and
         above: barely touches VORP; toward $0/slot: efficiency dominates).
         Players priced efficiently relative to the affordable pool get a
         boost, inefficient ones a penalty — proportional, not a cutoff.

    Original VORP is preserved as "raw_vorp"; "vorp" becomes the
    budget-adjusted figure the rest of the BPA pipeline reads.
    """
    salary_cap = league_context.get("salary_cap")
    if not salary_cap:
        return vorp_players

    salaries = salary_cap["salaries"]
    remaining_budget = salary_cap["remaining_budget"]
    remaining_slots = salary_cap["remaining_slots"]
    avg_per_slot = salary_cap["avg_per_slot"]

    min_bid = 1
    max_affordable = remaining_budget - (remaining_slots - 1) * min_bid

    priced = []
    for v in vorp_players:
        salary = salaries.get(v["player"].get("player_id"))
        if salary is not None and salary > max_affordable:
            continue
        v["raw_vorp"] = v["vorp"]
        v["salary"] = salary
        priced.append(v)

    ratios = [v["raw_vorp"] / v["salary"] for v in priced if v.get("salary") and v["raw_vorp"] > 0]
    if not ratios:
        return priced

    pool_avg_ratio = sum(ratios) / len(ratios)
    w = 1 - min(1, avg_per_slot / SALARY_COMFORTABLE_PER_SLOT)

    for v in priced:
        if not v.get("salary") or v["raw_vorp"] <= 0 or pool_avg_ratio <= 0:
            continue
        ratio = v["raw_vorp"] / v["salary"]
        v["vorp"] = v["raw_vorp"] * (ratio / pool_avg_ratio) ** w

    return priced


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
        return None, None, None, None, None, None, None, None, None, None

    vorp_players = _apply_salary_adjustment(vorp_players, league_context)
    if not vorp_players:
        return None, None, None, None, None, None, None, None, None, None

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
            return None, best["player"], 0, None, None, None, None, None, None, None
        return None, None, None, None, None, None, None, None, None, None

    # Rookie drafts are about accumulating the best assets, not filling
    # this instant's roster construction — positional need/capacity gating
    # doesn't apply. viable is already sorted by VORP descending, so the
    # top entry is simply the best player on the board. No positional
    # override is possible here (top player is always the pick), so no
    # trade bait either — there's nothing being passed over.
    if league_context.get("is_rookie_draft"):
        if DEV_MODE:
            print(f"  is_rookie_draft: pure BPA — {viable[0]['player'].get('full_name')}")
        return None, viable[0]["player"], 0, [], None, None, None, None, None, None

    # Step 4: Count meaningful players at each position
    picks_by_pos = _count_picks_by_pos(sim_active, league_context)

    # Step 5: Calculate positional urgency (opportunity cost × scarcity)
    if DEV_MODE:
        print(f"  drafted_count before urgency: {drafted_count}")
        print(f"  dedicated_cutoff before urgency: {dedicated_cutoff}")

    # Real ADP for the "who gets drafted next" simulation — falls back to
    # None (VORP-order simulation) on any fetch/match failure rather than
    # ever blocking a recommendation on an external API call succeeding.
    adp_map = None
    if all_players:
        try:
            adp_map = adp_client.build_adp_map(league_context, all_players, date.today().year)
        except Exception as e:
            if DEV_MODE:
                print(f"  adp_client.build_adp_map failed, falling back to VORP-order simulation: {e}")

    most_needed_pos, urgency_scores, need_scores = _calculate_urgency(
        viable, picks_by_pos, league_context, drafted_count, dedicated_cutoff, sim_taxi, sim_active, replacement, adp_map, all_players
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
        return None, best_fallback["player"] if best_fallback else None, 0, trade_bait_players, urgency_scores, None, None, None, need_scores, adp_map

    # Step 7: BPA decision (identical scoring for dynasty and redraft)
    # A genuinely open FLEX/SUPER_FLEX slot is a real starter opportunity,
    # not backup depth — a position whose dedicated slots are full but that
    # could still fill an open flex slot must stay eligible for the
    # override, or a strong pick at that position gets wrongly blocked as
    # "starter slot already filled" (verified live: QB1 filled by Maye
    # incorrectly excluded QB entirely, even with an open SUPER_FLEX slot
    # a strong 2nd QB should be able to compete for). backup_needs is
    # deliberately excluded here — that's bench depth, not a starter slot.
    starter_needed_positions = {
        pos for pos in ["QB", "RB", "WR", "TE"]
        if picks_by_pos.get(pos, 0) < (
            dedicated.get(pos, 0) +
            (drafted_count.get(pos, 0) - dedicated_cutoff.get(pos, 0)) / league_context.get("num_teams", 12)
        )
    }

    # Ramp TE's (and, in single-QB leagues, QB's) urgency influence in from
    # 0 up to full strength as the starting lineup actually fills up — see
    # _bpa_decision_v2's docstring for why these two specifically. Tied to
    # real roster progress (QB/RB/WR/TE dedicated slots only, never K/DEF)
    # rather than a fixed round number, so it adapts to how this specific
    # draft is actually going instead of an arbitrary universal cutoff.
    total_starters = sum(dedicated.values())
    filled_starters = sum(
        min(picks_by_pos.get(pos, 0), dedicated.get(pos, 0)) for pos in dedicated
    )
    starter_fill_ramp = (filled_starters / total_starters) if total_starters else 1.0

    has_superflex = league_context.get("has_superflex", False)
    ramped_modifier = URGENCY_MODIFIER * starter_fill_ramp

    # Relative starter strength: a backup-tier position (starter slot(s)
    # already filled) shouldn't get full urgency to stack a 2nd good player
    # just because one exists — not when some OTHER position's actual
    # starter is much weaker. Hedging the weak spot is worth more than
    # doubling up on a position that's already strong. Compare each filled
    # position's weakest starter against the single weakest starter on the
    # whole roster; a position already far ahead of that gets its backup
    # urgency dampened toward 0, proportional to the gap. A position whose
    # own starter already below replacement (or IS the weakest link) keeps
    # full weight — hedging there is exactly right. This doesn't hard-block
    # anything: a truly exceptional backup-tier player's raw VORP still
    # comes through in the score even at modifier=0, so a value gap large
    # enough to matter still wins.
    # QB is excluded from this comparison entirely in non-Superflex leagues:
    # a backup QB can't hedge a weak starting QB the way a bench RB/WR/TE
    # can hedge a weak starter (you can't bench your only startable QB for
    # a 2nd one). Its own eligibility stays governed by the flex-quality
    # block below instead — never by "my QB is technically my weakest
    # starter, so go get a 2nd one."
    value_field = league_context.get("value_type", "dynasty_value")
    positions_for_comparison = ["QB", "RB", "WR", "TE"] if has_superflex else ["RB", "WR", "TE"]
    starter_vorp_by_position = {}
    for pos in positions_for_comparison:
        if picks_by_pos.get(pos, 0) < dedicated.get(pos, 0):
            continue  # doesn't have all its starters yet, not part of this comparison
        pos_players = sorted(
            (p for p in sim_active.values() if p.get("position") == pos),
            key=lambda p: p.get(value_field, 0) or 0,
            reverse=True
        )
        starters = pos_players[:dedicated.get(pos, 0)]
        if starters:
            starter_vorp_by_position[pos] = min(
                (s.get(value_field, 0) or 0) - replacement.get(pos, 0) for s in starters
            )

    weakest_overall_starter_vorp = min(starter_vorp_by_position.values()) if starter_vorp_by_position else 0

    relative_strength_modifiers = {}
    for pos, own_strength in starter_vorp_by_position.items():
        if own_strength <= 0:
            relative_strength_modifiers[pos] = URGENCY_MODIFIER
        else:
            ratio = max(0.0, weakest_overall_starter_vorp) / own_strength
            relative_strength_modifiers[pos] = URGENCY_MODIFIER * min(1.0, ratio)

    urgency_modifiers = dict(relative_strength_modifiers)
    # This dampener exists to correct one specific false assumption: the
    # opportunity-cost simulation, if it has no real ADP data, assumes
    # every team drafts in pure VORP order — a false premise that made TE
    # look far more urgent early than real drafts ever bear out (verified
    # against ~7,000 real 2QB drafts: zero TEs in the first 28 picks).
    # Once adp_map is present, that false premise is gone — opportunity
    # cost is already computed from real draft behavior (team-aware or
    # generic ADP order, see _calculate_urgency), so a genuinely early
    # opportunity cost for TE (e.g. a clear TE1 with real snipe risk) is a
    # real signal, not the artifact this modifier was built to suppress.
    # Applying it on top of already-real data would double-correct.
    if not adp_map:
        urgency_modifiers["TE"] = min(urgency_modifiers.get("TE", URGENCY_MODIFIER), ramped_modifier)
    if not has_superflex:
        # A backup QB's "urgency" isn't a real signal at all here — there's
        # no genuine race for a 2nd unusable body the way there is for a
        # FLEX-competitive bench spot. Its eligibility is governed entirely
        # by the flex-quality block below; once eligible, it should win
        # only on raw value, never get an urgency-driven boost on top.
        urgency_modifiers["QB"] = 0
    if DEV_MODE:
        print(f"  starter lineup filled: {filled_starters}/{total_starters} ({round(starter_fill_ramp,3)})")
        print(f"  starter_vorp_by_position: {({k: round(v) for k,v in starter_vorp_by_position.items()})}, weakest={round(weakest_overall_starter_vorp)}")
        print(f"  urgency_modifiers={({k: round(v,3) for k,v in urgency_modifiers.items()})} (full weight={URGENCY_MODIFIER})")

    # A backup QB in a non-Superflex league has almost no realistic path to
    # ever being started — unlike a bench RB/WR/TE, which still has FLEX
    # eligibility, bye-week, and injury-replacement value. Its VORP is real
    # on paper but essentially never gets used. Block it from the override
    # entirely until the FLEX slot is genuinely, not just numerically,
    # filled — a surplus RB/WR/TE (beyond that position's own dedicated
    # count) actually clearing replacement level, not just a warm body.
    # Once that's true, a backup QB competes normally on value like anyone
    # else (e.g. a real riser who keeps sliding can still win it).
    blocked_positions = set()
    has_flex_slot = sum(league_context.get("flex_slot_counts", {}).values()) > 0
    if not has_superflex and has_flex_slot:
        value_field = league_context.get("value_type", "dynasty_value")
        flex_quality_filled = False
        for pos in ["RB", "WR", "TE"]:
            pos_players = sorted(
                (p for p in sim_active.values() if p.get("position") == pos),
                key=lambda p: p.get(value_field, 0) or 0,
                reverse=True
            )
            surplus = pos_players[dedicated.get(pos, 0):]
            if any((p.get(value_field, 0) or 0) - replacement.get(pos, 0) > 0 for p in surplus):
                flex_quality_filled = True
                break
        if not flex_quality_filled:
            blocked_positions.add("QB")
        if DEV_MODE:
            print(f"  flex_quality_filled={flex_quality_filled}, blocked_positions={blocked_positions}")

    bpa_player, suggested_pick, gap = _bpa_decision_v2(
        best_overall, best_needed, urgency_scores, viable, starter_needed_positions, urgency_modifiers, blocked_positions, need_scores,
        league_context.get("strict_starter_health", False)
    )

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


    return bpa_player, suggested_pick, gap, trade_bait_players, urgency_scores, urgency_modifiers, starter_needed_positions, blocked_positions, need_scores, adp_map


def get_recommendation_raw(available, league_context, all_players=None):
    """
    The same underlying pick as get_recommendation(), but with no Claude
    call at all — no narrative reasoning/positional_note/upside, just the
    deterministic numbers behind the decision: VORP, current value,
    positional rank, replacement level, urgency, and the final score
    (vorp * urgency^modifier) for the recommended player and the top
    alternatives at every position. For a "Claude Advice" toggle aimed at
    players who don't want to pay for flavor text and just want the raw
    math — including *why* the pick isn't simply whoever has the highest
    raw VORP (urgency/positional need routinely overrides that).

    Calls calculate_vorp() a second time rather than reusing calculate_bpa's
    internal vorp_players list — a little redundant computation, zero risk
    of changing what calculate_bpa actually decides. urgency_scores and
    urgency_modifiers ARE taken directly from calculate_bpa's return
    (rather than recomputed) so the exposed score is exactly what the real
    decision used, not a re-derived approximation of it.
    """
    bpa_player, suggested_pick, gap, trade_bait_players, urgency_scores, urgency_modifiers, starter_needed_positions, blocked_positions, need_scores, adp_map = calculate_bpa(
        available, league_context, all_players
    )
    final_pick = bpa_player or suggested_pick
    urgency_scores = urgency_scores or {}
    starter_needed_positions = starter_needed_positions or set()
    blocked_positions = blocked_positions or set()
    need_scores = need_scores or {}
    adp_map = adp_map or {}

    vorp_players, replacement, _, _ = calculate_vorp(available, league_context, all_players)

    def _override_blocked_reason(pos):
        if pos not in urgency_scores:
            return "not actively needed (starter + backup already filled)"
        if pos in blocked_positions:
            return "blocked (e.g. backup QB before FLEX is genuinely filled)"
        if starter_needed_positions and pos not in starter_needed_positions:
            return "starter slot already filled — can't outrank another position's open starter need"
        return None

    def _entry(v):
        pos = v["position"]
        opportunity_cost = urgency_scores.get(pos, 0)
        need = need_scores.get(pos, 0)
        blocked_reason = _override_blocked_reason(pos)
        adp_entry = adp_map.get(v["player"].get("player_id"))
        return {
            "name": v["player"].get("full_name"),
            "position": pos,
            "team": v["player"].get("team"),
            "value": round(v["value"], 2),
            "vorp": round(v["vorp"], 2),
            "replacement_level": round(replacement.get(pos, 0), 2),
            "opportunity_cost": round(opportunity_cost, 2),
            "roster_need": round(need, 2),
            "score": round(_calc_score(v["vorp"], opportunity_cost, need, pos, urgency_modifiers), 2),
            "eligible_for_pick": blocked_reason is None,
            "not_eligible_reason": blocked_reason,
            "adp": adp_entry["adp"] if adp_entry else None,
            "adp_formatted": adp_entry["adp_formatted"] if adp_entry else None,
        }

    by_position = {"QB": [], "RB": [], "WR": [], "TE": []}
    for v in vorp_players:
        if v["position"] in by_position:
            by_position[v["position"]].append(_entry(v))
    # vorp_players is already sorted by VORP desc, so each position's list
    # is too — positional rank is just its 1-indexed position in that list.
    for entries in by_position.values():
        for i, e in enumerate(entries, start=1):
            e["positional_rank"] = i

    recommended = None
    if final_pick:
        pos = final_pick.get("position")
        recommended = next(
            (e for e in by_position.get(pos, []) if e["name"] == final_pick.get("full_name")),
            None
        )

    return {
        "recommendation": final_pick.get("full_name") if final_pick else None,
        "position": final_pick.get("position") if final_pick else None,
        "recommended_details": recommended,
        "gap": round(gap, 2) if gap is not None else None,
        "alternatives_by_position": {pos: entries[:5] for pos, entries in by_position.items()},
        "trade_bait": [
            {"name": tb["name"], "position": tb["position"]} for tb in (trade_bait_players or [])
        ],
    }


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