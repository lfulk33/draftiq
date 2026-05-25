// ── State ────────────────────────────────────────────────────────────────────
const state = {
  username: '',
  userId: '',
  leagues: [],
  selectedLeague: null,
  selectedDraftId: null,
  draftData: null,
  recommendation: null,
  polling: null,
};

// ── Helpers ──────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const show = id => document.getElementById(id).classList.remove('hidden');
const hide = id => document.getElementById(id).classList.add('hidden');

function showScreen(name) {
  document.querySelectorAll('.screen').forEach(s => {
    s.classList.remove('active');
    s.classList.add('hidden');
  });
  const screen = document.getElementById(`screen-${name}`);
  screen.classList.remove('hidden');
  screen.classList.add('active');
}

function formatPickNum(round, slot) {
  return `${round}.${String(slot).padStart(2, '0')}`;
}

function confidenceWidth(tier) {
  if (tier === 'high') return '90%';
  if (tier === 'medium') return '55%';
  return '25%';
}

function confidenceText(tier, gap) {
  if (tier === 'high') return `High confidence · gap ${gap}`;
  if (tier === 'medium') return `Medium confidence · gap ${gap}`;
  return `Low confidence · gap ${gap}`;
}

// ── Setup Screen ─────────────────────────────────────────────────────────────
$('btn-load-leagues').addEventListener('click', loadLeagues);
$('input-username').addEventListener('keydown', e => {
  if (e.key === 'Enter') loadLeagues();
});

async function loadLeagues() {
  const username = $('input-username').value.trim();
  if (!username) return;

  $('btn-load-leagues').textContent = 'Loading...';
  $('btn-load-leagues').disabled = true;
  hide('setup-error');
  hide('league-list');

  try {
    const res = await fetch(`/api/leagues?username=${encodeURIComponent(username)}`);
    const data = await res.json();

    if (data.error) throw new Error(data.error);

    state.username = username;
    state.userId = data[0]?.user_id || '';
    state.leagues = data;

    renderLeagueList(data);
    show('league-list');
  } catch (err) {
    $('setup-error').textContent = err.message || 'Failed to load leagues.';
    show('setup-error');
  } finally {
    $('btn-load-leagues').textContent = 'Load Leagues';
    $('btn-load-leagues').disabled = false;
  }
}

function renderLeagueList(leagues) {
  const list = $('league-list');
  list.innerHTML = '';

  // Filter to active/recent drafts
  const sorted = [...leagues].sort((a, b) => {
    const order = { drafting: 0, pre_draft: 1, complete: 2 };
    return (order[a.draft_status] ?? 3) - (order[b.draft_status] ?? 3);
  });

  sorted.forEach(league => {
    const item = document.createElement('div');
    item.className = 'league-item';
    item.innerHTML = `
      <div class="league-name">${league.league_name}</div>
      <div class="league-meta">${league.season} · ${league.draft_id}</div>
      <span class="league-status ${league.draft_status}">${league.draft_status.replace('_', ' ')}</span>
    `;
    item.addEventListener('click', () => selectLeague(league));
    list.appendChild(item);
  });
}

async function selectLeague(league) {
  state.selectedLeague = league;
  state.selectedDraftId = league.draft_id;
  state.userId = league.user_id;

  showScreen('draft');
  $('header-league').textContent = league.league_name;

  await loadDraft();
  startPolling();
}

// ── Draft Screen ──────────────────────────────────────────────────────────────
$('btn-change-league').addEventListener('click', () => {
  stopPolling();
  state.recommendation = null;
  showScreen('setup');
});

$('btn-recommend').addEventListener('click', getRecommendation);

$('btn-refresh').addEventListener('click', async () => {
  await loadDraft();
});

// Tabs
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    const tabName = tab.dataset.tab;
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById(`tab-${tabName}`).classList.add('active');
  });
});

async function loadDraft() {
  const { selectedDraftId, selectedLeague, userId } = state;
  if (!selectedDraftId) return;

  try {
    const res = await fetch(
      `/api/draft/${selectedDraftId}?league_id=${selectedLeague.league_id}&user_id=${userId}`
    );
    const data = await res.json();
    if (data.error) throw new Error(data.error);

    state.draftData = data;
    renderDraftState(data);
  } catch (err) {
    console.error('Draft load error:', err);
  }
}

function renderDraftState(data) {
  const { picks, current_pick, league_context } = data;

  // Stats
  const roundNum = Math.ceil(current_pick / (league_context.num_teams || 12));
  const slotNum = current_pick - (roundNum - 1) * (league_context.num_teams || 12);
  $('stat-pick').textContent = formatPickNum(roundNum, slotNum);
  $('stat-mine').textContent = league_context.picks_made_by_me ?? 0;
  $('stat-taxi').textContent = league_context.taxi_slots_total != null
    ? `${(league_context.taxi_slots_total - league_context.taxi_slots_used) ?? 0}`
    : '—';
  $('stat-left').textContent = league_context.picks_remaining_for_me ?? '—';

  // Picks feed — descending order (most recent first)
  renderPicksFeed(picks, current_pick, league_context.num_teams || 12);
}

