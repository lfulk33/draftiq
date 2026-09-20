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
  mode: null, // 'draft' | 'waiver'
  checkedLeagueIds: new Set(),
};

// ── Helpers ──────────────────────────────────────────────────────────────────
// SSO gateway now sits in front of this app. A fetch() never gets an HTML
// login page on 401 (nginx forces the JSON branch for /api/ requests), so we
// have to catch the status ourselves and navigate — otherwise a session
// expiring mid-draft just surfaces as a generic "unauthenticated" error with
// no way forward. Returns the raw response so existing call sites can keep
// their own res.json()/res.ok handling unchanged.
function ssoFetch(url, opts) {
  return fetch(url, opts).then(res => {
    if (res.status === 401) {
      location.href = 'https://apps.paragoncommerce.co/login?next=' +
                      encodeURIComponent(location.href);
      return new Promise(() => {}); // never resolves; we are navigating away
    }
    if (res.status === 403) {
      location.href = 'https://apps.paragoncommerce.co/denied?app=draftiq';
      return new Promise(() => {});
    }
    return res;
  });
}

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
function showModeChoice() {
  const username = $('input-username').value.trim();
  if (!username) return;
  localStorage.setItem('da_username', username);
  hide('setup-error');
  hide('league-list');
  hide('league-checklist-wrap');
  show('mode-choice');
}

$('btn-continue').addEventListener('click', showModeChoice);
$('input-username').addEventListener('keydown', e => {
  if (e.key === 'Enter') showModeChoice();
});

// Config for the checkbox-league-list report modes (everything except
// 'draft', which uses the click-to-select single-league flow instead).
const REPORT_MODES = {
  waiver: {
    screen: 'waiver',
    endpoint: '/api/waiver-report',
    subId: 'waivers-sub',
    contentId: 'waivers-content',
    loadingText: 'Scouting waivers across selected leagues — running live web research per league, this can take a minute or two…',
  },
  chopped: {
    screen: 'chopped',
    endpoint: '/api/chopped-bid-report',
    subId: 'chopped-sub',
    contentId: 'chopped-content',
    loadingText: "Building this week's bid strategy across selected leagues — running live web research per league, this can take a minute or two…",
  },
};

const MODE_BUTTON_IDS = { draft: 'btn-mode-draft', waiver: 'btn-mode-waiver', chopped: 'btn-mode-chopped' };

$('btn-mode-draft').addEventListener('click', () => enterMode('draft'));
$('btn-mode-waiver').addEventListener('click', () => enterMode('waiver'));
$('btn-mode-chopped').addEventListener('click', () => enterMode('chopped'));

async function enterMode(mode) {
  state.mode = mode;
  const btn = $(MODE_BUTTON_IDS[mode]);
  const originalHtml = btn.innerHTML;
  btn.innerHTML = '<span class="btn-mode-title">Loading leagues...</span>';
  hide('setup-error');

  try {
    await loadLeagues();
    hide('mode-choice');
    if (mode === 'draft') {
      renderLeagueList(state.leagues);
      show('league-list');
      hide('league-checklist-wrap');
    } else {
      state.checkedLeagueIds = new Set();
      renderLeagueChecklist(state.leagues);
      show('league-checklist-wrap');
      hide('league-list');
    }
  } catch (err) {
    $('setup-error').textContent = err.message || 'Failed to load leagues.';
    show('setup-error');
  } finally {
    btn.innerHTML = originalHtml;
  }
}

async function loadLeagues() {
  const username = $('input-username').value.trim();
  if (!username) return;

  const res = await ssoFetch(`/api/leagues?username=${encodeURIComponent(username)}`);
  const data = await res.json();
  if (data.error) throw new Error(data.error);

  state.username = username;
  state.userId = data[0]?.user_id || '';
  state.leagues = data;
}

function renderLeagueChecklist(leagues) {
  const wrap = $('league-checklist');
  wrap.innerHTML = '';

  const sorted = [...leagues].sort((a, b) => a.league_name.localeCompare(b.league_name));
  sorted.forEach(league => {
    const item = document.createElement('label');
    item.className = 'league-checkbox-item';
    item.innerHTML = `
      <input type="checkbox" data-league-id="${league.league_id}">
      <span class="league-name">${league.league_name}</span>
    `;
    const checkbox = item.querySelector('input');
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) state.checkedLeagueIds.add(league.league_id);
      else state.checkedLeagueIds.delete(league.league_id);
      $('btn-run-waiver-report').disabled = state.checkedLeagueIds.size === 0;
    });
    wrap.appendChild(item);
  });

  $('btn-run-waiver-report').disabled = true;
}

$('btn-run-waiver-report').addEventListener('click', () => runReport(state.mode));

