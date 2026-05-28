import json
import os
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder="static", template_folder="templates")
CORS(app)

# Load players once at startup
with open("fantasy_players.json") as f:
    PLAYERS = {str(k): v for k, v in json.load(f).items()}

from config import (
    SLEEPER_USERNAME, BPA_THRESHOLD_DYNASTY, BPA_THRESHOLD_REDRAFT,
    TAXI_THRESHOLD_QB, TAXI_THRESHOLD_RB, TAXI_THRESHOLD_WR, TAXI_THRESHOLD_TE
)
from sleeper_league import get_user, get_leagues, get_rosters, get_taxi_players
from sleeper_draft import (
    get_drafts, get_draft_detail, get_picks,
    get_available_rookies, get_available_players, count_my_picks
)
from draft_advisor import (
    get_recommendation, calculate_starter_ids,
    calculate_roster_needs, get_roster_recommendations
)


def get_taxi_count(roster):
    taxi = get_taxi_players(roster)
    return len(taxi) if taxi else 0


def build_league_context(league_detail, draft_detail, my_roster, picks,
                          my_roster_id, players, my_draft_picks=None,
                          is_dynasty=True, starter_ids=None):
    my_picks_count = count_my_picks(picks, my_roster_id)
    total_rounds = draft_detail["settings"].get("rounds", 4)

    my_existing_players = []
    for pid in my_roster.get("players") or []:
        player = players.get(pid, {})
        if player:
            my_existing_players.append({
                "id": pid,
                "name": player.get("full_name"),
                "position": player.get("position"),
                "age": player.get("fc_age") or player.get("age"),
                "dynasty_value": player.get("fc_value", 0),
                "redraft_value": player.get("fc_redraft_value") or 0,
                "redraft_proxy": max(0, (1000 - (player.get("search_rank") or 1000)) * 10),
                "years_exp": player.get("years_exp", 99),
                "overall_rank": player.get("fc_overall_rank", 999),
                "search_rank": player.get("search_rank", 999),
            })

    _, backup_counts, _ = calculate_roster_needs(league_detail)

    return {
        "num_teams": league_detail["settings"].get("num_teams"),
        "roster_positions": league_detail.get("roster_positions"),
        "scoring_settings": league_detail.get("scoring_settings"),
        "draft_type": draft_detail.get("type"),
        "rounds": total_rounds,
        "taxi_slots_total": league_detail["settings"].get("taxi_slots"),
        "taxi_slots_used": get_taxi_count(my_roster),
        "taxi_years": league_detail["settings"].get("taxi_years"),
        "taxi_allow_vets": league_detail["settings"].get("taxi_allow_vets", 0),
        "picks_made_by_me": my_picks_count,
        "picks_made_total": len(picks),
        "picks_remaining_for_me": total_rounds - my_picks_count,
        "my_existing_roster": my_existing_players,
        "my_picks_this_draft": [
            {
                "id": pid,
                "name": players.get(pid, {}).get("full_name", "Unknown"),
                "position": players.get(pid, {}).get("position", "?"),
                "dynasty_value": players.get(pid, {}).get("fc_value", 0),
                "redraft_value": players.get(pid, {}).get("fc_redraft_value") or 0,
                "redraft_proxy": max(0, (1000 - (players.get(pid, {}).get("search_rank") or 1000)) * 10),
                "years_exp": players.get(pid, {}).get("years_exp", 99),
                "overall_rank": players.get(pid, {}).get("fc_overall_rank", 999),
                "search_rank": players.get(pid, {}).get("search_rank", 999),
            }
            for pid in (my_draft_picks or [])
        ],
        "my_starters": [
            {
                "name": players.get(pid, {}).get("full_name"),
                "position": players.get(pid, {}).get("position")
            }
            for pid in (starter_ids or [])
            if players.get(pid)
        ],
        "my_taxi_players": [
            players.get(pid, {}).get("full_name")
            for pid in (my_roster.get("taxi") or [])
            if players.get(pid)
        ],
        "backup_needs": {pos: backups for pos, backups in backup_counts.items()},
        "roster_construction_detail": {
            "QB": {
                "dedicated_slots": sum(1 for s in league_detail.get("roster_positions", []) if s == "QB"),
                "flex_eligible": sum(1 for s in league_detail.get("roster_positions", []) if s == "SUPER_FLEX")
            },
            "RB": {
                "dedicated_slots": sum(1 for s in league_detail.get("roster_positions", []) if s == "RB"),
                "flex_eligible": sum(1 for s in league_detail.get("roster_positions", []) if s in ["FLEX", "WRRB_FLEX"])
            },
            "WR": {
                "dedicated_slots": sum(1 for s in league_detail.get("roster_positions", []) if s == "WR"),
                "flex_eligible": sum(1 for s in league_detail.get("roster_positions", []) if s in ["FLEX", "REC_FLEX", "WRRB_FLEX"])
            },
            "TE": {
                "dedicated_slots": sum(1 for s in league_detail.get("roster_positions", []) if s == "TE"),
                "flex_eligible": sum(1 for s in league_detail.get("roster_positions", []) if s in ["FLEX", "REC_FLEX"])
            }
        },
        "is_dynasty": is_dynasty,
        "my_draft_slot": my_roster_id,  # placeholder, set properly below
        "bpa_threshold": BPA_THRESHOLD_DYNASTY if is_dynasty else BPA_THRESHOLD_REDRAFT,
        "value_type": "dynasty_value" if is_dynasty else "redraft_value",
        "value_key": "fc_value" if is_dynasty else "fc_redraft_value",

        # Canonical roster structure — read these in draft_advisor, never recompute
        "dedicated_slots": {
            "QB": sum(1 for s in league_detail.get("roster_positions", []) if s == "QB"),
            "RB": sum(1 for s in league_detail.get("roster_positions", []) if s == "RB"),
            "WR": sum(1 for s in league_detail.get("roster_positions", []) if s == "WR"),
            "TE": sum(1 for s in league_detail.get("roster_positions", []) if s == "TE"),
        },
        "flex_slot_counts": {
            slot: sum(1 for s in league_detail.get("roster_positions", []) if s == slot)
            for slot in ["FLEX", "SUPER_FLEX", "WRRB_FLEX", "REC_FLEX"]
            if any(s == slot for s in league_detail.get("roster_positions", []))
        },
        "has_superflex": any(s == "SUPER_FLEX" for s in league_detail.get("roster_positions", [])),
    }


