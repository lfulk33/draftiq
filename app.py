import streamlit as st
import json
import time
import math
from datetime import datetime
from sleeper_draft import get_picks, get_available_rookies, get_available_players, count_my_picks
from sleeper_league import get_rosters, get_league, get_taxi_count, get_league_users
from poller import is_rookie_draft
from config import SLEEPER_USERNAME
from config import BPA_THRESHOLD_DYNASTY, BPA_THRESHOLD_REDRAFT

st.set_page_config(
    page_title="Draft Assistant",
    page_icon="🏈",
    layout="wide"
)

def init_session():
    if "connected" not in st.session_state:
        st.session_state.connected = False
    if "recommendation" not in st.session_state:
        st.session_state.recommendation = None
    if "last_pick_count" not in st.session_state:
        st.session_state.last_pick_count = 0
    if "last_refresh" not in st.session_state:
        st.session_state.last_refresh = None
    if "roster_to_team" not in st.session_state:
        st.session_state.roster_to_team = {}
    if "roster_recommendations" not in st.session_state:
        st.session_state.roster_recommendations = []
    if "roster_sim_active" not in st.session_state:
        st.session_state.roster_sim_active = {}
    if "roster_sim_taxi" not in st.session_state:
        st.session_state.roster_sim_taxi = {}
    if "last_draft_status" not in st.session_state:
        st.session_state.last_draft_status = None
    if "claude_calls" not in st.session_state:
        st.session_state.claude_calls = 0
    if "analyze_calls" not in st.session_state:
        st.session_state.analyze_calls = 0

def build_roster_to_team(users, rosters):
    user_map = {u["user_id"]: u for u in users}
    roster_to_team = {}
    for roster in rosters:
        owner_id = roster["owner_id"]
        user = user_map.get(owner_id, {})
        metadata = user.get("metadata", {})
        team_name = metadata.get("team_name") or user.get("display_name", f"Roster {roster['roster_id']}")
        roster_to_team[roster["roster_id"]] = team_name
    return roster_to_team

