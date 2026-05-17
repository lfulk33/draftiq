import streamlit as st
import json
import time
from datetime import datetime
from sleeper_draft import get_picks, get_available_rookies, get_available_players, count_my_picks
from sleeper_league import get_rosters, get_league, get_taxi_count, get_league_users
from draft_advisor import get_recommendation
from poller import is_rookie_draft
from config import SLEEPER_USERNAME

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

def build_league_context(league_detail, draft_detail, my_roster, picks, my_roster_id, players):
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
        "picks_made_by_me": my_picks_count,
        "picks_remaining_for_me": total_rounds - my_picks_count,
        "my_existing_roster": my_existing_players
    }

def render_roster(my_roster, players):
    st.subheader("📊 Your Current Roster")
    
    positions = {"QB": [], "RB": [], "WR": [], "TE": [], "Other": []}
    
    for pid in my_roster.get("players") or []:
        player = players.get(pid, {})
        if not player:
            continue
        pos = player.get("position", "Other")
        if pos not in positions:
            pos = "Other"
        positions[pos].append({
            "name": player.get("full_name", "Unknown"),
            "age": player.get("fc_age") or player.get("age", "?"),
            "value": player.get("fc_value", "unranked")
        })

    cols = st.columns(4)
    for i, pos in enumerate(["QB", "RB", "WR", "TE"]):
        with cols[i]:
            st.write(f"**{pos}**")
            players_at_pos = sorted(
                positions[pos],
                key=lambda x: x["value"] if isinstance(x["value"], int) else 0,
                reverse=True
            )
            for p in players_at_pos:
                age_str = f"Age {p['age']}" if p['age'] != "?" else "Age ?"
                value_str = f"Val {p['value']}" if p['value'] != "unranked" else "unranked"
                st.caption(f"{p['name']} — {age_str} — {value_str}")

def render_setup():
    st.title("🏈 Draft Assistant")
    st.subheader("Connect to your draft")

    username = st.text_input("Sleeper username", value=SLEEPER_USERNAME)

    if st.button("Load leagues"):
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

    if "leagues" in st.session_state:
        league_names = [l["name"] for l in st.session_state.leagues]
        selected_league = st.selectbox("Select league", league_names)
        league_index = league_names.index(selected_league)
        league = st.session_state.leagues[league_index]

        if st.button("Load drafts"):
            with st.spinner("Fetching drafts..."):
                try:
                    from sleeper_draft import get_drafts
                    drafts = get_drafts(league["league_id"])
                    st.session_state.selected_league = league
                    st.session_state.drafts = drafts
                except Exception as e:
                    st.error(f"Error fetching drafts: {e}")

    if "drafts" in st.session_state:
        draft_labels = [
            f"{d.get('season', 'Unknown')} {d.get('metadata', {}).get('name', d['type'].title() + ' Draft')} (Rounds {d['settings'].get('rounds', '?')}) - {d['status'].replace('_', ' ').title()}"
            for d in st.session_state.drafts
        ]
        selected_draft = st.selectbox("Select draft", draft_labels)
        draft_index = draft_labels.index(selected_draft)
        draft = st.session_state.drafts[draft_index]

        if st.button("Connect to draft"):
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
                    st.rerun()

                except Exception as e:
                    st.error(f"Error connecting: {e}")

def render_draft():
    players = st.session_state.players
    draft_id = st.session_state.draft_id
    my_roster_id = st.session_state.my_roster_id
    my_roster = st.session_state.my_roster
    league_detail = st.session_state.league_detail
    draft_detail = st.session_state.draft_detail
    roster_to_team = st.session_state.roster_to_team

    picks = get_picks(draft_id)
    current_count = len(picks)
    rookie_draft = is_rookie_draft(draft_detail)
    available = get_available_rookies(players, picks) if rookie_draft else get_available_players(players, picks)
    league_context = build_league_context(league_detail, draft_detail, my_roster, picks, my_roster_id, players)

    if current_count > st.session_state.last_pick_count:
        st.session_state.last_pick_count = current_count
        with st.spinner("Getting recommendation..."):
            rec = get_recommendation(picks, available, my_roster, league_context, current_count + 1)
            st.session_state.recommendation = rec

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
            label = f"Pick {pick['pick_no']} — {name} ({pos}) — {team_name}"
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
            
            if confidence == "high":
                st.success(f"### {rec['recommendation']} ({rec['position']})")
                st.success(f"🟢 High confidence — value gap of {gap} points over next best")
            elif confidence == "medium":
                st.warning(f"### {rec['recommendation']} ({rec['position']})")
                st.warning(f"🟡 Medium confidence — value gap of {gap} points over next best")
            else:
                st.error(f"### {rec['recommendation']} ({rec['position']})")
                st.error(f"🔴 Low confidence — coin flip territory, gap of {gap} points")

            st.write(f"**Reasoning:** {rec['reasoning']}")
            st.write(f"**Positional note:** {rec['positional_note']}")
            st.write(f"**Upside:** {rec['upside']}")
            st.divider()
            st.write("**Alternatives:**")
            for alt in rec.get("alternatives", []):
                st.write(f"- **{alt['name']}** ({alt.get('position', '?')}): {alt['reason']}")
        else:
            st.info("Waiting for next pick...")

        if st.button("🔄 Refresh recommendation"):
            with st.spinner("Getting recommendation..."):
                rec = get_recommendation(picks, available, my_roster, league_context, current_count + 1)
                st.session_state.recommendation = rec
                st.rerun()

    st.divider()
    render_roster(my_roster, players)

    st.sidebar.title("⚙️ Settings")
    st.sidebar.write(f"**League:** {st.session_state.selected_league['name']}")
    st.sidebar.write(f"**Draft status:** {draft_detail['status']}")
    st.sidebar.write(f"**Last updated:** {st.session_state.last_refresh}")
    auto_refresh = st.sidebar.toggle("Auto refresh", value=True)
    refresh_interval = st.sidebar.slider("Refresh interval (seconds)", 10, 60, 15)

    if auto_refresh:
        time.sleep(refresh_interval)
        st.rerun()

init_session()

if st.session_state.connected:
    render_draft()
else:
    render_setup()