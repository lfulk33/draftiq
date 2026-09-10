import os
from flask import Flask, jsonify, request, send_from_directory, render_template
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder="static", template_folder="templates")
CORS(app)

from config import (
    SLEEPER_USERNAME, BPA_THRESHOLD_DYNASTY, BPA_THRESHOLD_REDRAFT,
    TAXI_THRESHOLD_QB, TAXI_THRESHOLD_RB, TAXI_THRESHOLD_WR, TAXI_THRESHOLD_TE
)
from sleeper_league import get_user, get_leagues, get_rosters, get_taxi_players, get_taxi_count, load_players
from sleeper_draft import (
    get_drafts, get_draft_detail, get_picks,
    get_available_rookies, get_available_players, count_my_picks
)
from draft_advisor import (
    get_recommendation, get_recommendation_raw, calculate_starter_ids,
    calculate_roster_needs, get_roster_recommendations
)
from salary_cap import load_salaries, load_keepers
from fantasypros_client import load_fantasypros_redraft_values
import historical_stats
import player_overrides
import waiver_scout
import algorithmic_waiver_scan
import chopped_bid_advisor
import daily_reports

# Load players once at startup
PLAYERS = {str(k): v for k, v in load_players().items()}

# Redraft, full-PPR leagues use FantasyPros' overall rankings instead of
# FantasyCalc's redraft_value — see _build_draft_state for the exact
# qualification check (is_dynasty=False and scoring rec==1). Dynasty
# leagues and anything not full-PPR are unaffected.
FANTASYPROS_REDRAFT_VALUES, _fp_unmatched = load_fantasypros_redraft_values(
    "FantasyPros_2026_Draft_ALL_Rankings.csv", PLAYERS
)
if _fp_unmatched:
    print(f"[fantasypros_client] {len(_fp_unmatched)} entries unmatched (kickers/DEF expected)")

# Weekly-elimination format: a starter who might not play this week isn't
# a real starter option the way he would be in a normal season-long
# league, regardless of his season-long value. Scoped to exactly this one
# league, not a general redraft setting — see draft_advisor._calculate_urgency
# for where this gates out injury-flagged players from the starter
# opportunity-cost calculation specifically (they still show up normally
# as real, pickable backup-tier options at their own value).
CHOPPED_LEAGUE_ID = "1313587679283138560"

# POC: salary-cap tracking, scoped to exactly one league. Not a general
# feature yet — see project memory for the plan to generalize this later.
DSFF_LEAGUE_ID = "1312162869869031424"
DSFF_SALARY_CAP = 200
DSFF_SALARIES, _dsff_salary_unmatched = load_salaries("dsff_salaries.csv", PLAYERS)
DSFF_KEEPERS, _dsff_keeper_unmatched = load_keepers("dsff_keepers.csv", SLEEPER_USERNAME, PLAYERS)
if _dsff_salary_unmatched:
    print(f"[salary_cap] {len(_dsff_salary_unmatched)} salary entries unmatched: {_dsff_salary_unmatched}")
if _dsff_keeper_unmatched:
    print(f"[salary_cap] {len(_dsff_keeper_unmatched)} keeper entries unmatched: {_dsff_keeper_unmatched}")


