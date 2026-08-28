# fantasy-football-agent

A PPR fantasy football assistant for Claude. Ask about draft rankings,
weekly matchups, recent player news, or waiver-wire pickups, and it pulls
real, current data instead of guessing from memory.

## How it works

This plugin bundles a small MCP server with a skill that tells Claude when
and how to use it. Every tool fetches **live**, at the moment you ask, from
free public sources:

- [`nfl_data_py`](https://github.com/nflverse/nfl_data_py) (nflverse/nflfastR data) — historical and weekly NFL stats, including a play-by-play fallback for the most current games (see below)
- [Sleeper's public API](https://docs.sleeper.com/) — league rosters and scoring settings, trending adds/drops, and player injury/depth-chart status
- ESPN's public NFL news RSS feed — recent headlines

**There is no hosted backend, no account, and no API key required.** The
server runs as a local process on your own machine, started automatically
by Claude Code / Cowork when the plugin is installed. That also means it
costs nothing to run beyond your own normal Claude usage — there's no
shared infrastructure to pay for, no matter how many people install it.
Nearly everything is fetched live with no storage at all; the one
exception is a small local file that remembers which players you've
marked as drafted (see "Remembering who's already drafted" below) — it
lives only on your machine, not on any server.

## Components

| Component | What it does |
|---|---|
| `fantasy-football-assistant` skill | Tells Claude when to reach for these tools and how to combine them into a recommendation |
| `fantasy-football` MCP server | Exposes 11 tools: `get_player_stats`, `get_matchup_difficulty`, `search_recent_news`, `get_waiver_wire_available`, `get_draft_rankings`, `get_draft_projections`, `mark_player_drafted`, `mark_player_available`, `get_draft_board`, `get_start_sit_outlook`, `get_league_scoring_settings` |

### Remembering who's already drafted

Say "Christian McCaffrey was drafted" or "take Bijan Robinson off the
board" mid-draft, and Claude calls `mark_player_drafted` — future calls to
`get_draft_rankings` in this and later conversations stop suggesting that
player, without you needing to re-filter the list yourself every time.
`mark_player_available` undoes it, and `get_draft_board` lists everyone
currently marked off.

This is genuinely persistent (a small JSON file at
`~/.local/share/fantasy-football-agent/draft_boards.json`), but it's
**local to the machine running the MCP server** — it doesn't sync to your
phone, another computer, or to any friend who's also installed this
plugin. If you're drafting from more than one device, or want everyone in
a real Sleeper league excluded automatically without naming players one
by one, pass that league's `league_id` into `get_draft_rankings` instead
(or in addition) — it already cross-references who's actually rostered
there, live, every time.

### Draft projections that actually include rookies (optional)

`get_draft_rankings` (above) is deliberately retrospective — it ranks
players by what they actually did in a completed season, which means a
rookie with zero prior NFL games structurally can't appear in it. For
real forward-looking draft prep, there's a second tool,
`get_draft_projections`, that pulls real expert-consensus **projections**
for the upcoming season from [FantasyPros](https://www.fantasypros.com/api-data/)
— includes rookies, and accounts for offseason trades, coaching changes,
and depth-chart moves that pure past-production numbers can't see.

This is optional and off by default: get a free FantasyPros API key
(personal/non-commercial use tier, no card) and set it as an environment
variable (`FANTASYPROS_API_KEY`) wherever the server is running. Without
a key configured, `get_draft_projections` returns a clear "not
configured" message and the skill falls back to `get_draft_rankings`
instead of failing silently.

Two things worth knowing if you're relying on this:
- **FantasyPros' free tier caps each position at roughly 10 players per
  request**, regardless of how many you ask for. Fine for early-round
  draft prep, thin for late rounds or deep leagues. When this cap is hit,
  the response includes a `shallow_pool_positions` /
  `shallow_pool_note` field flagging exactly which positions were
  affected, and cautions against comparing `value_over_replacement`
  across positions when one position's pool is artificially shallow —
  ranking within a position is still solid.
- **`get_draft_rankings` and `get_draft_projections` answer different
  questions** — actual past production vs. real forward projection —
  and are meant to be used together, not as interchangeable versions of
  the same thing.

### Facts, freshness, and confidence — not false certainty

Fantasy advice is easy to get wrong in a specific way: stating a guess
with the same confidence as a fact, or giving a flat "start him" the
moment before news breaks that changes the picture. This plugin is built
to avoid both:

- **Facts vs. inference.** Every field a tool returns is a fact as
  reported by its source — "Questionable, hamstring" is Sleeper's injury
  designation, not a guess. The skill is instructed to keep that visibly
  separate from Claude's own judgment ("my read: that usually means a
  game-time call") rather than blending the two into one sentence that
  reads as if both came from the data.
- **Timestamped freshness.** Every tool response carries a `fetched_at`.
  Injury status specifically can be a few hours old (Sleeper's player
  directory is cached for ~20h, per their own guidance on how often to
  call it) and carries its own `as_of` / `data_age`. News headlines carry
  `published_age` ("20 minutes ago" vs. "4 days ago"). The skill is
  instructed to surface these, not just the underlying fact, when they're
  relevant to the recommendation.
- **A real confidence policy, not hedging by default.**
  `get_start_sit_outlook` computes `requires_contingent_advice` —
  triggered specifically by a Questionable/Doubtful injury tag or news
  published in roughly the last 12 hours. When that's true, the skill
  gives contingent advice ("start him if he's active by kickoff;
  otherwise go with your alternative") instead of a false single verdict.
  When it's false — including a clean injury report, or an "Out"/"IR" tag
  that's close to settled — it gives a direct, single answer instead of
  manufacturing uncertainty that isn't there.

### Using more than just rosters from Sleeper

Early versions of this plugin only used Sleeper for two things: who owns
which player in a league, and which players are trending on waivers. Sleeper's
public API has more worth pulling in, so this plugin also uses:

- **Player injury/depth-chart status** — Sleeper's player directory
  includes `injury_status` (Questionable/Doubtful/Out/IR), injury body
  part, and depth-chart position/order. Surfaced as `injury_report` on
  `get_player_stats` and `get_start_sit_outlook`, and as `injury_status` on
  waiver-wire candidates — a trending add who's actually hurt is a much
  weaker pickup than the raw add-count alone suggests. This directory is a
  multi-MB response, so it's cached to disk for about a day rather than
  re-fetched on every call, in line with Sleeper's own guidance.
- **Trending drops**, not just adds — `get_start_sit_outlook` checks
  whether a player is being widely dropped leaguewide and surfaces that as
  `leaguewide_sentiment`, a corroborating signal alongside recent form and
  matchup.
- **A league's actual scoring settings** — `get_league_scoring_settings`
  returns a league's real `scoring_settings` and roster requirements, and
  `league_id` is now accepted by `get_draft_rankings` and
  `get_start_sit_outlook` to score using those real settings instead of
  the standard-PPR default. Useful the moment a league isn't exactly
  standard PPR (0.5 PPR, TE premium, non-standard TD values, etc.). This
  mapping is best-effort for common scoring categories — Sleeper doesn't
  publicly document the exact `scoring_settings` key list, so uncommon
  bonus categories may not be captured; the raw `scoring_settings` is
  always included in the response so a mismatch is visible rather than
  silent.

Not yet used: Sleeper's league `matchups` and full roster `starters`
endpoints, which could power an "optimal lineup against my actual
opponent this week" tool — a reasonable next step, not built yet.

### Forming an opinion, not just reporting stats

`get_start_sit_outlook` is the tool built for "should I bench X" questions.
It bundles three signals for one player ahead of one week:

- **Recent form** — a computed trend (trending up / down / steady) from
  games *before* that week, so a start/sit call about week 5 isn't
  contaminated by week 5's own result.
- **Matchup difficulty** — how generous the upcoming opponent has been to
  that position league-wide.
- **Recent news** — anything currently being reported about the player.

The skill instructs Claude to weigh these into an actual opinion (e.g.
"one bad week isn't a trend if usage held steady and there's no news — no
reason to bench him yet" vs. "three weak games plus a tough matchup plus a
report of reduced snaps — sit him"), rather than just listing the numbers
and leaving the decision to you. It's not predicting the future — it's
reasoning from real recent performance and real reported news, the same
way a well-informed friend would before Sunday.

### Current-season data, without waiting on nflverse's aggregate file

nflverse publishes NFL data in a few separate pieces, and they don't all
land on the same schedule. The convenience file most tools would normally
use (pre-aggregated player stats per week) can lag behind — there have
been real stretches where it hadn't been (re)generated for the current
season even though the season was well underway. The raw play-by-play
release, however, updates continuously as games are played and is
essentially always current.

So this plugin tries the pre-aggregated file first (fast, and matches
nflverse's official numbers exactly), and if that's not out yet for the
season in question, computes the same stat columns itself directly from
play-by-play instead of falling back to an older season. That keeps the
current season usable the moment games exist for it, without depending on
a second team's aggregation schedule.

Two things worth knowing about the play-by-play path:
- **Fantasy points are a computed approximation** in that case (standard
  PPR formula: 1 pt/reception, 1 pt per 10 rush/rec yard, 6 pts/rush or
  rec TD, 1 pt per 25 pass yard, 4 pts/pass TD, -2 per interception or
  lost fumble). It won't be a bit-for-bit match to any one platform's exact
  house rules or include things like return TDs — close enough for
  rankings and trend-spotting, not a replacement for your league's own
  scoring if it's unusual.
- Every response includes `season_used`. A `fallback_note` only appears
  when an *earlier* season had to be used (e.g. you ask about a season
  that hasn't started); using the play-by-play path for the season you
  actually asked about isn't flagged as a fallback, since it's still
  current data for the right season, just computed a different way.

## Requirements

- Python 3.10+ (must be what `python3` resolves to on whatever machine runs the server)
- The packages in `requirements.txt` (`fastmcp`, `nfl_data_py`, `pandas`, `requests`) — installed automatically on first run, see Setup below
- Normal internet access (to reach nflverse's data releases, Sleeper's API, and ESPN's RSS feed — and, once, PyPI for that first-run dependency install)

No accounts, API keys, or paid services needed.

## Setup

Install the plugin — either way works, no separate setup step required:

- **Claude Code**: this repo is itself a plugin marketplace
  (`.claude-plugin/marketplace.json`), so:

  ```bash
  claude plugin marketplace add <your-github-username>/fantasy-football-agent
  claude plugin install fantasy-football-agent@fantasy-football-agent-marketplace
  ```

- **Cowork**: download the packaged `.plugin` file from this repo's
  releases and open/save it directly.

The MCP server bootstraps its own Python dependencies
(`fastmcp`, `nfl_data_py`, `pandas`, `requests`) the first time it
actually runs — there's no separate `pip install` step to remember,
regardless of which install path you used. That first run takes a minute
or two longer than normal while it installs; after that, startup is fast.
(If you're developing on this repo directly rather than just installing
it, `pip install -r requirements.txt` still works the normal way and
skips that first-run wait.)

**Sanity check before relying on it**: ask it something simple first
("what's the current NFL season?" or a draft-rankings question) and
confirm it actually calls the `fantasy-football` tools rather than
saying the MCP server isn't connected. Give the first attempt a minute or
two in case it's still installing dependencies; if it still reports the
server as disconnected after that, something else is wrong — check that
`python3` resolves to a real Python 3.10+ on whatever machine is running
the server, and that it has normal internet access (needed both for the
one-time dependency install and for every tool call afterward).

## Usage

Once installed, just ask naturally:

- "Who should I draft first in a PPR league?"
- "How's Bijan Robinson's matchup look in week 6?"
- "Any recent news on CeeDee Lamb?"
- "Check the waiver wire for league `<your Sleeper league ID>`"
- "Should I start or sit Jayden Reed this week?"

Your Sleeper league ID (needed for waiver-wire questions) is in your
league's URL on sleeper.com, e.g. `sleeper.com/leagues/<league_id>`.

## Running it as an always-on remote service (Render + Upstash)

The local install above is the simplest path — the server only exists
while your own machine is running Claude. This repo also supports a
second, fully self-hosted deployment mode: the exact same `servers/`
code, run continuously as a small free web service, reachable from any
device (including your phone, via Claude's mobile app) as a
[custom MCP connector](https://claude.com/docs/connectors/custom/remote-mcp)
— no laptop needing to stay open.

This is the deployment I actually run day-to-day. It's built on two free
tiers, deliberately paired for a real reason rather than picked
arbitrarily: [Render](https://render.com) hosts the server itself, but
its free tier's filesystem is **ephemeral** — anything written to local
disk is wiped on every restart, which happens automatically after ~15
minutes idle. Since the whole point of the draft board is that it
persists ("don't suggest players I've already drafted"), that state is
stored in [Upstash](https://upstash.com) Redis instead — also free, and
unlike Render's disk, it survives restarts. `render.yaml` at the repo
root defines the full deploy (build/start commands, Python version
pin, and the optional `FANTASYPROS_API_KEY` / required
`UPSTASH_REDIS_REST_URL` + `UPSTASH_REDIS_REST_TOKEN` env vars) as a
Render Blueprint, so a fork deploys with no manual service configuration.

Full walkthrough, including the auth option and the cold-start tradeoff,
is in [`deploy/README.md`](deploy/README.md). This is optional and
separate from the local plugin, and it's yours alone to run — if someone
else wants the same, they deploy their own copy rather than sharing
yours; nobody's hosting bill or inference cost is shared.

## Known limitations

- **`get_draft_rankings` is retrospective, not a projection, and never
  includes rookies.** It ranks players by actual production in a
  completed season, adjusted for position scarcity. A rookie has no prior
  NFL season, so they structurally can't appear here — not a bug, a
  direct consequence of how the ranking works. (Real forward-looking
  projections that *do* include rookies are available via the optional
  `get_draft_projections` tool — see "Draft projections that actually
  include rookies" above.) The skill still says this plainly for
  `get_draft_rankings` specifically, and offers `search_recent_news` for
  rookie context. Verified directly against the current rookie class
  (2026 draft): 290 rookies are already resolvable by name via
  `get_player_stats` / `get_start_sit_outlook` — roster and ID data
  updates well ahead of game data — but none appear in
  `get_draft_rankings` until they've actually played, and about a third
  don't yet have a Sleeper ID mapped, so very recent additions may not
  show up in `get_waiver_wire_available` right away either.
- **News search covers ESPN's general NFL feed**, not every beat writer or
  team-specific source. No results doesn't necessarily mean no news exists.
- **Waiver-wire tool currently supports Sleeper only** (not ESPN/Yahoo leagues).
- **No paid data providers (e.g. SportsDataIO) are used, on purpose.**
  Evaluated directly against this project's zero-recurring-cost design:
  their free tiers cap out at last season's data, and current-season/
  injury/fantasy-specific feeds require a paid subscription. The one
  thing they'd add that's genuinely useful — numeric projections — is
  already covered for free via the optional FantasyPros integration
  above (with its own free-tier player-count cap, noted there). What
  they'd still add beyond that is broader real-time news aggregation past
  ESPN's general feed, and (mostly redundant with FantasyPros) Vegas
  spread/total lines for implied team scoring — nflverse's free schedule
  data already includes these (verified: populated for the full current
  season, including games not yet played), just not wired into a tool yet.
- Data source terms of use are the responsibility of whoever runs this
  server — Sleeper's API and nflverse's data are free for this kind of use,
  but check current terms before relying on this for anything beyond casual
  league use.

This tool is for entertainment/informational purposes — it doesn't
guarantee outcomes, and fantasy football is still a game of chance.

## License

MIT — see `LICENSE`.
