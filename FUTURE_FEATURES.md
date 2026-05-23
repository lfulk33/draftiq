# Future Features

## Roster View
- Toggle between dynasty value view and redraft value view for lineup suggestions
- Redraft mode would use weekly projections instead of fc_value for lineup decisions
- Optimize roster refresh to only re-fetch when a new pick by the user is detected, rather than every poll cycle, to reduce Sleeper API calls

## Roster Analysis
- Pull FantasyCalc redraft values alongside dynasty values (isDynasty=false endpoint)
- Store as fc_redraft_value on each player in fantasy_players.json
- Pass both dynasty and redraft value to Claude roster move prompts
- Claude should use redraft value as primary signal for taxi vs active bench decisions
- Add dynasty/redraft toggle to roster panel display

## Draft Strategy Modes
- **Full Dynasty:** Pure VORP/value-based drafting, accumulate assets and trade for needs. Prioritizes long-term upside over immediate roster balance.
- **Win Now:** Prioritizes redraft value and immediate contributors. Less developmental stashing, more proven veterans.
- **Balanced:** Enforces positional limits and roster balance. Ensures starters at every position before taking backups or developmental players.
- Each mode would adjust BPA threshold, positional cap enforcement, and Claude's reasoning priorities.

## Dual Recommendation Display
- Show two picks side by side: "Best Value" (highest VORP regardless of position) and "Balance Pick" (best VORP at most needed position)
- Claude explains the tradeoff between the two options
- User decides based on their strategy preference
- Example: "Pure value: Cam Ward (QB, VORP 348) | Balance: Jordyn Tyson (WR, VORP 341) — Ward is the best available player but you already have 2 QBs and 0 WRs"
- Maps naturally to strategy modes: Value Pick = Full Dynasty, Balance Pick = Balanced

## Draft Assistant
- Manual pick entry mode for ESPN and Yahoo leagues (no API)
- Multi-league profile support with saved configurations
- Draft replay/demo mode using historical draft data for testing

## Claude Reasoning
- Confidence score tuning based on historical recommendation accuracy
- Post-draft grade: how did Claude's recommendations compare to actual results
- Trade value suggestions based on roster construction after draft

## Model Settings
- User-selectable LLM model (Claude, GPT-4, Gemini)
- Bring your own API key support for freemium tier

## Hosting
- Move from Streamlit Community Cloud to AWS when revenue justifies it
- Persistent user accounts with saved league connections
- Background polling so app doesn't need to be open during draft