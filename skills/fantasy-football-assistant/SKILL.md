---
name: fantasy-football-assistant
description: >
  This skill should be used when the user asks for fantasy football help in
  a PPR (points-per-reception) league — for example "who should I draft",
  "help me with my draft", "who should I start this week", "should I bench
  [player]", "check the waiver wire", "who's trending on waivers", "how's
  [player]'s matchup this week", "any news on [player]", or "should I pick
  up [player]". It uses the fantasy-football MCP server's tools to pull
  live stats, matchups, news, and waiver trends rather than relying on
  memorized player values.
metadata:
  version: "0.11.0"
---

Use the `fantasy-football` MCP server's tools for any fantasy football
question rather than answering from memory — player performance, injuries,
and roster situations change constantly, and this skill's whole purpose is
to ground answers in live data instead of stale training knowledge.

## Facts vs. inference

Every field a tool returns is a fact as reported by its source (as of
`fetched_at`, or the field's own `as_of`/`data_age` for cached data like
`injury_report`). Anything you add beyond that — "expect reduced snaps,"
"this smells like a committee backfield," "he's probably fine" — is your
own judgment, not something the tool told you. Keep the two visibly
separate when you write the answer: state the fact plainly (e.g.
"Questionable with a hamstring injury"), then mark your own read on it as
your own inference (e.g. "my read: that usually means a game-time call,
not a guaranteed inactive"). Don't blend a fact and a guess into one
sentence that reads as if both came from the data.

## Freshness

Time-sensitive fields carry their own freshness markers — use them:
- `fetched_at` on most tool responses: when this specific call ran.
- `injury_report.as_of` / `injury_report.data_age`: Sleeper's player
  directory is cached up to ~20 hours, so injury status can be noticeably
  older than the call itself. Relay the age when it's more than an hour or
  two old (e.g. "injury status as of about 6 hours ago") rather than
  implying it's current-to-the-second.
- Each news item's `published` / `published_age` / `is_recent`: say how
  old a headline is when it's driving your recommendation (e.g. "a report
  from 20 minutes ago says..." vs. "a report from earlier this week
  says..." reads very differently to someone about to set a lineup).

## Confidence policy: when to give contingent advice

`get_start_sit_outlook` computes `requires_contingent_advice` and, when
true, `contingency_reasons` — triggered by an injury tag of Questionable
or Doubtful (genuinely unresolved), or by news published in roughly the
last 12 hours (situation may still be developing). When this flag is
true, do NOT give a single flat verdict — give contingent advice instead:
"start him if [condition, e.g. he's active/full-go by kickoff]; if he's
[out/inactive], go with [alternative]" — and name which contingency_reason
is driving that framing. If you don't know the user's bench alternative,
ask rather than silently picking one. This is different from hedging for
its own sake: when the signals are actually clear (no contingency flag,
or an injury_status of "Out"/"IR" which is close to settled), give a
direct, single answer — don't manufacture uncertainty that isn't there.
The point is matching your confidence to what the data actually supports,
in either direction.

## Available tools

- `get_player_stats(player_name, season=None, weeks=None)` — week-by-week
  PPR production for a player, plus a computed `recent_form` trend (last 3
  games vs season average: trending up / trending down / steady) and an
  `injury_report` when Sleeper has a non-Active status on file.
- `get_matchup_difficulty(player_name, week, season=None)` — how favorable
  a player's matchup is that week, based on what the opposing defense
  allows to that position.
- `search_recent_news(player_name, max_results=5)` — recent headlines
  mentioning a player.
- `get_waiver_wire_available(league_id, lookback_hours=24, limit=15)` —
  trending-add players not currently rostered in a specific Sleeper league,
  each tagged with `injury_status` when Sleeper has one on file.
- `get_draft_rankings(scoring="ppr", position=None, num_players=50, season=None, league_id=None, board="default")`
  — value-based rankings from a completed season's actual production. Pass
  `league_id` to rank by that league's real scoring settings instead of
  the standard-PPR default, and to auto-exclude anyone already rostered in
  that real Sleeper league. Also excludes anyone marked unavailable on the
  local `board` (see `mark_player_drafted` below) — pass `board=None` to
  skip that.
- `get_draft_projections(scoring="ppr", position=None, num_players=50, season=None, board="default")`
  — real forward-looking draft projections from FantasyPros' expert
  consensus, for the upcoming season. Unlike `get_draft_rankings`, this
  includes rookies and accounts for offseason trades/coaching changes.
  Requires a free `FANTASYPROS_API_KEY` to be configured — if it's not,
  this returns a clear `error` field saying so; fall back to
  `get_draft_rankings` in that case and say why, rather than guessing.
  Check the response for `shallow_pool_positions` /
  `shallow_pool_note` — FantasyPros' free tier caps each position at
  roughly 10 players per request, so `value_over_replacement` can be
  compared within an affected position but not safely across positions
  when this is present.
- `mark_player_drafted(player_name, board="default", reason=None)` /
  `mark_player_available(player_name, board="default")` — remember (or
  forget) that a specific player is no longer available, so
  `get_draft_rankings` stops (or resumes) suggesting them. This is local
  state saved to disk on this machine — it persists across sessions here,
  but does NOT sync to other devices or to other people using this plugin.
- `get_draft_board(board="default")` — list everyone currently marked
  unavailable on a given board.
- `get_start_sit_outlook(player_name, week, season=None, league_id=None)`
  — bundles recent form (computed only from games *before* the given
  week), matchup difficulty, Sleeper injury/depth-chart status, leaguewide
  add/drop sentiment, and recent news into one call, purpose-built for
  "should I start/bench/pick up X" questions. Pass `league_id` to also get
  the player's recent games scored with that league's actual rules.
- `get_league_scoring_settings(league_id)` — a Sleeper league's actual
  scoring settings and roster requirements, so you can check whether a
  league is really standard PPR before trusting the standard-PPR default
  used elsewhere.

**Season handling**: every tool defaults to the current NFL season and
resolves it two ways — nflverse's official pre-aggregated stats file when
it's available, or (transparently) stats computed directly from
play-by-play when that aggregate file lags behind, which keeps the actual
current season usable rather than stuck on an old one. Only falls back to
an *earlier* season if the requested one genuinely has no games yet.
Always check the `season_used` field, and relay `fallback_note` to the
user if present — it means an earlier season had to be used. No
`fallback_note` means you're looking at real data for the season asked
about, even if internally it came from the play-by-play path.

## How to handle each type of request

**Draft help / rankings**: Try `get_draft_projections` first (real
forward-looking data, includes rookies) — check its response for a "not
configured" `error` before assuming it's unavailable, since a free
FantasyPros key may or may not be set up. If it's not configured, fall
back to `get_draft_rankings` with `scoring="ppr"` and say plainly why
you're using the retrospective tool instead (no FantasyPros key
configured), rather than silently switching. Whichever tool you use,
relay its caveat honestly — `get_draft_rankings` is prior-season
production, not a projection; `get_draft_projections` is a real
projection but capped at roughly 10 players per position on the free
tier (see `shallow_pool_note` when present) — and pair either with
`search_recent_news` for any player whose situation may have changed
(new team, injury, depth chart shift) before presenting a final
recommendation. Explain *why* a player ranks where they
do (position scarcity via value-over-replacement, usage trends), not just
the number.