async function runReport(mode) {
  const cfg = REPORT_MODES[mode];
  const leagueIds = [...state.checkedLeagueIds];
  if (!cfg || !leagueIds.length) return;

  showScreen(cfg.screen);
  const sub = $(cfg.subId);
  const content = $(cfg.contentId);
  sub.textContent = 'Running…';
  content.innerHTML = `<div class="waivers-loading-note">${cfg.loadingText}</div>`;

  try {
    const params = new URLSearchParams({
      username: state.username,
      league_ids: leagueIds.join(','),
    });
    const res = await ssoFetch(`${cfg.endpoint}?${params}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Failed to generate report.');

    sub.textContent = 'Generated ' + new Date(data.generated_at).toLocaleString();
    renderReportCards(data.reports || [], cfg.contentId);
  } catch (err) {
    content.innerHTML = `<div class="waivers-empty-note">Error: ${err.message}</div>`;
  }
}

function renderReportCards(reports, contentId) {
  const content = $(contentId);
  content.innerHTML = '';
  if (!reports.length) {
    content.innerHTML = '<div class="waivers-empty-note">No leagues returned.</div>';
    return;
  }
  reports.forEach(r => {
    const card = document.createElement('div');
    card.className = 'waiver-league-card';
    if (r.error) {
      card.innerHTML = `<h2>${r.league_name}</h2><div class="error-body">${r.error}</div>`;
      content.appendChild(card);
      return;
    }
    let budgetHtml = '';
    if (r.budget) {
      const b = r.budget;
      budgetHtml = `<div class="budget-line">Budget: $${b.remaining_budget} left of $${b.total_season_budget} · ~$${b.even_pace_baseline_per_week}/wk to make it through week ${b.last_elimination_week}</div>`;
    }
    card.innerHTML = `<h2>${r.league_name}</h2>${budgetHtml}<div class="algo-flags-slot"></div><div class="report-body"></div><div class="available-pool-slot"></div>`;
    const algoFlags = buildAlgoFlagsEl(r.algorithmic);
    if (algoFlags) card.querySelector('.algo-flags-slot').appendChild(algoFlags);
    card.querySelector('.report-body').textContent = r.report;
    const availablePool = buildAvailablePoolEl(r.available);
    if (availablePool) card.querySelector('.available-pool-slot').appendChild(availablePool);
    content.appendChild(card);
  });
}

function buildAlgoFlagsEl(algorithmic) {
  const gaps = (algorithmic && algorithmic.value_gaps) || [];
  const mismatches = (algorithmic && algorithmic.depth_chart_mismatches) || [];
  if (!gaps.length && !mismatches.length) return null;

  const wrap = document.createElement('div');
  wrap.className = 'algo-flags';
  const label = document.createElement('div');
  label.className = 'algo-flags-label';
  label.textContent = 'Algorithmic flags (free, no Claude)';
  wrap.appendChild(label);

  const addLine = (tag, text) => {
    const line = document.createElement('div');
    line.className = 'algo-flag-line';
    const tagEl = document.createElement('span');
    tagEl.className = 'algo-tag';
    tagEl.textContent = tag;
    line.appendChild(tagEl);
    line.appendChild(document.createTextNode(text));
    wrap.appendChild(line);
  };

  gaps.forEach(f => addLine(
    f.criterion.replace(/_/g, ' '),
    `${f.position} — add ${f.add} (${f.add_team}), drop ${f.drop} — ${f.reason}`
  ));
  mismatches.forEach(f => addLine(
    'depth chart',
    `${f.position} — ${f.add} (${f.add_team}) — ${f.reason}`
  ));

  return wrap;
}

function buildAvailablePoolEl(available) {
  if (!available || !Object.values(available).some(list => list && list.length)) return null;

  const details = document.createElement('details');
  details.className = 'available-pool';
  const summary = document.createElement('summary');
  summary.textContent = 'Available players by position';
  details.appendChild(summary);

  Object.entries(available).forEach(([pos, plist]) => {
    if (!plist || !plist.length) return;
    const label = document.createElement('div');
    label.className = 'available-pos-label';
    label.textContent = pos;
    details.appendChild(label);
    plist.forEach(p => {
      const line = document.createElement('div');
      line.className = 'available-player-line';
      const injuryPart = p.injury_status ? `, ${p.injury_status}` : '';
      line.textContent = `${p.name} (${p.team}) — search_rank ${p.search_rank}${injuryPart}`;
      details.appendChild(line);
    });
  });

  return details;
}

function backToSetup() {
  state.mode = null;
  showScreen('setup');
  hide('mode-choice');
  hide('league-list');
  hide('league-checklist-wrap');
}

$('btn-waiver-back').addEventListener('click', backToSetup);
$('btn-chopped-back').addEventListener('click', backToSetup);

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
  state.mode = null;
  showScreen('setup');
  hide('mode-choice');
  hide('league-list');
  hide('league-checklist-wrap');
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
function activateTab(tabName) {
  document.querySelectorAll('.tab').forEach(t => {
    t.classList.toggle('active', t.dataset.tab === tabName);
  });
  document.querySelectorAll('.tab-content').forEach(c => {
    const isTarget = c.id === `tab-${tabName}`;
    c.classList.toggle('active', isTarget);
    c.classList.toggle('hidden', !isTarget);
  });
}

document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => activateTab(tab.dataset.tab));
});

async function loadDraft(disableButton = true) {
  const { selectedDraftId, selectedLeague, userId } = state;
  if (!selectedDraftId) return;

  if (disableButton) $('btn-recommend').disabled = true;
  try {
    const res = await ssoFetch(
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

  // Keeper slots are pre-filled by Sleeper into later rounds the moment the
  // draft goes live, well before the draft actually reaches those rounds.
  // Hide them until we've actually gotten there so the board only shows
  // picks prior to the current one, not a wall of future keeper reveals.
  const relevantPicks = picks.filter(pick => {
    if (!pick.is_keeper) return true;
    const round = pick.round || Math.ceil(pick.pick_no / numTeams);
    return round <= clockRound;
  });

  // Picks in reverse chronological order (most recent first)
  const reversed = [...relevantPicks].reverse();
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
        ${pick.salary != null ? `<span class="salary-badge">$${pick.salary}</span>` : ''}
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

  const useClaude = $('toggle-use-claude').checked;

  $('btn-recommend').disabled = true;
  $('btn-recommend-text').textContent = useClaude ? 'Thinking...' : 'Calculating...';
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

    const res = await ssoFetch('/api/recommend', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        draft_id: selectedDraftId,
        league_id: selectedLeague.league_id,
        user_id: userId,
        use_claude: useClaude,
      }),
    });

    const rec = await res.json();
    console.log('raw rec:', JSON.stringify(rec, null, 2));
    if (rec.trace) console.error('Server traceback:', rec.trace);
    if (res.status === 409) {
      // Board changed during recommendation — refresh and retry once automatically
      await loadDraft();
      const retryRes = await ssoFetch('/api/recommend', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          draft_id: selectedDraftId,
          league_id: selectedLeague.league_id,
          user_id: userId,
          use_claude: useClaude,
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
  // Raw (no-Claude) mode has a completely different response shape —
  // alternatives_by_position instead of prose alternatives/reasoning.
  if (rec.alternatives_by_position) {
    renderRawRecommendation(rec);
    return;
  }

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

  // POC: salary-cap leagues only — rec.salary is only present when the
  // backend has salary_cap context for this league.
  if (rec.salary != null) {
    const salaryBadge = document.createElement('span');
    salaryBadge.className = 'salary-badge';
    salaryBadge.textContent = `$${rec.salary}`;
    metaEl.appendChild(salaryBadge);
  }

  // If the recommended player is also flagged as trade bait, badge the main
  // card instead of listing the same name again in Also Consider.
  // trade_bait isn't always an array in Claude's response — normalize first.
  const rawTradeBait = rec.trade_bait;
  const allTradeBait = Array.isArray(rawTradeBait) ? rawTradeBait : (rawTradeBait ? [rawTradeBait] : []);
  const selfTradeBait = allTradeBait.find(tb => tb && tb.name === rec.recommendation);
  const otherTradeBait = allTradeBait.filter(tb => tb !== selfTradeBait);
  if (selfTradeBait) {
    const badge = document.createElement('span');
    badge.className = 'trade-bait-badge';
    badge.textContent = selfTradeBait.type === 'redraft' ? 'TRADE BAIT · REDRAFT' : 'TRADE BAIT · DYNASTY';
    metaEl.appendChild(badge);
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
  renderAlternatives(rec.alternatives || [], otherTradeBait);
  renderRoster();
  renderNotes(rec);

  show('rec-content');

  activateTab('alternatives');
}

function renderRawRecommendation(rec) {
  $('rec-player').textContent = rec.recommendation || '—';

  const metaEl = $('rec-meta');
  metaEl.innerHTML = '';
  if (rec.position) {
    const badge = document.createElement('span');
    badge.className = 'pos-badge';
    badge.textContent = rec.position;
    metaEl.appendChild(badge);
  }
  if (rec.recommended_details?.team) {
    const team = document.createElement('span');
    team.className = 'rec-team';
    team.textContent = rec.recommended_details.team;
    metaEl.appendChild(team);
  }

  // No Claude confidence tier in raw mode — hide that bar entirely.
  document.querySelector('.rec-conf').style.visibility = 'hidden';

  const reasoningEl = $('rec-reasoning');
  reasoningEl.innerHTML = '';
  const d = rec.recommended_details;
  if (d) {
    const grid = document.createElement('div');
    grid.className = 'raw-rec-numbers';
    const rows = [
      ['Value', d.value],
      ['VORP', d.vorp],
      ['Positional rank', `${d.position}${d.positional_rank}`],
      ['Replacement level', d.replacement_level],
      ['Real ADP', d.adp_formatted ? `pick ${d.adp_formatted} (${d.adp})` : 'no ADP data'],
      ['Opportunity cost (board risk)', d.opportunity_cost],
      ['Roster need', d.roster_need],
      ['Final score', d.score],
    ];
    if (rec.gap != null) rows.push(['Gap vs. best overall', rec.gap]);
    rows.forEach(([label, val]) => {
      const row = document.createElement('div');
      row.className = 'stat-row';
      row.innerHTML = `<span>${label}</span><span>${val}</span>`;
      grid.appendChild(row);
    });
    reasoningEl.appendChild(grid);
  }

  hide('rec-prompt');
  hide('new-pick-banner');
  document.querySelector('.rec-label').style.visibility = 'visible';
  $('rec-player').style.visibility = 'visible';
  $('rec-meta').style.visibility = 'visible';
  $('rec-reasoning').style.visibility = 'visible';

  renderRawAlternatives(rec.alternatives_by_position || {}, rec.recommendation);
  renderRoster();
  renderNotes({});

  show('rec-content');
  activateTab('alternatives');
}

function renderRawAlternatives(byPosition, pickedName) {
  const list = $('alts-list');
  list.innerHTML = '';
  ['QB', 'RB', 'WR', 'TE'].forEach(pos => {
    const entries = byPosition[pos];
    if (!entries || !entries.length) return;
    const group = document.createElement('div');
    group.className = 'raw-pos-group';
    group.innerHTML = `<div class="raw-pos-head">${pos}</div>`;
    entries.forEach(e => {
      const row = document.createElement('div');
      row.className = 'raw-alt-row' + (e.name === pickedName ? ' is-pick' : '') + (e.eligible_for_pick === false ? ' is-blocked' : '');
      row.innerHTML = `
        <div class="raw-alt-row-main">
          <span class="raw-alt-name">${e.positional_rank}. ${e.name}${e.team ? ` (${e.team})` : ''}${e.adp_formatted ? ` <span class="raw-alt-adp">ADP ${e.adp_formatted}</span>` : ''}</span>
          <span class="raw-alt-nums">score ${e.score} · VORP ${e.vorp} · opp ${e.opportunity_cost} · need ${e.roster_need} · val ${e.value}</span>
        </div>
        ${e.eligible_for_pick === false ? `<div class="raw-alt-blocked-note">Can't win the pick: ${e.not_eligible_reason}</div>` : ''}
      `;
      group.appendChild(row);
    });
    list.appendChild(group);
  });
  if (!list.children.length) {
    list.innerHTML = '<p class="alts-empty">No alternatives data.</p>';
  }
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
        ${tb.salary != null ? `<span class="salary-badge">$${tb.salary}</span>` : ''}
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
        ${alt.salary != null ? `<span class="salary-badge">$${alt.salary}</span>` : ''}
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

    // POC: salary-cap leagues only — lc.salary_cap is only present when
    // the backend has salary_cap context for this league.
    if (lc.salary_cap) {
      const sc = lc.salary_cap;
      const budgetItem = document.createElement('div');
      budgetItem.className = 'note-item';
      budgetItem.innerHTML = `
        <div class="note-label">Budget</div>
        <div class="needs-grid">
          <div class="need-item">
            <div class="need-pos">Spent</div>
            <div class="need-val">$${sc.total_spent} / $${sc.cap}</div>
          </div>
          <div class="need-item">
            <div class="need-pos">Remaining</div>
            <div class="need-val ${sc.remaining_budget <= 0 ? 'needed' : 'filled'}">$${sc.remaining_budget}</div>
          </div>
          <div class="need-item">
            <div class="need-pos">Slots left</div>
            <div class="need-val">${sc.remaining_slots}</div>
          </div>
          <div class="need-item">
            <div class="need-pos">Avg/slot</div>
            <div class="need-val">$${sc.avg_per_slot}</div>
          </div>
          <div class="need-item">
            <div class="need-pos">Max bid</div>
            <div class="need-val">$${sc.max_affordable}</div>
          </div>
        </div>
      `;
      content.appendChild(budgetItem);
    }
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
  ssoFetch('/api/default-username')
    .then(r => r.json())
    .then(d => { if (d.username) $('input-username').value = d.username; })
    .catch(() => { });
}