def build_league_context(league_detail, draft_detail, my_roster, picks, my_roster_id, players, my_draft_picks=None, is_dynasty=True, starter_ids=None):
    from draft_advisor import calculate_roster_needs
    my_picks_count = count_my_picks(picks, my_roster_id)
    total_rounds = draft_detail["settings"].get("rounds", 4)

    my_existing_players = []
    for pid in my_roster.get("players") or []:
        player = players.get(pid, {})
        if player:
            my_existing_players.append({
                "name": player.get("full_name"),
                "position": player.get("position"),
                "age": player.get("fc_age") or player.get("age"),
                "dynasty_value": player.get("fc_value", "unranked")
            })

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
        "picks_remaining_for_me": total_rounds - my_picks_count,
        "my_existing_roster": my_existing_players,
        "my_picks_this_draft": [
            {
                "name": players.get(pid, {}).get("full_name", "Unknown"),
                "position": players.get(pid, {}).get("position", "?"),
                "dynasty_value": players.get(pid, {}).get("fc_value", "unranked")
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
        "starter_needs": {
            pos: max(0, dedicated - sum(1 for p in my_existing_players + [
                {"position": players.get(pid, {}).get("position")} 
                for pid in (my_draft_picks or [])
            ] if p.get("position") == pos))
            for pos, dedicated in {
                "QB": sum(1 for s in league_detail.get("roster_positions", []) if s == "QB"),
                "RB": sum(1 for s in league_detail.get("roster_positions", []) if s == "RB"),
                "WR": sum(1 for s in league_detail.get("roster_positions", []) if s == "WR"),
                "TE": sum(1 for s in league_detail.get("roster_positions", []) if s == "TE"),
            }.items()
        },
        "backup_needs": {
            pos: backups
            for pos, backups in calculate_roster_needs(league_detail)[1].items()
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
        "bpa_threshold": BPA_THRESHOLD_DYNASTY if is_dynasty else BPA_THRESHOLD_REDRAFT,
        "value_type": "dynasty_value" if is_dynasty else "redraft_value",
    }

def render_roster(my_roster, players, league_detail, sim_active, sim_taxi, is_dynasty=True):
    from draft_advisor import calculate_starter_ids

    st.subheader("📊 Recommended Roster")
    st.caption("🟢 Starter   ⬜ Bench   🚕 Taxi   🏥 IR (if healthy)")

    active_ids = set(sim_active.keys())
    taxi_ids = set(sim_taxi.keys())
    reserve_ids = set(my_roster.get("reserve") or [])

    enriched = []
    for pid in active_ids:
        player = players.get(pid, {})
        if not player:
            continue
        enriched.append({
            "id": pid,
            "name": player.get("full_name", "Unknown"),
            "position": player.get("position", "?"),
            "age": player.get("fc_age") or player.get("age", "?"),
            "value": player.get("fc_value", 0) if isinstance(player.get("fc_value"), int) else 0,
            "years_exp": player.get("years_exp", 99),
            "starter": False,
            "on_ir": pid in reserve_ids
        })

    enriched.sort(key=lambda x: (
        players.get(x["id"], {}).get("fc_redraft_value") or 0,
        x["value"]
    ), reverse=True)

    starter_ids = calculate_starter_ids(set(p["id"] for p in enriched), players, league_detail)
    for player in enriched:
        player["starter"] = player["id"] in starter_ids

    pos_order = ["QB", "RB", "WR", "TE"]
    all_positions = sorted(set(p["position"] for p in enriched) - {"?"})
    extra_positions = [p for p in all_positions if p not in pos_order]
    columns = pos_order + extra_positions

    cols = st.columns(len(columns))

    for i, pos in enumerate(columns):
        with cols[i]:
            st.write(f"**{pos}**")
            pos_players = [p for p in enriched if p["position"] == pos]
            for p in pos_players:
                age_str = f"Age {p['age']}" if p['age'] != "?" else "Age ?"
                dynasty_str = f"D:{p['value']}" if p['value'] else "D:unranked"
                redraft_val = players.get(p['id'], {}).get('fc_redraft_value', 0) or 0
                redraft_str = f"R:{redraft_val}" if redraft_val else "R:unranked"
                val_str = f"{dynasty_str} | {redraft_str}"
                ir_note = " 🏥 (if healthy)" if p.get("on_ir") else ""
                team = players.get(p['id'], {}).get('team', '')
                team_str = f" ({team})" if team else ""
                label = f"{p['name']}{team_str}{ir_note}\n{age_str} | {val_str}"
                if p["starter"]:
                    st.success(label)
                else:
                    st.info(label)

    if taxi_ids and is_dynasty:
        st.divider()
        st.write("**🚕 Taxi Squad**")
        taxi_players = []
        for pid in taxi_ids:
            player = players.get(pid, {})
            if not player:
                continue
            value = player.get("fc_value", 0) if isinstance(player.get("fc_value"), int) else 0
            years_exp = player.get("years_exp", 99)
            taxi_years = league_detail["settings"].get("taxi_years", 3)
            note = None
            if value > 3000:
                note = "⬆️ Consider promoting"
            elif years_exp > taxi_years:
                note = "✂️ Cut candidate"
            taxi_players.append({
                "name": player.get("full_name", "Unknown"),
                "position": player.get("position", "?"),
                "age": player.get("fc_age") or player.get("age", "?"),
                "value": value,
                "note": note
            })

        taxi_cols = st.columns(max(len(taxi_players), 1))
        for i, p in enumerate(taxi_players):
            with taxi_cols[i]:
                team = players.get(pid, {}).get('team', '')
                team_str = f" ({team})" if team else ""
                label = f"{p['name']}{team_str} ({p['position']})\nAge {p['age']} | Val {p['value']}"
                if p["note"]:
                    if "promoting" in p["note"]:
                        st.warning(f"{label}\n{p['note']}")
                    else:
                        st.error(f"{label}\n{p['note']}")
                else:
                    st.info(f"🚕 {label}")

def render_roster_recommendations(recommendations):
    if not recommendations:
        return

    st.subheader("📋 Roster Action Items")

    for rec in recommendations:
        action = rec["action"]
        severity = rec["severity"]

        label = f"**{rec['player']} ({rec['position']})** — {action}"

        if severity == "success":
            st.success(label)
        elif severity == "error":
            st.error(label)
        else:
            st.info(label)

        st.caption(rec["reasoning"])

        for move in rec.get("cascading_moves", []):
            move_action = move.get("action", "")
            move_label = f"↳ **{move['player_name']}** — {move_action}: {move['reason']}"
            if move_action == "CUT":
                st.error(move_label)
            elif move_action == "PROMOTE_TO_BENCH":
                st.warning(move_label)
            else:
                st.info(move_label)

        st.divider()

def render_setup():
    st.title("🏈 Draft Assistant")
    st.subheader("Connect to your draft")

    username = st.text_input("Sleeper username", value=SLEEPER_USERNAME)

    if st.button("Load leagues", key="load_leagues_btn"):
        with st.spinner("Fetching leagues..."):
            try:
                from sleeper_league import get_user, get_leagues
                user = get_user(username)
                user_id = user["user_id"]
                leagues = get_leagues(user_id)
                st.session_state.user_id = user_id
                st.session_state.username = username
                st.session_state.leagues = leagues
            except Exception as e:
                st.error(f"Error fetching leagues: {e}")

    if "leagues" in st.session_state and not st.session_state.connected:
        league_names = [l["name"] for l in st.session_state.leagues]
        selected_league = st.selectbox("Select league", league_names, key="setup_league_select")
        league_index = league_names.index(selected_league)
        league = st.session_state.leagues[league_index]

        if st.button("Load drafts", key="load_drafts_btn"):
            with st.spinner("Fetching drafts..."):
                try:
                    from sleeper_draft import get_drafts
                    drafts = get_drafts(league["league_id"])
                    st.session_state.selected_league = league
                    st.session_state.drafts = drafts
                except Exception as e:
                    st.error(f"Error fetching drafts: {e}")

    if "drafts" in st.session_state and not st.session_state.connected:
        draft_labels = [
            f"{d.get('season', 'Unknown')} {d.get('metadata', {}).get('name', d['type'].title() + ' Draft')} (Rounds {d['settings'].get('rounds', '?')}) - {d['status'].replace('_', ' ').title()}"
            for d in st.session_state.drafts
        ]
        selected_draft = st.selectbox("Select draft", draft_labels, key="setup_draft_select")
        draft_index = draft_labels.index(selected_draft)
        draft = st.session_state.drafts[draft_index]

        if st.button("Connect to draft", key="connect_to_draft_btn"):
            with st.spinner("Connecting..."):
                try:
                    from sleeper_draft import get_draft_detail
                    from sleeper_league import get_rosters
                    import json

                    league = st.session_state.selected_league
                    league_id = league["league_id"]
                    user_id = st.session_state.user_id

                    league_detail = get_league(league_id)
                    rosters = get_rosters(league_id)
                    users = get_league_users(league_id)
                    my_roster = next(r for r in rosters if r["owner_id"] == user_id)
                    draft_detail = get_draft_detail(draft["draft_id"])
                    roster_to_team = build_roster_to_team(users, rosters)

                    with open("fantasy_players.json") as f:
                        players = json.load(f)

                    st.session_state.connected = True
                    st.session_state.league_id = league_id
                    st.session_state.my_roster_id = my_roster["roster_id"]
                    st.session_state.draft_id = draft["draft_id"]
                    st.session_state.league_detail = league_detail
                    st.session_state.draft_detail = draft_detail
                    st.session_state.my_roster = my_roster
                    st.session_state.players = players
                    st.session_state.roster_to_team = roster_to_team
                    for key in ["drafts", "leagues"]:
                        st.session_state.pop(key, None)
                    st.rerun()

                except Exception as e:
                    st.error(f"Error connecting: {e}")

def render_draft():
    from sleeper_league import get_rosters, get_taxi_players
    from draft_advisor import get_recommendation, get_roster_recommendations, calculate_starter_ids

    players = st.session_state.players
    draft_id = st.session_state.draft_id
    my_roster_id = st.session_state.my_roster_id
    league_detail = st.session_state.league_detail
    draft_detail = st.session_state.draft_detail
    is_dynasty = league_detail.get("settings", {}).get("type", 0) == 2
    roster_to_team = st.session_state.roster_to_team

    rosters = get_rosters(st.session_state.league_id)
    my_roster = next(r for r in rosters if r["roster_id"] == my_roster_id)

    picks = get_picks(draft_id)
    current_count = len(picks)
    rookie_draft = is_rookie_draft(draft_detail)
    available = get_available_rookies(players, picks) if rookie_draft else get_available_players(players, picks)

    my_draft_picks = [p["player_id"] for p in picks if p["roster_id"] == my_roster_id]
#    print(f"starter_needs: {league_context.get('starter_needs')}")
#    print(f"my_picks positions: {[p['position'] for p in league_context.get('my_picks_this_draft', [])]}")
    taxi_ids = set(get_taxi_players(my_roster))
    all_ids = set(my_roster.get("players") or [])
    if my_draft_picks:
        all_ids.update(my_draft_picks)
    active_ids = all_ids - taxi_ids

    starter_ids = calculate_starter_ids(active_ids, players, league_detail)
    league_context = build_league_context(league_detail, draft_detail, my_roster, picks, my_roster_id, players, my_draft_picks, is_dynasty, starter_ids)

    def enrich_pid(pid):
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
            "on_ir": pid in set(my_roster.get("reserve") or [])
        }

    raw_taxi_ids = set(get_taxi_players(my_roster))
    raw_sim_active = {pid: enrich_pid(pid) for pid in active_ids if enrich_pid(pid)}
    raw_sim_taxi = {pid: enrich_pid(pid) for pid in raw_taxi_ids if enrich_pid(pid)}

    display_active = st.session_state.roster_sim_active if st.session_state.roster_sim_active else raw_sim_active
    display_taxi = st.session_state.roster_sim_taxi if st.session_state.roster_sim_taxi else raw_sim_taxi

    st.sidebar.title("⚙️ Settings")
    st.sidebar.write(f"**League:** {st.session_state.selected_league['name']}")
    st.sidebar.write(f"**Draft status:** {draft_detail['status']}")
    st.sidebar.write(f"**Last updated:** {st.session_state.last_refresh}")
    auto_refresh = st.sidebar.toggle("Auto refresh", value=True)
    refresh_interval = st.sidebar.slider("Refresh interval (seconds)", 10, 60, 15)

    from config import DEV_MODE
    if draft_detail.get("status") == "complete" and not DEV_MODE:
        if st.session_state.get("last_draft_status") != "complete":
            st.session_state.roster_recommendations = []
            st.session_state.roster_sim_active = {}
            st.session_state.roster_sim_taxi = {}
            st.session_state.last_draft_status = "complete"
        display_active = raw_sim_active
        display_taxi = raw_sim_taxi

        st.title("🏈 Draft Assistant")
        st.success("✅ Draft Complete!")
        st.write(f"**League:** {st.session_state.selected_league['name']}")
        st.write(f"**Your picks this draft:** {len(my_draft_picks)}")
        st.divider()
        st.subheader("🎯 Your Draft Picks")
        for pid in my_draft_picks:
            player = players.get(pid, {})
            name = player.get("full_name", "Unknown")
            pos = player.get("position", "?")
            age = player.get("fc_age") or player.get("age", "?")
            value = player.get("fc_value", "unranked")
            st.info(f"**{name}** ({pos}) — Age {age} | Val {value}")
        st.divider()

        st.info("🏈 Live draft analysis is complete. Post-draft roster analysis coming soon — this will help you optimize cuts, taxi moves, and waiver targets based on your final roster.")

        roster_placeholder = st.empty()
        roster_placeholder.empty()
        with roster_placeholder.container():
            render_roster(my_roster, players, league_detail, display_active, display_taxi, is_dynasty)
            render_roster_recommendations(st.session_state.roster_recommendations)
        return

    st.session_state.last_refresh = datetime.now().strftime("%I:%M:%S %p")

    total_picks = draft_detail["settings"].get("rounds", 4) * draft_detail["settings"].get("teams", 12)

    st.title("🏈 Draft Assistant")
    col_header1, col_header2, col_header3 = st.columns(3)
    with col_header1:
        st.metric("Picks Made", f"{current_count} of {total_picks}")
    with col_header2:
        st.metric("My Picks", count_my_picks(picks, my_roster_id))
    with col_header3:
        st.metric("Draft Type", "Rookie" if rookie_draft else "Startup/Redraft")

    st.divider()

    col_left, col_right = st.columns([1, 1])

    with col_left:
        st.subheader("📋 Recent Picks")
        recent = picks[-10:] if len(picks) >= 10 else picks
        for pick in reversed(recent):
            pid = pick["player_id"]
            player = players.get(pid, {})
            name = player.get("full_name", "Unknown")
            pos = player.get("position", "?")
            roster_id = pick["roster_id"]
            is_mine = roster_id == my_roster_id
            team_name = roster_to_team.get(roster_id, f"Roster {roster_id}")
            dynasty = player.get('fc_value', '')
            redraft = player.get('fc_redraft_value', '')
            val_str = f" D:{dynasty}" if dynasty else ""
            val_str += f" R:{redraft}" if redraft else ""
            label = f"Pick {pick['pick_no']} — {name} ({pos}){val_str} — {team_name}"
            if is_mine:
                st.success(f"⭐ {label}")
            else:
                st.info(label)

    with col_right:
        st.subheader("🤖 Claude's Recommendation")
        if st.session_state.recommendation:
            rec = st.session_state.recommendation

            confidence = rec.get("confidence_tier", "unknown")
            gap = rec.get("confidence_gap")

            rec_player = next((p for p in available.values() if p.get("full_name") == rec["recommendation"]), {})
            rec_dynasty = rec_player.get("fc_value", "")
            rec_redraft = rec_player.get("fc_redraft_value", "")
            rec_val_str = f" | D:{rec_dynasty} R:{rec_redraft}" if rec_dynasty or rec_redraft else ""

            if confidence == "high":
                st.success(f"### {rec['recommendation']} ({rec['position']}){rec_val_str}")
                st.success(f"🟢 High confidence — {'rank gap of ' + str(gap) + ' positions' if is_dynasty else 'value gap of ' + str(gap) + ' points'} over next best")
            elif confidence == "medium":
                st.warning(f"### {rec['recommendation']} ({rec['position']}){rec_val_str}")
                st.warning(f"🟡 Medium confidence — {'rank gap of ' + str(gap) + ' positions' if is_dynasty else 'value gap of ' + str(gap) + ' points'} over next best")
            else:
                st.error(f"### {rec['recommendation']} ({rec['position']}){rec_val_str}")
                st.error(f"🔴 Low confidence — coin flip territory, {'rank gap of ' + str(gap) + ' positions' if is_dynasty else 'gap of ' + str(gap) + ' points'}")

            st.write(f"**Reasoning:** {rec['reasoning']}")
            st.write(f"**Positional note:** {rec['positional_note']}")
            st.write(f"**Upside:** {rec['upside']}")
            st.divider()
            st.write("**Alternatives:**")
            for alt in rec.get("alternatives", []):
                alt_player = next((p for p in available.values() if p.get("full_name") == alt["name"]), {})
                dynasty = alt_player.get("fc_value", "")
                redraft = alt_player.get("fc_redraft_value", "")
                val_str = f" D:{dynasty}" if dynasty else ""
                val_str += f" R:{redraft}" if redraft else ""
                st.write(f"- **{alt['name']}** ({alt.get('position', '?')}){val_str}: {alt['reason']}")
        else:
            st.info("Waiting for next pick...")

        MAX_RECOMMENDATIONS = 20
        calls_left = MAX_RECOMMENDATIONS - st.session_state.claude_calls
        if calls_left > 0:
            if st.button(f"🎯 Get Recommendation ({calls_left} remaining)", key=f"rec_btn_{current_count}_{calls_left}"):
                with st.spinner("Getting recommendation..."):
                    rec = get_recommendation(picks, available, my_roster, league_context, current_count + 1, players)
                    st.session_state.recommendation = rec
                    st.session_state.claude_calls += 1
                    st.rerun()
        else:
            st.warning("Recommendation limit reached for this session.")

    st.divider()
    if is_dynasty:
        MAX_ANALYSES = 3
        analyses_left = MAX_ANALYSES - st.session_state.get("analyze_calls", 0)
        if analyses_left > 0:
            if st.button(f"🔍 Analyze My Picks ({analyses_left} remaining)", key="analyze_live"):
                st.session_state.roster_recommendations = []
                st.session_state.roster_sim_active = {}
                st.session_state.roster_sim_taxi = {}
                with st.spinner("Claude is analyzing your roster moves..."):
                    recs, r_sim_active, r_sim_taxi = get_roster_recommendations(my_roster, players, league_detail, my_draft_picks, starter_ids)
                    st.session_state.roster_recommendations = recs
                    st.session_state.roster_sim_active = r_sim_active
                    st.session_state.roster_sim_taxi = r_sim_taxi
                st.session_state.analyze_calls = st.session_state.get("analyze_calls", 0) + 1
                display_active = st.session_state.roster_sim_active if st.session_state.roster_sim_active else raw_sim_active
                display_taxi = st.session_state.roster_sim_taxi if st.session_state.roster_sim_taxi else raw_sim_taxi
        else:
            st.warning("Analysis limit reached for this session.")

    roster_placeholder = st.empty()
    roster_placeholder.empty()
    with roster_placeholder.container():
        render_roster(my_roster, players, league_detail, display_active, display_taxi, is_dynasty)
        render_roster_recommendations(st.session_state.roster_recommendations)

    
    if auto_refresh:
        if "last_rerun" not in st.session_state:
            st.session_state.last_rerun = datetime.now()
        elapsed = (datetime.now() - st.session_state.last_rerun).seconds
        if elapsed >= refresh_interval:
            st.session_state.last_rerun = datetime.now()
            st.rerun()
        else:
            time.sleep(1)
            st.rerun()

init_session()
setup_placeholder = st.empty()
if st.session_state.connected:
    setup_placeholder.empty()
    render_draft()
else:
    with setup_placeholder.container():
        render_setup()