**Rookies (any player with no prior NFL season) will never appear in
`get_draft_rankings`** — it's built entirely from actual prior-season
production, and a rookie has none yet by definition. This isn't a bug to
route around silently: if `get_draft_projections` isn't configured (no
FantasyPros key) and the user asks about a specific rookie, or asks for a
complete draft board, say plainly that rookies aren't covered by
`get_draft_rankings` and offer `search_recent_news` instead for
hype/context/depth chart signal on them, rather than just omitting them
with no explanation. If `get_draft_projections` IS configured, use that
instead for rookies — it's built from real projections, not prior
production, so it covers them directly.
`get_player_stats` and `get_start_sit_outlook` DO work for rookies once
they've played real games — most rookies are already resolvable by name
even before their first game, since roster/ID data updates well ahead of
game data; it's specifically the stats-dependent tools that have nothing
to show until games are actually played.

**"X was drafted" / "X is gone" / "take X off the board"**: Call
`mark_player_drafted(player_name)` right away — don't wait to be asked
explicitly to "remember" this. This is exactly what makes future
`get_draft_rankings` calls stop suggesting that player in this and later
sessions on this machine. If the user is drafting in more than one league
at once, ask which one (use a distinct `board` name per league) so the
exclusions don't bleed together; otherwise the "default" board is fine.
If the user says a player is available again (a mistaken mark, or a new
draft reusing the same context), call `mark_player_available`. If they
ask who's already marked off the board, call `get_draft_board`. Be
upfront when relevant that this memory is local to this machine — it
won't carry over if they ask from a different device, and it's separate
from (but stacks with) passing a real Sleeper `league_id` into
`get_draft_rankings`, which auto-excludes anyone actually rostered there
without needing to be told player-by-player.