# ── Shared draft state builder ─────────────────────────────────────────────────

def _build_draft_state(draft_id, league_id, user_id):
    """
    Fetch all Sleeper data and build the full draft state needed by both
    /api/draft and /api/recommend. Returns a dict with all context needed
    to render the UI and generate a recommendation.

    Raises exceptions on Sleeper API failures or missing roster.
    """
    import sleeper_league as sl

    draft_detail = get_draft_detail(draft_id)
    league_detail = sl.get_league(league_id)
    rosters = get_rosters(league_id)

    my_roster = next(
        (r for r in rosters if r.get("owner_id") == user_id), None
    )
    if not my_roster:
        raise ValueError("Roster not found for this user in this league.")

    my_roster_id = my_roster["roster_id"]
    picks = get_picks(draft_id)

    is_dynasty = league_detail.get("settings", {}).get("type") == 2
    rookie_draft = draft_detail.get("type") in ["rookie", "auction"]

    available = (
        get_available_rookies(PLAYERS, picks)
        if rookie_draft
        else get_available_players(PLAYERS, picks)
    )

    my_draft_picks = [
        p["player_id"]
        for p in picks
        if p.get("roster_id") == my_roster_id and p.get("player_id")
    ]

    active_ids = set(my_roster.get("players") or [])
    taxi_ids = set(get_taxi_players(my_roster))
    active_ids -= taxi_ids
    starter_ids = calculate_starter_ids(list(active_ids), PLAYERS, league_detail)

    my_draft_slot = draft_detail.get("slot_to_roster_id", {})
    # Find which slot this user's roster occupies
    my_slot = next(
        (int(slot) for slot, rid in my_draft_slot.items() if rid == my_roster_id),
        None
    )

    league_context = build_league_context(
        league_detail, draft_detail, my_roster, picks,
        my_roster_id, PLAYERS, my_draft_picks, is_dynasty, starter_ids
    )
    league_context["my_draft_slot"] = my_slot

    # Identify taxi players using sim state so taxi badges show during draft
    if is_dynasty:
        from draft_advisor import _build_sim_state
        all_picks = (
            league_context.get("my_picks_this_draft", []) +
            league_context.get("my_existing_roster", [])
        )
        _, sim_taxi = _build_sim_state(all_picks, league_context)
        league_context["my_taxi_players"] = [
            p.get("name") for p in sim_taxi.values() if p.get("name")
        ]

    return {
        "draft_detail": draft_detail,
        "my_slot": my_slot,
        "league_detail": league_detail,
        "my_roster": my_roster,
        "my_roster_id": my_roster_id,
        "picks": picks,
        "is_dynasty": is_dynasty,
        "available": available,
        "my_draft_picks": my_draft_picks,
        "starter_ids": starter_ids,
        "league_context": league_context,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/api/default-username")
def api_default_username():
    return jsonify({"username": SLEEPER_USERNAME or ""})

@app.route("/")
def index():
    return send_from_directory("templates", "index.html")


@app.route("/api/leagues")
def api_leagues():
    username = request.args.get("username", SLEEPER_USERNAME)
    try:
        user = get_user(username)
        user_id = user["user_id"]
        leagues = get_leagues(user_id)
        result = []
        for league in leagues:
            drafts = get_drafts(league["league_id"])
            for draft in drafts:
                result.append({
                    "league_id": league["league_id"],
                    "league_name": league.get("name", "Unknown League"),
                    "draft_id": draft["draft_id"],
                    "draft_status": draft.get("status"),
                    "season": draft.get("season"),
                    "user_id": user_id,
                })
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/draft/<draft_id>")
def api_draft(draft_id):
    """
    Returns the current draft state for the UI — picks feed, stats, and
    league context. Called on initial load and every 5 seconds by the poller.
    """
    try:
        league_id = request.args.get("league_id")
        user_id = request.args.get("user_id")

        state = _build_draft_state(draft_id, league_id, user_id)

        picks = state["picks"]
        my_roster_id = state["my_roster_id"]
        league_detail = state["league_detail"]
        league_context = state["league_context"]
        is_dynasty = state["is_dynasty"]

        picks_feed = [
            {
                "pick_no": p.get("pick_no"),
                "round": p.get("round"),
                "round_slot": p.get("draft_slot"),
                "player_id": p.get("player_id"),
                "player_name": PLAYERS.get(p.get("player_id"), {}).get("full_name"),
                "position": PLAYERS.get(p.get("player_id"), {}).get("position"),
                "team": PLAYERS.get(p.get("player_id"), {}).get("team"),
                "is_mine": p.get("roster_id") == my_roster_id,
            }
            for p in picks
        ]

        return jsonify({
            "draft_id": draft_id,
            "league_id": league_id,
            "picks": picks_feed,
            "current_pick": len(picks) + 1,
            "league_context": league_context,
            "is_dynasty": is_dynasty,
            "my_roster_id": my_roster_id,
            "my_draft_slot": state["my_slot"],
            "roster_positions": league_detail.get("roster_positions", []),
        })

    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/api/recommend", methods=["POST"])
def api_recommend():
    """
    Fetches fresh draft state from Sleeper and returns a Claude-generated
    pick recommendation. Always re-fetches picks to avoid stale data.
    """
    try:
        data = request.json
        draft_id = data["draft_id"]
        league_id = data["league_id"]
        user_id = data["user_id"]

        state = _build_draft_state(draft_id, league_id, user_id)

        rec = get_recommendation(
            state["picks"],
            state["available"],
            state["my_roster"],
            state["league_context"],
            len(state["picks"]) + 1,
            PLAYERS
        )

        # Verify recommended player is still available.
        # Fetch picks fresh from Sleeper right now — not the cached picks from
        # _build_draft_state — to catch any picks made during Claude's response time.
        rec_name = rec.get("recommendation")
        if rec_name:
            fresh_picks = get_picks(data["draft_id"])
            picked_names = {
                PLAYERS.get(p.get("player_id"), {}).get("full_name")
                for p in fresh_picks
                if p.get("player_id")
            }
            if rec_name in picked_names:
                print(f"Race condition detected: {rec_name} already picked")
                return jsonify({"error": "The board changed while generating your recommendation. Please try again."}), 409

        return jsonify(rec)

    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    dev_mode = os.environ.get("DEV_MODE", "false").lower() == "true"
    from werkzeug.serving import run_simple
    run_simple('0.0.0.0', port, app, use_reloader=False, use_debugger=True)