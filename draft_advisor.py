import json
from llm_client import get_completion

SYSTEM_PROMPT = """You are an expert dynasty fantasy football draft assistant. 
You specialize in dynasty leagues with superflex/2QB formats.
You reason about long-term player value, age curves, and positional scarcity.
You give concise, confident recommendations with clear reasoning.
Always respond in valid JSON only. No preamble, no markdown, no explanation outside the JSON."""

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

    my_players = []
    for pid in my_roster.get("players") or []:
        player = available.get(pid) or {}
        if player:
            my_players.append({
                "name": player.get("full_name"),
                "position": player.get("position"),
                "age": player.get("fc_age")
            })

    roster_positions = league_context.get("roster_positions", [])
    te_slots = sum(1 for p in roster_positions if p == "TE")
    qb_slots = sum(1 for p in roster_positions if p in ["QB", "SUPER_FLEX"])
    taxi_open = league_context.get("taxi_slots_total", 0) - league_context.get("taxi_slots_used", 0)
    picks_remaining = league_context.get("picks_remaining_for_me", 0)

    prompt = f"""You are advising on pick {pick_number} in a dynasty rookie draft.

LEAGUE CONTEXT:
{json.dumps(league_context, indent=2)}

MY CURRENT ROSTER PLAYERS IN THIS DRAFT:
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
recommend who I should draft with this pick.

Respond with this exact JSON structure:
{{
    "recommendation": "Player Name",
    "position": "POS",
    "reasoning": "2-3 sentence explanation of why this player at this pick",
    "positional_note": "Brief note on positional scarcity or roster fit",
    "upside": "Brief note on dynasty ceiling",
    "alternatives": [
        {{"name": "Player Name", "reason": "One sentence why they are the alternative"}},
        {{"name": "Player Name", "reason": "One sentence why they are the alternative"}}
    ]
}}"""

    return prompt

def get_recommendation(picks, available, my_roster, league_context, pick_number):
    prompt = build_prompt(picks, available, my_roster, league_context, pick_number)
    response = get_completion(prompt, system=SYSTEM_PROMPT)
    try:
        return json.loads(response)
    except json.JSONDecodeError:
        clean = response.strip().removeprefix("```json").removesuffix("```").strip()
        return json.loads(clean)

if __name__ == "__main__":
    from session import start_session
    from sleeper_draft import get_picks, get_available_rookies, get_available_players
    from sleeper_league import get_rosters, get_league

    session = start_session()
    draft_id = session["draft_id"]
    players = session["players"]
    league_id = session["league_id"]
    my_roster_id = session["my_roster_id"]

    league_detail = get_league(league_id)

    my_existing_players = []
    rosters = get_rosters(league_id)
    my_roster = next(r for r in rosters if r["roster_id"] == my_roster_id)
    for pid in my_roster.get("players") or []:
        player = players.get(pid, {})
        if player:
            my_existing_players.append({
                "name": player.get("full_name"),
                "position": player.get("position"),
                "age": player.get("fc_age") or player.get("age"),
                "dynasty_value": player.get("fc_value", "unranked")
            })

    league_context = {
        "num_teams": league_detail["settings"].get("num_teams"),
        "roster_positions": league_detail.get("roster_positions"),
        "scoring_settings": league_detail.get("scoring_settings"),
        "draft_type": session["draft_detail"].get("type"),
        "rounds": session["draft_detail"]["settings"].get("rounds"),
        "taxi_slots_total": league_detail["settings"].get("taxi_slots"),
        "taxi_slots_used": 1,
        "taxi_years": league_detail["settings"].get("taxi_years"),
        "picks_made_by_me": 4,
        "picks_remaining_for_me": 2,
        "my_existing_roster": my_existing_players
    }

    picks = get_picks(draft_id)
    available = get_available_rookies(players, picks)
    pick_number = len(picks) + 1

    print(f"Getting recommendation for pick {pick_number}...")
    rec = get_recommendation(picks, available, my_roster, league_context, pick_number)

    print(f"\nRECOMMENDATION: {rec['recommendation']} ({rec['position']})")
    print(f"\nReasoning: {rec['reasoning']}")
    print(f"Positional note: {rec['positional_note']}")
    print(f"Upside: {rec['upside']}")
    print(f"\nAlternatives:")
    for alt in rec.get("alternatives", []):
        print(f"  - {alt['name']}: {alt['reason']}")