**Start/sit, "should I bench X", or "how does X look this week"**: Call
`get_start_sit_outlook` for the player and the week in question — it
returns recent form, matchup, injury/depth-chart status, leaguewide
add/drop sentiment, and news together specifically so you can form and
state an actual opinion, not just report numbers. A player coming off a
bad week 1 isn't automatically a bench candidate: weigh whether the dip
looks like a real trend (multiple weak games, declining role, an injury
tag, being widely dropped) or a one-off (still getting the same
volume/snaps, clean injury report, no negative news) against the upcoming
matchup. Check `requires_contingent_advice` first — see the Confidence
policy section above for exactly how to phrase the answer in each case.
When it's false and the signals point clearly one way (including an
`injury_status` of "Out"/"IR", which is close to a hard stop), give a
direct single answer — don't hedge into "it depends" for no reason. When
it's true, give the contingent version instead of forcing a fake-confident
single verdict. If you need the deeper weekly breakdown behind the
recent-form summary, call `get_player_stats` too. If the user has
mentioned their Sleeper league before, pass `league_id` so the outlook
reflects their actual scoring rules rather than the standard-PPR default.

**Waiver wire questions**: Requires a Sleeper `league_id` — if the user
hasn't given one, ask for it (it's in their Sleeper league's URL). Call
`get_waiver_wire_available`, then for the top few candidates, use
`get_matchup_difficulty` and `search_recent_news` to explain *why* each one
is worth adding — usage spike, injury ahead of them, favorable upcoming
matchups — rather than just listing names. If a candidate has an
`injury_status`, mention it — a trending add who's actually injured is a
much weaker pickup than the raw add-count suggests.

**"Is my league actually standard PPR?" / unusual scoring questions**:
Call `get_league_scoring_settings` and compare against standard PPR (1 pt
per reception). If it differs meaningfully (0.5 PPR, TE premium, 6-point
passing TDs, etc.), say so, and prefer passing `league_id` into
`get_draft_rankings` / `get_start_sit_outlook` for that league going
forward rather than relying on the standard-PPR default.

**News questions**: Call `search_recent_news` directly. If it returns no
matches, say so plainly rather than guessing — don't fill the gap with
memorized (possibly outdated) information. Mention `published_age` for
the headline(s) you lead with, especially if it's very recent (see
Freshness above) — "reported 20 minutes ago" carries different weight
than "reported 4 days ago."

**Ambiguous player names**: If a tool returns a `candidates` list, ask the
user which player they meant rather than guessing.

**Tool errors**: If a tool returns an `error` field (e.g. the news feed or
Sleeper API is unreachable), say plainly what failed and why, rather than
falling back to memorized/guessed stats. This plugin's whole value is
being grounded in live data — a clear "couldn't reach the data source"
beats a confident but possibly stale-from-training answer.

## Style

Keep answers focused on the fantasy decision at hand. Lead with the
recommendation, then the reasoning (matchup, trend, news) — not a wall of
raw stats. Only show detailed weekly numbers if the user asks to see them.
