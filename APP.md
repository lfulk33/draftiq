# APP.md — draft-assistant

## What it is

A Flask app that gives live, VORP-based pick recommendations during Sleeper
fantasy football drafts (dynasty, redraft, best ball, auction), plus
Claude-written waiver-wire and salary-cap-bid reports across all of a user's
Sleeper leagues. Used live, pick-by-pick, during real drafts. If it
disappeared, the user loses in-draft recommendation support and the daily
waiver report — annoying mid-draft, not catastrophic, and nothing it holds is
unique (see Data below).

## URLs

| URL | What it serves |
|---|---|
| `draftiq.paragoncommerce.co` | The whole app — static frontend + `/api/*` |

App key in the SSO registry: `draftiq` · Who is granted it: not verified from
this repo — ask the parent/SSO registry, this app doesn't know its own grants.

## Shape

Server-rendered Flask (`server.py`) serving a static JS/HTML frontend
(`static/`, `templates/`) plus a JSON API under `/api/`.

- **Entry point:** `server.py` (`app = Flask(...)`), run under gunicorn as
  `server:app`.
- **Port:** `127.0.0.1:5000` (loopback; nginx proxies `draftiq.paragoncommerce.co`
  to it).
- **Generated files:** none checked into the repo. `reports/waiver_report_latest.json`
  is written by `daily_reports.py` and is pure cache — gitignored, safe to
  delete, rebuilds on next run.

## Deploying

    ./deploy/push

Run from the laptop. Pushes to `origin/main`, then over ssh: `git pull origin
main` in the box's plain checkout at `~/draft-assistant`, then
`sudo systemctl restart draft-assistant`. Finishes by curling
`https://draftiq.paragoncommerce.co/api/default-username` and requires
200/302/401 back — anything else fails the script. A 401 here is the *healthy*
answer (no session cookie was sent), so getting one means nginx, the SSO gate,
and this app are all up; a connection failure or 5xx means they are not.

**There is no CI deploy.** A `.github/workflows/deploy.yml` used to exist that
looked like it auto-deployed on push to main; its script was literally
`echo "deploy triggered"` — it SSHed in and did nothing. Removed rather than
fixed, because turning on real auto-deploy-on-push to a live service is a
decision for Larry, not something to wire up silently while cleaning up a lie.
Deploy is a manual, deliberate step until someone decides otherwise.

**How this was actually deployed until today:** an untracked `~/draft-assistant/deploy.sh`
sitting only on the box (`git pull origin main && sudo systemctl restart
draft-assistant`, no verification step). Pulled into the repo as `deploy/push`
above and deleted from the box so there's one copy instead of two that can
drift.

**The box's git remote pointed at the wrong URL until today.** It cloned from
`https://github.com/lfulk33/draft-assistant.git`; the repo was renamed to
`draftiq` at some point and GitHub's rename-redirect was silently carrying the
`git pull` through. That redirect is not permanent — it breaks the moment
anyone else claims the name `draft-assistant` on GitHub. Repointed to
`https://github.com/lfulk33/draftiq.git` (verified same commit history before
and after the switch).

## Data — the section the parent relies on

| Path | What it holds | Replaceable? |
|---|---|---|
| `players.json` (19MB) | Full Sleeper player DB | Yes — `sleeper_client.py`, cron-refreshed 03:00 daily |
| `fantasy_players.json` (6.5MB) | FantasyCalc-enriched player values | Yes — `fantasycalc_client.py`, cron-refreshed 03:00 daily |
| `season_stats_2023/24/25.json` | Real Sleeper season stats, for VORP calibration | Yes — `historical_stats.py:fetch_season_stats()` hits Sleeper's stats API directly. **Not on the cron** — refetches lazily when `historical_stats.py` runs and finds no cache, not on a schedule. |
| `adp_2qb_14_2026.json`, `adp_ppr_12_2026.json` | Real ADP from FantasyFootballCalculator | Yes — `adp_client.py`, cached, refetches when stale. Not on the cron either. |
| `beatadp_players.json` | Real Sleeper-platform ADP scraped from BeatADP | Yes — `beatadp_client.py`, same caching pattern. Fragile to BeatADP changing their page's internal JSON structure (documented in the file's own docstring), but that's a code-maintenance risk, not a backup gap. |
| `player_overrides.json` (86 bytes) | Hand-maintained "don't draft" / "backup only" list — real judgment calls (e.g. an injured player to exclude from VORP), not fetched from anywhere | **No, not fetchable — but already safe.** It's committed to git with real history and pushed to GitHub (`origin`), which is itself an off-box copy. Doesn't need the nightly box backup on top of that. |
| `.env` | `ANTHROPIC_API_KEY` | No, but already on the parent's backup per their message |