function renderPicksFeed(picks, currentPick, numTeams) {
  const feed = $('picks-feed');
  feed.innerHTML = '';

  // Add "on clock" card first
  const clockRound = Math.ceil(currentPick / numTeams);
  const clockSlot = currentPick - (clockRound - 1) * numTeams;
  const clockCard = document.createElement('div');
  clockCard.className = 'pick-card on-clock';
  clockCard.innerHTML = `
    <div class="pick-num">${formatPickNum(clockRound, clockSlot)} · on clock</div>
    <div class="pick-name empty">Your pick</div>
    <div class="pick-pos"></div>
  `;
  feed.appendChild(clockCard);

  // Picks in reverse order (most recent first)
  const reversed = [...picks].reverse();
  reversed.forEach(pick => {
    const card = document.createElement('div');
    const isMine = pick.is_mine;
    card.className = `pick-card${isMine ? ' mine' : ''}`;

    const round = pick.round || Math.ceil(pick.pick_no / numTeams);
    const slot = pick.round_slot || (pick.pick_no - (round - 1) * numTeams);
    const name = pick.player_name
      ? (pick.player_name.split(' ').length > 1
          ? pick.player_name.split(' ').slice(-1)[0]
          : pick.player_name)
      : '—';

    card.innerHTML = `
      <div class="pick-num">${formatPickNum(round, slot)}</div>
      <div class="pick-name">${name}</div>
      <div class="pick-pos">
        ${pick.position ? `<span class="pick-pos-badge">${pick.position}</span>` : ''}
        ${pick.team || ''}
      </div>
    `;
    feed.appendChild(card);
  });
}

async function getRecommendation() {
  const { selectedDraftId, selectedLeague, userId } = state;
  if (!selectedDraftId) return;

  // Loading state
  $('btn-recommend').disabled = true;
  $('btn-recommend-text').textContent = 'Thinking...';
  show('btn-recommend-spinner');

  hide('rec-empty');
  hide('rec-content');

  try {
    // Refresh draft first
    await loadDraft();

    const res = await fetch('/api/recommend', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        draft_id: selectedDraftId,
        league_id: selectedLeague.league_id,
        user_id: userId,
      }),
    });

    const rec = await res.json();
    if (rec.error) throw new Error(rec.error);

    state.recommendation = rec;
    renderRecommendation(rec);
  } catch (err) {
    $('rec-empty').textContent = `Error: ${err.message}`;
    show('rec-empty');
  } finally {
    $('btn-recommend').disabled = false;
    $('btn-recommend-text').textContent = 'Get Recommendation';
    hide('btn-recommend-spinner');
  }
}

function renderRecommendation(rec) {
  // Player name + meta
  $('rec-player').textContent = rec.recommendation || '—';

  const metaEl = $('rec-meta');
  metaEl.innerHTML = '';
  if (rec.position) {
    const badge = document.createElement('span');
    badge.className = 'pos-badge';
    badge.textContent = rec.position;
    metaEl.appendChild(badge);
  }

  // Find player team from draft data
  const draftData = state.draftData;
  if (draftData) {
    const playerInfo = getPlayerTeamFromAvailable(rec.recommendation, draftData);
    if (playerInfo) {
      const team = document.createElement('span');
      team.className = 'rec-team';
      team.textContent = playerInfo;
      metaEl.appendChild(team);
    }
  }

  // Confidence
  const tier = rec.confidence_tier || 'low';
  const gap = rec.confidence_gap || 0;
  $('conf-fill').style.width = confidenceWidth(tier);
  $('conf-label').textContent = confidenceText(tier, gap);

  // Reasoning
  $('rec-reasoning').textContent = rec.reasoning || '';

  // Alternatives tab
  renderAlternatives(rec.alternatives || []);

  // Roster tab
  renderRoster();

  // Notes tab
  renderNotes(rec);

  show('rec-content');

  // Activate alternatives tab by default
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  document.querySelector('[data-tab="alternatives"]').classList.add('active');
  $('tab-alternatives').classList.add('active');
}

function getPlayerTeamFromAvailable(name, draftData) {
  // Look up from league context my_picks or existing roster
  // Fall back to a simple team lookup if available
  return null; // Will enhance with player data later
}

function renderAlternatives(alts) {
  const list = $('alts-list');
  list.innerHTML = '';

  if (!alts.length) {
    list.innerHTML = '<p style="color:#aaa;font-size:13px;padding:8px 0">No alternatives available.</p>';
    return;
  }

  alts.forEach(alt => {
    const item = document.createElement('div');
    item.className = 'alt-item';
    item.innerHTML = `
      <div class="alt-top">
        <span class="alt-name">${alt.name}</span>
        ${alt.position ? `<span class="alt-pos">${alt.position}</span>` : ''}
      </div>
      <div class="alt-reason">${alt.reason || ''}</div>
    `;
    list.appendChild(item);
  });
}

