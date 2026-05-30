// ── State ────────────────────────────────────────────────────────────────────
const state = {
  username: '',
  userId: '',
  leagues: [],
  selectedLeague: null,
  selectedDraftId: null,
  draftData: null,
  recommendation: null,
  recommendationStale: false,
  draftFrozen: false,
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

  if (name === 'draft') {
    document.querySelector('.sticky-footer').classList.remove('hidden');
  } else {
    document.querySelector('.sticky-footer').classList.add('hidden');
  }
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
  state.draftFrozen = false;
  showScreen('setup');
});

$('btn-recommend').addEventListener('click', getRecommendation);

$('btn-refresh').addEventListener('click', async () => {
  const btn = $('btn-refresh');
  btn.disabled = true;
  btn.textContent = '...';
  await loadDraft();
  btn.textContent = '↺';
  btn.disabled = false;
});

// Tabs
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    const tabName = tab.dataset.tab;
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => {
      c.classList.remove('active');
      c.classList.add('hidden');
    });
    tab.classList.add('active');
    const target = document.getElementById(`tab-${tabName}`);
    target.classList.remove('hidden');
    target.classList.add('active');
  });
});

async function loadDraft(disableButton = true) {
  const { selectedDraftId, selectedLeague, userId } = state;
  if (!selectedDraftId) return;

  if (disableButton) $('btn-recommend').disabled = true;
  try {
    const res = await fetch(
      `/api/draft/${selectedDraftId}?league_id=${selectedLeague.league_id}&user_id=${userId}`
    );
    const data = await res.json();
    if (data.error) throw new Error(data.error);

    state.draftData = data;

    if (state.draftFrozen) {
      // Draft frozen — only update picks feed, not roster or rec card
      renderPicksFeed(data.picks, data.current_pick, data.league_context?.num_teams || 12);
      return;
    }

    renderDraftState(data);

    // Freeze roster and recommend button when no picks remaining
    if ((data.league_context?.picks_remaining_for_me ?? 1) <= 0) {
      state.draftFrozen = true;
      $('btn-recommend').disabled = true;
      $('btn-recommend-text').textContent = 'Draft Complete';
      // Keep polling so picks feed continues updating
    }
  } catch (err) {
    console.error('Draft load error:', err);
  } finally {
    if (disableButton) $('btn-recommend').disabled = false;
  }
}

function renderDraftState(data) {
  const { picks, current_pick, league_context } = data;
  if (data.my_draft_slot) state.myDraftSlot = data.my_draft_slot;

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

  // Always render roster and notes so columns are populated before recommendation
  renderRoster();
  if (!state.recommendation) {
    renderAlternatives([]);
    renderNotes({});
    hide('rec-empty');
    show('rec-content');
    show('rec-prompt');
    document.querySelector('.rec-label').style.visibility = 'hidden';
    $('rec-player').style.visibility = 'hidden';
    $('rec-meta').style.visibility = 'hidden';
    document.querySelector('.rec-conf').style.visibility = 'hidden';
    $('rec-reasoning').style.visibility = 'hidden';
  }
}