**Correcting an assumption from `SSO-APP-CHANGES.md`:** the 03:00 cron only
refetches `players.json` and `fantasy_players.json`. `season_stats_*`,
`adp_*.json`, and `beatadp_players.json` are genuinely regenerable too, just
lazily on next use rather than nightly — worth knowing if a restore ever
depends on cron timing rather than "the app ran once."

**Net answer to the parent's question:** nothing of this app's is both
irreplaceable and unbacked. The one hand-maintained file is already in git.

## Secrets

- `ANTHROPIC_API_KEY` — from `.env`, used for Claude-written waiver/bid
  reports (`waiver_scout.py`, `chopped_bid_advisor.py`). Does not expire on a
  schedule; regenerate at console.anthropic.com if it does.

No other secret. Sleeper's API needs no key. FantasyCalc/FantasyFootballCalculator/BeatADP
are public, unauthenticated endpoints.

## SSO

401/403 handling lives in `static/js/app.js`, in a shared `ssoFetch()` helper
wired into all six of the app's own `fetch()` calls (`/api/leagues`,
`/api/recommend` ×2, `/api/draft`, both report endpoints, `/api/default-username`):

    function ssoFetch(url, opts) {
      return fetch(url, opts).then(res => {
        if (res.status === 401) {
          location.href = 'https://apps.paragoncommerce.co/login?next=' +
                          encodeURIComponent(location.href);
          return new Promise(() => {});
        }
        if (res.status === 403) {
          location.href = 'https://apps.paragoncommerce.co/denied?app=draftiq';
          return new Promise(() => {});
        }
        return res;
      });
    }

No second route bypasses SSO — this app is not on the tailnet, only
`draftiq.paragoncommerce.co`, so there's no hostname guard the way `budget`
needs one.

Does not read `X-Sso-User`. The app already asks for a Sleeper username on its
own screen (a different identity than the paragoncommerce.co account) — SSO
identity and Sleeper identity are deliberately not the same thing here.

`CORS(app)` (unrestricted, `flask_cors`) has been removed entirely rather than
scoped, since nothing needs cross-origin access — frontend and API are
same-origin.

## Gotchas

- **The box's checkout is a plain `git pull` clone, not the bare-repo +
  `post-receive` pattern the other apps on this box use.** If you go looking
  for `~/draft-assistant/repo.git`, it doesn't exist — there's just
  `~/draft-assistant/` as a normal working tree, updated by `deploy/push`
  sshing in and pulling directly.
- **Two GitHub remote names for one repo**: this repo was renamed
  `draft-assistant` → `draftiq` at some point. Any old clone, script, or
  bookmark using the old name still silently works today via GitHub's
  rename-redirect — until someone else registers that name, at which point it
  breaks with no warning. If something references `draft-assistant.git`
  anywhere, repoint it.
- The Flask dev server (`python3 server.py` locally) runs with
  `use_reloader=False` — code edits during local dev need a manual
  kill+restart, there's no autoreload.

## Verifying it actually works

    curl -s -o /dev/null -w '%{http_code}\n' https://draftiq.paragoncommerce.co/api/default-username

Signed out (no session cookie — e.g. `curl` from a machine that never logged
in, or `curl --cookie ""`), this should be **401** with a JSON body, never an
HTML login page — that's the SSO contract for `/api/` paths working correctly.
Getting a 200 here without a session would mean the SSO gate isn't actually in
front of this path.

Signed in, `https://draftiq.paragoncommerce.co/` should load the draft UI and
`/api/leagues?username=<sleeper-username>` should return real league data.

## Open items

- No auto-deploy. Manual `deploy/push` only, by design for now (see
  Deploying above) — revisit if that becomes annoying.
- App grants (who's allowed to sign in) aren't verified from this repo; ask
  the parent / check the SSO registry directly if that ever matters here.
- `historical_stats.py`, `adp_client.py`, `beatadp_client.py` caches aren't on
  any cron — they refresh lazily on next use. Fine for how this app is
  actually used (run before/during a draft), but means a long-idle box could
  serve slightly stale ADP/stats on the very next run until the cache
  refreshes.