function renderRoster() {
  const content = $('roster-content');
  content.innerHTML = '';

  const lc = state.draftData?.league_context;
  if (!lc) return;

  const allPicks = [
    ...(lc.my_existing_roster || []),
    ...(lc.my_picks_this_draft || []),
  ];

  if (!allPicks.length) {
    content.innerHTML = '<p style="color:#aaa;font-size:13px;padding:8px 0">No players drafted yet.</p>';
    return;
  }

  const byPos = {};
  allPicks.forEach(p => {
    const pos = p.position || '?';
    if (!byPos[pos]) byPos[pos] = [];
    byPos[pos].push(p);
  });

  const posOrder = ['QB', 'RB', 'WR', 'TE'];
  posOrder.forEach(pos => {
    if (!byPos[pos]) return;
    const group = document.createElement('div');
    group.className = 'roster-pos-group';
    group.innerHTML = `<div class="roster-pos-head">${pos}</div>`;

    byPos[pos].forEach(p => {
      const isStarter = lc.my_starters?.some(s => s.name === p.name);
      const row = document.createElement('div');
      row.className = 'roster-player-row';
      const isDynasty = lc.is_dynasty;
      const valStr = isDynasty
        ? `D:${p.dynasty_value || 0}`
        : `R:${p.redraft_value || 0}`;
      row.innerHTML = `
        <div class="roster-player-name ${isStarter ? 'starter' : ''}">
          ${isStarter ? '<span class="starter-dot"></span>' : ''}${p.name}
        </div>
        <div class="roster-player-vals">${valStr}</div>
      `;
      group.appendChild(row);
    });

    content.appendChild(group);
  });
}

function renderNotes(rec) {
  const content = $('notes-content');
  content.innerHTML = '';

  // Positional note
  if (rec.positional_note) {
    const item = document.createElement('div');
    item.className = 'note-item';
    item.innerHTML = `
      <div class="note-label">Positional note</div>
      <div class="note-text">${rec.positional_note}</div>
    `;
    content.appendChild(item);
  }

  // Upside
  if (rec.upside) {
    const item = document.createElement('div');
    item.className = 'note-item';
    item.innerHTML = `
      <div class="note-label">Upside</div>
      <div class="note-text">${rec.upside}</div>
    `;
    content.appendChild(item);
  }

  // Roster needs
  const lc = state.draftData?.league_context;
  if (lc) {
    const needsItem = document.createElement('div');
    needsItem.className = 'note-item';
    needsItem.innerHTML = '<div class="note-label">Roster Needs</div>';

    const grid = document.createElement('div');
    grid.className = 'needs-grid';

    const dedicated = lc.roster_construction_detail || {};
    const backup = lc.backup_needs || {};
    const picks = [...(lc.my_existing_roster || []), ...(lc.my_picks_this_draft || [])];

    ['QB', 'RB', 'WR', 'TE'].forEach(pos => {
      const d = dedicated[pos]?.dedicated_slots || 0;
      const b = backup[pos] || 0;
      const total = d + b;
      const have = picks.filter(p => p.position === pos).length;
      const remaining = Math.max(0, total - have);

      const needEl = document.createElement('div');
      needEl.className = 'need-item';
      needEl.innerHTML = `
        <div class="need-pos">${pos}</div>
        <div class="need-val ${remaining === 0 ? 'filled' : 'needed'}">${have}/${total} ${remaining === 0 ? '✓' : `(${remaining} needed)`}</div>
      `;
      grid.appendChild(needEl);
    });

    needsItem.appendChild(grid);
    content.appendChild(needsItem);
  }
}

// ── Polling ───────────────────────────────────────────────────────────────────
function startPolling() {
  stopPolling();
  state.polling = setInterval(async () => {
    if (state.draftData) {
      const prevPick = state.draftData.current_pick;
      await loadDraft();
      // If pick advanced, clear recommendation
      if (state.draftData.current_pick !== prevPick) {
        state.recommendation = null;
        hide('rec-content');
        show('rec-empty');
        $('rec-empty').textContent = 'New pick detected. Tap Get Recommendation.';
      }
    }
  }, 15000); // poll every 15 seconds
}

function stopPolling() {
  if (state.polling) {
    clearInterval(state.polling);
    state.polling = null;
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────
// Pre-fill username if known
const savedUsername = localStorage.getItem('da_username');
if (savedUsername) {
  $('input-username').value = savedUsername;
}

$('btn-load-leagues').addEventListener('click', () => {
  localStorage.setItem('da_username', $('input-username').value.trim());
});