def build_league_context(league_detail, draft_detail, my_roster, picks,
                          my_roster_id, players, my_draft_picks=None,
                          is_dynasty=True, starter_ids=None, rosters=None):
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
        "picks_made_total": sum(1 for p in picks if not p.get("is_keeper")),
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
        "backup_needs": {
            "QB": 1 if sum(1 for s in league_detail.get("roster_positions", []) if s == "QB") > 0 else 0,
            "RB": 1 if sum(1 for s in league_detail.get("roster_positions", []) if s == "RB") > 0 else 0,
            "WR": 1 if sum(1 for s in league_detail.get("roster_positions", []) if s == "WR") > 0 else 0,
            "TE": 1 if sum(1 for s in league_detail.get("roster_positions", []) if s == "TE") > 0 else 0,
        },
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

        # For team-aware "who drafts next" simulation in draft_advisor —
        # lets the urgency simulation ask "what does THIS specific team
        # picking next actually need" instead of assuming every pick goes
        # in pure ADP order league-wide.
        "all_picks": picks,
        "slot_to_roster_id": draft_detail.get("slot_to_roster_id", {}),
        "rosters_by_id": {r["roster_id"]: r for r in (rosters or [])},

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

        # A continuing dynasty league's annual draft is the incoming rookie
        # class — draft_detail.type never actually distinguishes "rookie
        # draft" (Sleeper only uses it for snake/linear/auction), so this is
        # the reliable signal instead. In a rookie draft, positional need
        # shouldn't gate the recommendation at all — see calculate_bpa.
        "is_rookie_draft": bool(is_dynasty and league_detail.get("previous_league_id")),
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

    # Redraft + full-PPR leagues run on FantasyPros rankings instead of
    # FantasyCalc's redraft_value. Build a per-request override rather than
    # mutating the shared PLAYERS dict, so dynasty leagues (which also read
    # fc_redraft_value for taxi/redraft-proxy checks) are unaffected.
    is_full_ppr = league_detail.get("scoring_settings", {}).get("rec") == 1
    if not is_dynasty and is_full_ppr:
        effective_players = {
            pid: ({**p, "fc_redraft_value": FANTASYPROS_REDRAFT_VALUES[pid]}
                  if pid in FANTASYPROS_REDRAFT_VALUES else p)
            for pid, p in PLAYERS.items()
        }
    else:
        effective_players = PLAYERS

    # Redraft leagues: rescale each skill-position player's current
    # positional rank onto a real, ground-truth points scale (3-year
    # average, weighted by this league's own scoring), so VORP comparisons
    # across positions are fair regardless of whether the market-value
    # source (FantasyPros/FantasyCalc) was built with this league's roster
    # format in mind — see historical_stats.py for the full rationale.
    # Dynasty is unaffected: real points has no equivalent for long-term
    # asset value.
    if not is_dynasty:
        scoring_settings = league_detail.get("scoring_settings") or {}
        real_points_values = historical_stats.apply_real_points_translation(
            effective_players, "fc_redraft_value", scoring_settings
        )
        effective_players = {
            pid: ({**p, "fc_redraft_value": real_points_values[pid]}
                  if pid in real_points_values else p)
            for pid, p in effective_players.items()
        }

    # Manual per-player corrections (see player_overrides.py) — hand-verified
    # facts Sleeper's own data doesn't reliably carry, most commonly a
    # suspension/exempt-list situation (or, for Best Ball, real skepticism
    # about an official injury timeline) that never shows up in
    # injury_status. Applies to every league — these are deliberate,
    # individually-verified corrections, not a blanket policy, so there's
    # no reason to scope them to one league the way strict_starter_health
    # (below) is. Stamped as a separate is_backup_only_override field
    # rather than reusing injury_status, so it doesn't get tangled with
    # strict_starter_health's Chopped-only Sleeper-injury_status check —
    # a Best Ball league should never auto-exclude every Sleeper-flagged
    # injury (that league wants ceiling/risk tolerance), only the specific
    # players actually placed on this list.
    overrides, _overrides_unmatched = player_overrides.load_overrides(effective_players)
    if _overrides_unmatched:
        print(f"[player_overrides] {len(_overrides_unmatched)} unmatched: {_overrides_unmatched}")
    removed_ids = {pid for pid, state in overrides.items() if state == "removed"}
    backup_only_ids = {pid for pid, state in overrides.items() if state == "backup_only"}
    effective_players = {
        pid: ({**p, "is_backup_only_override": True} if pid in backup_only_ids else p)
        for pid, p in effective_players.items()
        if pid not in removed_ids
    }

    available = (
        get_available_rookies(effective_players, picks)
        if rookie_draft
        else get_available_players(effective_players, picks)
    )

    # In a continuing dynasty/keeper league, every team's carried-over roster
    # already holds real players before this draft even starts. picks only
    # covers this draft session, so without this, anyone already rostered on
    # another team from last season (e.g. an established starter) shows up
    # as "available" even though they were never actually on the board.
    all_rostered_ids = {
        pid for r in rosters for pid in (r.get("players") or [])
    }
    if all_rostered_ids:
        available = {
            pid: p for pid, p in available.items()
            if pid not in all_rostered_ids
        }

    # Once a draft goes live, Sleeper pre-fills every keeper's slot into the
    # picks list (is_keeper=True) — those are placeholders for players
    # already on the roster, not real selections made this draft. Without
    # excluding them, keepers get counted twice: once via keeper_cost (the
    # keeper CSV) and again here as if freshly drafted.
    my_draft_picks = [
        p["player_id"]
        for p in picks
        if p.get("roster_id") == my_roster_id and p.get("player_id") and not p.get("is_keeper")
    ]

    active_ids = set(my_roster.get("players") or [])
    taxi_ids = set(get_taxi_players(my_roster))
    active_ids -= taxi_ids
    starter_ids = calculate_starter_ids(list(active_ids), PLAYERS, league_detail)

    # A continuing dynasty league's annual draft isn't necessarily
    # rookies-only just because it's dynasty + has a previous_league_id —
    # some leagues re-inject cut veterans into the same pool (e.g. Alvin
    # Kamara, Tyreek Hill showing up alongside true rookies). Pure-BPA mode
    # is only correct when the pool really is rookies-only; otherwise it
    # skips positional need/urgency entirely and just chases raw value.
    # Detect real veteran talent in the pool as the tell — a true
    # rookie-only draft has no such players available at all.
    significant_veterans = sum(
        1 for p in available.values()
        if (p.get("years_exp") or 0) >= 1 and (p.get("fc_value") or 0) >= 500
    )
    has_veteran_talent = significant_veterans >= 3

    my_draft_slot = draft_detail.get("slot_to_roster_id", {})
    # Find which slot this user's roster occupies
    my_slot = next(
        (int(slot) for slot, rid in my_draft_slot.items() if rid == my_roster_id),
        None
    )

    league_context = build_league_context(
        league_detail, draft_detail, my_roster, picks,
        my_roster_id, effective_players, my_draft_picks, is_dynasty, starter_ids,
        rosters
    )
    league_context["my_draft_slot"] = my_slot
    league_context["strict_starter_health"] = league_id == CHOPPED_LEAGUE_ID
    if has_veteran_talent:
        league_context["is_rookie_draft"] = False

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

    # POC: salary-cap budget tracking, scoped to exactly one league.
    # remaining_slots deliberately does NOT use draft_detail.settings.rounds —
    # that value is unreliable/not yet finalized for this league. It's a
    # full draft meant to fill every open roster spot, so remaining slots
    # comes from the league's actual roster construction instead.
    if league_id == DSFF_LEAGUE_ID:
        # This is a continuing dynasty league, but NOT a rookie-only draft —
        # the salary sheet has 519 priced players (established vets like
        # Bijan Robinson $62 included), not a rookie class. is_rookie_draft's
        # heuristic (dynasty + previous_league_id) assumes every continuing
        # dynasty league's annual draft is rookies-only, which is wrong here:
        # this is a full restocking draft where cut veterans re-enter the
        # pool alongside rookies. Positional/urgency weighting needs to stay
        # active — real salary is being spent on players who play this
        # season, not just accumulated as future dynasty assets.
        league_context["is_rookie_draft"] = False

        keeper_cost = sum(DSFF_KEEPERS.values())
        drafted_cost = sum(DSFF_SALARIES.get(pid, 0) for pid in my_draft_picks)
        total_spent = keeper_cost + drafted_cost
        remaining_budget = DSFF_SALARY_CAP - total_spent
        total_roster_spots = len(league_detail.get("roster_positions", []))
        remaining_slots = max(1, total_roster_spots - len(DSFF_KEEPERS) - len(my_draft_picks))
        # Most you could spend on any single player and still leave $1 for
        # every other remaining slot — same formula as the hard affordability
        # gate in draft_advisor._apply_salary_adjustment.
        max_affordable = remaining_budget - (remaining_slots - 1) * 1

        league_context["salary_cap"] = {
            "cap": DSFF_SALARY_CAP,
            "keeper_cost": keeper_cost,
            "drafted_cost": drafted_cost,
            "total_spent": total_spent,
            "remaining_budget": remaining_budget,
            "remaining_slots": remaining_slots,
            "avg_per_slot": round(remaining_budget / remaining_slots, 2),
            "max_affordable": max_affordable,
            "salaries": DSFF_SALARIES,
        }

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
        "all_players": effective_players,
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
                "is_keeper": bool(p.get("is_keeper")),
                "salary": DSFF_SALARIES.get(p.get("player_id")) if league_id == DSFF_LEAGUE_ID else None,
            }
            for p in picks
        ]

        return jsonify({
            "draft_id": draft_id,
            "league_id": league_id,
            "picks": picks_feed,
            "current_pick": sum(1 for p in picks if not p.get("is_keeper")) + 1,
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
    Fetches fresh draft state from Sleeper and returns a pick
    recommendation. Always re-fetches picks to avoid stale data.

    use_claude (default true): when false, skips the Claude call entirely
    and returns the deterministic algorithm's numbers directly — VORP,
    value, positional rank, replacement level for the pick and the top
    alternatives at every position — no narrative text, no LLM cost.
    """
    try:
        data = request.json
        draft_id = data["draft_id"]
        league_id = data["league_id"]
        user_id = data["user_id"]
        use_claude = data.get("use_claude", True)

        state = _build_draft_state(draft_id, league_id, user_id)

        if use_claude:
            rec = get_recommendation(
                state["picks"],
                state["available"],
                state["my_roster"],
                state["league_context"],
                len(state["picks"]) + 1,
                state["all_players"]
            )
        else:
            rec = get_recommendation_raw(
                state["available"],
                state["league_context"],
                state["all_players"]
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


def _generate_one_waiver_report(league_id, user_id):
    import sleeper_league as sl
    league_detail = sl.get_league(league_id)
    league_name = league_detail.get("name", league_id)
    rosters = get_rosters(league_id)
    my_roster = next((r for r in rosters if r.get("owner_id") == user_id), None)
    if not my_roster:
        return {"league_id": league_id, "league_name": league_name, "error": "Roster not found for this user in this league."}

    is_dynasty = league_detail.get("settings", {}).get("type") == 2
    roster_summary = waiver_scout.build_roster_summary(my_roster, PLAYERS, league_detail)
    available_summary = waiver_scout.build_available_summary(rosters, PLAYERS)
    report_text, claude_error = waiver_scout.safe_generate_report(
        waiver_scout.generate_waiver_report, league_name, is_dynasty, roster_summary, available_summary
    )
    return {
        "league_id": league_id, "league_name": league_name,
        "available": available_summary, "report": report_text, "claude_error": claude_error,
    }


@app.route("/api/waiver-report")
def api_waiver_report():
    """
    Runs the waiver-wire scouting report across whichever leagues the user
    checked on the setup screen, in parallel (each involves several real
    web searches, so this can take a while run sequentially).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    username = request.args.get("username", SLEEPER_USERNAME)
    if not username:
        return jsonify({"error": "No username provided."}), 400
    user = get_user(username)
    if not user:
        return jsonify({"error": f"Sleeper user '{username}' not found."}), 404
    user_id = user["user_id"]

    league_ids = [lid for lid in request.args.get("league_ids", "").split(",") if lid]
    if not league_ids:
        return jsonify({"error": "No leagues selected."}), 400

    results = []
    with ThreadPoolExecutor(max_workers=len(league_ids)) as executor:
        futures = {
            executor.submit(_generate_one_waiver_report, lid, user_id): lid
            for lid in league_ids
        }
        for future in as_completed(futures):
            lid = futures[future]
            try:
                results.append(future.result())
            except Exception as e:
                results.append({"league_id": lid, "league_name": lid, "error": str(e)})

    results.sort(key=lambda r: league_ids.index(r["league_id"]))
    return jsonify({"generated_at": __import__("datetime").datetime.utcnow().isoformat() + "Z", "reports": results})


@app.route("/reports/waivers")
def reports_waivers():
    """
    Bookmarkable page showing the last cron-generated waiver report across
    every league (see daily_reports.py) — no button to click, no leagues
    to pick, just whatever the morning run produced. Distinct from
    /api/waiver-report + the "Scout Waivers" UI mode, which still exist
    for on-demand runs against whichever leagues you select live.
    """
    import json
    if not os.path.exists(daily_reports.WAIVER_REPORT_PATH):
        return render_template("reports_waivers.html", generated_at=None, reports=[])
    with open(daily_reports.WAIVER_REPORT_PATH) as f:
        payload = json.load(f)
    return render_template("reports_waivers.html", generated_at=payload.get("generated_at"), reports=payload.get("reports", []))


def _generate_one_bid_report(league_id, user_id):
    import sleeper_league as sl
    league_detail = sl.get_league(league_id)
    league_name = league_detail.get("name", league_id)
    rosters = get_rosters(league_id)
    my_roster = next((r for r in rosters if r.get("owner_id") == user_id), None)
    if not my_roster:
        return {"league_id": league_id, "league_name": league_name, "error": "Roster not found for this user in this league."}

    is_dynasty = league_detail.get("settings", {}).get("type") == 2
    roster_summary = waiver_scout.build_roster_summary(my_roster, PLAYERS, league_detail)
    available_summary = waiver_scout.build_available_summary(rosters, PLAYERS)
    algorithmic = algorithmic_waiver_scan.run_algorithmic_scan(roster_summary, available_summary, is_dynasty)
    budget_summary = chopped_bid_advisor.build_budget_summary(my_roster, league_detail)
    report_text, claude_error = waiver_scout.safe_generate_report(
        chopped_bid_advisor.generate_bid_report, league_name, roster_summary, available_summary, budget_summary
    )
    return {
        "league_id": league_id, "league_name": league_name, "budget": budget_summary, "is_dynasty": is_dynasty,
        "algorithmic": algorithmic, "available": available_summary, "report": report_text, "claude_error": claude_error,
    }


@app.route("/api/chopped-bid-report")
def api_chopped_bid_report():
    """
    Runs the weekly Chopped-league bid-strategy report across whichever
    leagues the user checked, in parallel (each involves several real web
    searches, so this can take a while run sequentially).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    username = request.args.get("username", SLEEPER_USERNAME)
    if not username:
        return jsonify({"error": "No username provided."}), 400
    user = get_user(username)
    if not user:
        return jsonify({"error": f"Sleeper user '{username}' not found."}), 404
    user_id = user["user_id"]

    league_ids = [lid for lid in request.args.get("league_ids", "").split(",") if lid]
    if not league_ids:
        return jsonify({"error": "No leagues selected."}), 400

    results = []
    with ThreadPoolExecutor(max_workers=len(league_ids)) as executor:
        futures = {
            executor.submit(_generate_one_bid_report, lid, user_id): lid
            for lid in league_ids
        }
        for future in as_completed(futures):
            lid = futures[future]
            try:
                results.append(future.result())
            except Exception as e:
                results.append({"league_id": lid, "league_name": lid, "error": str(e)})

    results.sort(key=lambda r: league_ids.index(r["league_id"]))
    return jsonify({"generated_at": __import__("datetime").datetime.utcnow().isoformat() + "Z", "reports": results})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    dev_mode = os.environ.get("DEV_MODE", "false").lower() == "true"
    from werkzeug.serving import run_simple
    run_simple('0.0.0.0', port, app, use_reloader=False, use_debugger=True)