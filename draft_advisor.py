import json
from llm_client import get_completion

SYSTEM_PROMPT = """You are an expert dynasty fantasy football draft assistant. 
You specialize in dynasty leagues with superflex/2QB formats.
You reason about long-term player value, age curves, and positional scarcity.
You give concise, confident recommendations with clear reasoning.
Always respond in valid JSON only. No preamble, no markdown, no explanation outside the JSON."""

def calculate_starter_ids(active_ids, players, league_detail):
    roster_positions = league_detail.get("roster_positions", [])
    required = {"QB": 0, "RB": 0, "WR": 0, "TE": 0, "FLEX": 0, "SUPER_FLEX": 0}
    for pos in roster_positions:
        if pos in required:
            required[pos] += 1

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

    slots_filled = {"QB": 0, "RB": 0, "WR": 0, "TE": 0, "FLEX": 0, "SUPER_FLEX": 0}
    starter_ids = set()

    for player in enriched:
        pos = player["position"]
        if pos in slots_filled and slots_filled[pos] < required[pos]:
            slots_filled[pos] += 1
            starter_ids.add(player["id"])

    flex_eligible = {"RB", "WR", "TE"}
    for player in enriched:
        if player["id"] not in starter_ids and slots_filled["FLEX"] < required["FLEX"] and player["position"] in flex_eligible:
            slots_filled["FLEX"] += 1
            starter_ids.add(player["id"])

    for player in enriched:
        if player["id"] not in starter_ids and slots_filled["SUPER_FLEX"] < required["SUPER_FLEX"]:
            slots_filled["SUPER_FLEX"] += 1
            starter_ids.add(player["id"])

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