function renderPicksFeed(picks, currentPick, numTeams) {
  const feed = $('picks-feed');
  feed.innerHTML = '';

  // On-clock card first
  const clockRound = Math.ceil(currentPick / numTeams);
  const clockSlot = state.myDraftSlot || (currentPick - (clockRound - 1) * numTeams);
  const clockCard = document.createElement('div');
  clockCard.className = 'pick-card on-clock';
  clockCard.innerHTML = `
    <div class="pick-num">${formatPickNum(clockRound, clockSlot)}</div>
    <div class="pick-name empty">Your pick</div>
    <div class="pick-pos"></div>
  `;
  // Arrow pointing right, to the left of on-clock card
  const arrowEl = document.createElement('div');
  arrowEl.className = 'pick-arrow-indicator';
  arrowEl.innerHTML = `
    <svg width="80" height="48" viewBox="0 0 80 48" fill="none" xmlns="http://www.w3.org/2000/svg">
      <line x1="10" y1="24" x2="70" y2="24" stroke="#8b3a0f" stroke-width="5" stroke-linecap="round"/>
      <polyline points="50,8 70,24 50,40" stroke="#8b3a0f" stroke-width="5" fill="none" stroke-linejoin="round" stroke-linecap="round"/>
    </svg>
  `;
  feed.appendChild(arrowEl);
  feed.appendChild(clockCard);

  // Picks in reverse chronological order (most recent first)
  const reversed = [...picks].reverse();
  reversed.forEach(pick => {
    const card = document.createElement('div');
    const isMine = pick.is_mine;
    card.className = `pick-card${isMine ? ' mine' : ''}`;

    const round = pick.round || Math.ceil(pick.pick_no / numTeams);
    const slot = pick.round_slot || (pick.pick_no - (round - 1) * numTeams);
    let nameHtml = '—';
    if (pick.player_name) {
      const parts = pick.player_name.split(' ');
      if (parts.length > 1) {
        const first = parts.slice(0, -1).join(' ');
        const last = parts.slice(-1)[0];
        nameHtml = `<span class="pick-first">${first}</span><span class="pick-last">${last.toUpperCase()}</span>`;
      } else {
        nameHtml = `<span class="pick-last">${pick.player_name.toUpperCase()}</span>`;
      }
    }

    card.innerHTML = `
      <div class="pick-num">${formatPickNum(round, slot)}</div>
      <div class="pick-name">${nameHtml}</div>
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
  state.recommendation = null;
  state.recommendationStale = false;

  $('btn-recommend').disabled = true;
  $('btn-recommend-text').textContent = 'Thinking...';
  show('btn-recommend-spinner');

  hide('rec-empty');
  hide('rec-content');

  try {
    // Fetch fresh draft state, wait for Sleeper to propagate, then fetch again.
    // Only proceed when pick count is stable across two fetches.
    await loadDraft(false);
    const pickCountBefore = state.draftData?.current_pick;
    await new Promise(resolve => setTimeout(resolve, 2000));
    await loadDraft();
    const pickCountAfter = state.draftData?.current_pick;
    if (pickCountAfter !== pickCountBefore) {
      // Pick count changed during our wait — retry automatically with fresh data
      await new Promise(resolve => setTimeout(resolve, 1000));
      await loadDraft(false);
    }

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
    console.log('raw rec:', JSON.stringify(rec, null, 2));
    if (rec.trace) console.error('Server traceback:', rec.trace);
    if (res.status === 409) {
      // Board changed during recommendation — refresh and retry once automatically
      await loadDraft();
      const retryRes = await fetch('/api/recommend', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          draft_id: selectedDraftId,
          league_id: selectedLeague.league_id,
          user_id: userId,
        }),
      });
      const retryRec = await retryRes.json();
      if (retryRec.error) throw new Error(retryRec.error);
      state.recommendation = retryRec;
      renderRecommendation(retryRec);
      $('btn-recommend').disabled = false;
      $('btn-recommend-text').textContent = 'Get Recommendation';
      hide('btn-recommend-spinner');
      return;
    }
    if (rec.error) throw new Error(rec.error);
    console.log('rec:', JSON.stringify(rec, null, 2));

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
  $('rec-player').textContent = rec.recommendation || '—';

  const metaEl = $('rec-meta');
  metaEl.innerHTML = '';
  if (rec.position) {
    const badge = document.createElement('span');
    badge.className = 'pos-badge';
    badge.textContent = rec.position;
    metaEl.appendChild(badge);
  }

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

  const tier = rec.confidence_tier || 'low';
  const gap = rec.confidence_gap || 0;
  $('conf-fill').style.width = confidenceWidth(tier);
  $('conf-label').textContent = confidenceText(tier, gap);

  $('rec-reasoning').textContent = rec.reasoning || '';
  hide('rec-prompt');
  hide('new-pick-banner');
  document.querySelector('.rec-label').style.visibility = 'visible';
  $('rec-player').style.visibility = 'visible';
  $('rec-meta').style.visibility = 'visible';
  document.querySelector('.rec-conf').style.visibility = 'visible';
  $('rec-reasoning').style.visibility = 'visible';
  renderAlternatives(rec.alternatives || [], rec.trade_bait || []);
  renderRoster();
  renderNotes(rec);

  show('rec-content');

  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(c => {
    c.classList.remove('active');
    c.classList.add('hidden');
  });
  document.querySelector('[data-tab="alternatives"]').classList.add('active');
  $('tab-alternatives').classList.remove('hidden');
  $('tab-alternatives').classList.add('active');
}

function getPlayerTeamFromAvailable(name, draftData) {
  return state.recommendation?.team || null;
}

function renderAlternatives(alts, tradeBait) {
  const list = $('alts-list');
  list.innerHTML = '';

  if (!alts.length && !tradeBait) {
    list.innerHTML = '<p class="alts-empty">Tap Get Recommendation to see suggestions for this pick.</p>';
    return;
  }

  // Trade bait cards — always first if present
  const tradeBaitArr = Array.isArray(tradeBait) ? tradeBait : (tradeBait ? [tradeBait] : []);
  tradeBaitArr.forEach(tb => {
    if (!tb || !tb.name) return;
    const item = document.createElement('div');
    item.className = 'alt-item trade-bait-item';
    const badgeLabel = tb.type === 'redraft' ? 'TRADE BAIT · REDRAFT' : 'TRADE BAIT · DYNASTY';
    item.innerHTML = `
      <div class="alt-top">
        <span class="alt-name">${tb.name}</span>
        ${tb.position ? `<span class="alt-pos">${tb.position}</span>` : ''}
        <span class="trade-bait-badge">${badgeLabel}</span>
      </div>
      <div class="alt-reason">${tb.reason || 'Best available at a full position — worth drafting to trade for a positional need.'}</div>
    `;
    list.appendChild(item);
  });

  const tradeBaitNames = new Set(tradeBaitArr.map(tb => tb.name));
  alts.forEach(alt => {
    if (tradeBaitNames.has(alt.name)) return;
    const item = document.createElement('div');
    item.className = 'alt-item';
    item.innerHTML = `
      <div class="alt-top">
        <span class="alt-name">${alt.name}</span>
        ${alt.position ? `<span class="alt-pos">${alt.position}</span>` : ''}
        ${alt.team ? `<span class="alt-team">${alt.team}</span>` : ''}
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

  const seen = new Set();
  const allPicks = [
    ...(lc.my_existing_roster || []),
    ...(lc.my_picks_this_draft || []),
  ].filter(p => {
    if (!p.name || seen.has(p.name)) return false;
    seen.add(p.name);
    return true;
  });

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
    // Sort by taxi last, then by name alphabetically within active
    byPos[pos].sort((a, b) => {
      // Taxi always last
      const aTaxi = lc.my_taxi_players?.includes(a.name) ? 1 : 0;
      const bTaxi = lc.my_taxi_players?.includes(b.name) ? 1 : 0;
      if (aTaxi !== bTaxi) return aTaxi - bTaxi;
      // Starters before bench
      const aStarter = lc.my_starters?.some(s => s.name === a.name) ? 0 : 1;
      const bStarter = lc.my_starters?.some(s => s.name === b.name) ? 0 : 1;
      if (aStarter !== bStarter) return aStarter - bStarter;
      // Within each tier, sort by redraft value descending
      const aVal = a.redraft_value || 0;
      const bVal = b.redraft_value || 0;
      return bVal - aVal;
    });
    const group = document.createElement('div');
    group.className = 'roster-pos-group';
    group.innerHTML = `<div class="roster-pos-head">${pos}</div>`;

    byPos[pos].forEach(p => {
      const isStarter = lc.my_starters?.some(s => s.name === p.name);
      const isTaxi = lc.my_taxi_players?.includes(p.name);
      const row = document.createElement('div');
      row.className = 'roster-player-row';
      row.innerHTML = `
        <div class="roster-player-name ${isStarter ? 'starter' : ''}">
          ${isStarter ? '<span class="starter-dot"></span>' : ''}${p.name}${isTaxi ? ' <span class="taxi-badge">TAXI</span>' : ''}
        </div>
      `;
      group.appendChild(row);
    });

    content.appendChild(group);
  });
}

