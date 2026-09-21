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

Run from the laptop. Matches the house pattern the other five apps on the box
use: a bare repo at `~/draft-assistant/repo.git`, a work tree checked out at
`~/draft-assistant/checkout`, and a `prod` git remote
(`paragon:/home/ec2-user/draft-assistant/repo.git`) already configured locally.
The script does, in order: push to `origin/main` (the off-box copy), push to
`prod main` (triggers the box's `post-receive` hook, which checks out, restarts
`draft-assistant.service`, and retries a local health check for a few seconds),
then — **client-side, not in the hook** — curls
`https://draftiq.paragoncommerce.co/api/default-username` and requires
200/302/401 back. A 401 here is the *healthy* answer (no session cookie was
sent).

The served-URL check runs in the script rather than the hook because **a
post-receive hook cannot fail a push** — it runs after git has already
accepted the refs, so `git push` returns 0 no matter what the hook prints. The
hook is the early warning (it prints `DEGRADED` if its own restart/health-check
fails); `deploy/push`'s own curl at the end is the actual gate, and is the part
that can make you notice.

**There is no CI deploy.** A `.github/workflows/deploy.yml` used to exist that
looked like it auto-deployed on push to main; its script was literally
`echo "deploy triggered"` — it SSHed in and did nothing. Removed rather than
fixed, because turning on real auto-deploy-on-push to a live service is a
decision for Larry, not something to wire up silently while cleaning up a lie.
Deploy is a manual, deliberate step until someone decides otherwise.

**History, for anyone reading old commits:** this used to be an untracked
`~/draft-assistant/deploy.sh` living only on the box (plain `git pull` into a
regular clone, no bare repo, no verification step) — pulled into the repo once,
then the parent thread migrated the box itself to the bare-repo/checkout
layout at Larry's request on 2026-09-20 so this app would stop being the odd
one out. Separately, the box's git remote briefly pointed at
`https://github.com/lfulk33/draft-assistant.git` — the repo's name before a
rename to `draftiq` — surviving only on GitHub's rename-redirect. Both fixed
the same day; mentioned here in case either old shape shows up in a stale
script or bookmark somewhere.

## Data — the section the parent relies on

| Path | What it holds | Replaceable? |
|---|---|---|
| `players.json` (19MB) | Full Sleeper player DB | Yes — `sleeper_client.py`, cron-refreshed 03:00 daily from `checkout/` |
| `fantasy_players.json` (6.5MB) | FantasyCalc-enriched player values | Yes — `fantasycalc_client.py`, cron-refreshed 03:00 daily from `checkout/` |
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

- **`checkout/` has no `.git`.** It's a bare-repo work tree, not a normal
  clone — `cd checkout && git log` fails with "not a git repository". Use
  `git --git-dir=$HOME/draft-assistant/repo.git --work-tree=$HOME/draft-assistant/checkout <cmd>`
  from the box, or just work from your laptop and `deploy/push` instead.
- **The app's own JSON caches and `.env` live in `checkout/`, not the repo.**
  `git checkout -f` in the post-receive hook leaves untracked files alone, so
  deploys don't disturb them — but if you ever look for them at the old
  top-level `~/draft-assistant/*.json` path from before the 2026-09-20
  migration, they're gone from there; they're under `checkout/` now, and so
  are the systemd `WorkingDirectory` and the 03:00 crontab's `cd`.
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