def get_roster_recommendations(my_roster, players, league_detail, my_draft_picks, starter_ids):
    from sleeper_league import get_taxi_players

    taxi_ids = set(get_taxi_players(my_roster))
    reserve_ids = set(my_roster.get("reserve") or [])
    active_ids = set(my_roster.get("players") or [])
    if my_draft_picks:
        active_ids.update(my_draft_picks)
    active_ids = active_ids - taxi_ids - reserve_ids

    taxi_slots_total = league_detail["settings"].get("taxi_slots", 0)
    taxi_years = league_detail["settings"].get("taxi_years", 3)
    taxi_allow_vets = league_detail["settings"].get("taxi_allow_vets", 0)
    roster_positions = league_detail.get("roster_positions", [])
    roster_max = len(roster_positions)

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
            "years_exp": p.get("years_exp", 99)
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
        open_taxi = taxi_slots_total - len(sim_taxi)
        roster_count = len(sim_active)
        roster_over = max(0, roster_count - roster_max)

        bench_players = sorted(
            [p for p in sim_active.values() if p["id"] not in starter_ids and p["id"] != rookie["id"]],
            key=lambda x: x["value"]
        )
        taxi_players = sorted(sim_taxi.values(), key=lambda x: x["value"])
        worst_bench = bench_players[0] if bench_players else None
        worst_taxi = taxi_players[0] if taxi_players else None
        is_starter = rookie["id"] in starter_ids
        taxi_eligible = rookie["years_exp"] == 0 and taxi_allow_vets == 0

        if is_starter:
            recommendations.append({
                "player": rookie["name"],
                "position": rookie["position"],
                "action": "ACTIVE STARTER",
                "note": f"High value ({rookie['value']}) earns a starting spot.",
                "severity": "success"
            })
            continue

        if roster_over <= 0:
            if taxi_eligible and open_taxi > 0:
                recommendations.append({
                    "player": rookie["name"],
                    "position": rookie["position"],
                    "action": "TAXI",
                    "note": f"Developmental stash. Move to taxi ({open_taxi} slot(s) available).",
                    "severity": "info"
                })
                sim_taxi[rookie["id"]] = rookie
                sim_active.pop(rookie["id"], None)
            else:
                recommendations.append({
                    "player": rookie["name"],
                    "position": rookie["position"],
                    "action": "ACTIVE BENCH",
                    "note": f"Keep on active roster as a bench contributor (Val {rookie['value']}).",
                    "severity": "info"
                })
            continue

        # Check taxi first even if over roster limit
        if taxi_eligible and open_taxi > 0:
            recommendations.append({
                "player": rookie["name"],
                "position": rookie["position"],
                "action": "TAXI",
                "note": f"Developmental stash. Move to taxi ({open_taxi} slot(s) available). Reduces roster by 1.",
                "severity": "info"
            })
            sim_taxi[rookie["id"]] = rookie
            sim_active.pop(rookie["id"], None)
            continue

        if taxi_eligible:
            if worst_taxi and rookie["value"] > worst_taxi["value"]:
                if worst_taxi["value"] > (worst_bench["value"] if worst_bench else 0):
                    recommendations.append({
                        "player": rookie["name"],
                        "position": rookie["position"],
                        "action": "TAXI",
                        "note": f"Move to taxi. {worst_taxi['name']} (Val {worst_taxi['value']}) moves to bench.",
                        "severity": "info"
                    })
                    recommendations.append({
                        "player": worst_taxi["name"],
                        "position": worst_taxi["position"],
                        "action": "PROMOTE TO BENCH",
                        "note": f"Move from taxi to active bench. Worth more than worst bench player {worst_bench['name'] if worst_bench else 'none'} (Val {worst_bench['value'] if worst_bench else 0}).",
                        "severity": "warning"
                    })
                    if worst_bench:
                        recommendations.append({
                            "player": worst_bench["name"],
                            "position": worst_bench["position"],
                            "action": "CUT",
                            "note": f"Lowest value bench player (Val {worst_bench['value']}). Cut to make room.",
                            "severity": "error"
                        })
                        sim_active.pop(worst_bench["id"], None)
                    sim_taxi.pop(worst_taxi["id"], None)
                    sim_active[worst_taxi["id"]] = worst_taxi
                    sim_taxi[rookie["id"]] = rookie
                    sim_active.pop(rookie["id"], None)
                else:
                    recommendations.append({
                        "player": rookie["name"],
                        "position": rookie["position"],
                        "action": "TAXI",
                        "note": f"Move to taxi. Cut {worst_taxi['name']} (Val {worst_taxi['value']}), lowest value taxi player.",
                        "severity": "info"
                    })
                    recommendations.append({
                        "player": worst_taxi["name"],
                        "position": worst_taxi["position"],
                        "action": "CUT",
                        "note": f"Lowest value taxi player (Val {worst_taxi['value']}). Cut to make room for {rookie['name']}.",
                        "severity": "error"
                    })
                    sim_taxi.pop(worst_taxi["id"], None)
                    sim_taxi[rookie["id"]] = rookie
                    sim_active.pop(rookie["id"], None)
            else:
                if worst_bench and rookie["value"] > worst_bench["value"]:
                    recommendations.append({
                        "player": rookie["name"],
                        "position": rookie["position"],
                        "action": "ACTIVE BENCH",
                        "note": f"Not worth a taxi spot over current taxi players. Keep on bench (Val {rookie['value']}).",
                        "severity": "info"
                    })
                    recommendations.append({
                        "player": worst_bench["name"],
                        "position": worst_bench["position"],
                        "action": "CUT",
                        "note": f"Lowest value bench player (Val {worst_bench['value']}). Cut to make room.",
                        "severity": "error"
                    })
                    sim_active.pop(worst_bench["id"], None)
                else:
                    recommendations.append({
                        "player": rookie["name"],
                        "position": rookie["position"],
                        "action": "CUT CANDIDATE",
                        "note": f"Worth less than worst bench (Val {worst_bench['value'] if worst_bench else 0}) and worst taxi (Val {worst_taxi['value'] if worst_taxi else 0}). Consider cutting.",
                        "severity": "error"
                    })
                    sim_active.pop(rookie["id"], None)
        else:
            if worst_bench and rookie["value"] > worst_bench["value"]:
                recommendations.append({
                    "player": rookie["name"],
                    "position": rookie["position"],
                    "action": "ACTIVE BENCH",
                    "note": f"Not taxi eligible. Keep on bench (Val {rookie['value']}).",
                    "severity": "info"
                })
                recommendations.append({
                    "player": worst_bench["name"],
                    "position": worst_bench["position"],
                    "action": "CUT",
                    "note": f"Lowest value bench player (Val {worst_bench['value']}). Cut to make room.",
                    "severity": "error"
                })
                sim_active.pop(worst_bench["id"], None)
            else:
                recommendations.append({
                    "player": rookie["name"],
                    "position": rookie["position"],
                    "action": "CUT CANDIDATE",
                    "note": f"Not taxi eligible and worth less than worst bench player (Val {worst_bench['value'] if worst_bench else 0}). Consider cutting.",
                    "severity": "error"
                })
                sim_active.pop(rookie["id"], None)

    for p in sorted(sim_taxi.values(), key=lambda x: x["value"]):
        if p["years_exp"] > taxi_years:
            recommendations.append({
                "player": p["name"],
                "position": p["position"],
                "action": "TAXI EXPIRED",
                "note": f"Exceeded taxi eligibility ({p['years_exp']} years exp, max {taxi_years}). Must be promoted or cut.",
                "severity": "error"
            })
        elif p["value"] > 3000:
            recommendations.append({
                "player": p["name"],
                "position": p["position"],
                "action": "CONSIDER PROMOTING",
                "note": f"Dynasty value ({p['value']}) suggests this player is ready to contribute. Consider promoting to active roster.",
                "severity": "warning"
            })

    return recommendations, sim_active, sim_taxi