function renderNotes(rec) {
  const content = $('notes-content');
  content.innerHTML = '';

  if (rec.positional_note) {
    const item = document.createElement('div');
    item.className = 'note-item';
    item.innerHTML = `
      <div class="note-label">Positional note</div>
      <div class="note-text">${rec.positional_note}</div>
    `;
    content.appendChild(item);
  }

  if (rec.upside) {
    const item = document.createElement('div');
    item.className = 'note-item';
    item.innerHTML = `
      <div class="note-label">Upside</div>
      <div class="note-text">${rec.upside}</div>
    `;
    content.appendChild(item);
  }

  const lc = state.draftData?.league_context;
  if (lc) {
    const needsItem = document.createElement('div');
    needsItem.className = 'note-item';
    needsItem.innerHTML = '<div class="note-label">Roster Needs</div><div class="needs-note">* includes developmental taxi stashes</div>';

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
      if (state.draftData.current_pick !== prevPick) {
        // Don't clear the rec card — user may still be reading it.
        // Just show a banner and let them tap Get Recommendation when ready.
        // Don't null out recommendation yet — user may still be reading.
        // Mark it as stale instead so Get Recommendation knows to refresh.
        state.recommendationStale = true;
        renderRoster();
        show('new-pick-banner');

        // Disable recommend button briefly to let Sleeper propagate
        $('btn-recommend').disabled = true;
        $('btn-recommend-text').textContent = 'Updating...';
        setTimeout(() => {
          $('btn-recommend').disabled = false;
          $('btn-recommend-text').textContent = 'Get Recommendation';
        }, 3000);
      }
    }
  }, 5000);
}

function stopPolling() {
  if (state.polling) {
    clearInterval(state.polling);
    state.polling = null;
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────
const savedUsername = localStorage.getItem('da_username');
if (savedUsername) {
  $('input-username').value = savedUsername;
} else {
  fetch('/api/default-username')
    .then(r => r.json())
    .then(d => { if (d.username) $('input-username').value = d.username; })
    .catch(() => { });
}

$('btn-load-leagues').addEventListener('click', () => {
  localStorage.setItem('da_username', $('input-username').value.trim());
});