"""
fantasy-football-agent MCP server.

Exposes ten tools for PPR fantasy football decisions. Every tool fetches
live from free public sources at call time (nfl_data_py, Sleeper's public
API, ESPN's public NFL news RSS feed) — there is no database, no hosted
backend, and no API key required. This process runs locally, started by
whatever Claude client has this plugin installed.

The one exception to "no persistent state": a small local JSON file
(~/.local/share/fantasy-football-agent/draft_boards.json) remembers
players marked drafted/unavailable via mark_player_drafted, so
get_draft_rankings can stop suggesting them across sessions. This file
lives only on the machine running this server — it is not synced or
shared with anyone else who has this plugin installed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
from fastmcp import FastMCP

from football_data import (
    compute_league_fantasy_points,
    current_season,
    draft_board_exclusion_keys,
    fetch_nfl_news_items,
    get_draft_board as fd_get_draft_board,
    get_id_crosswalk,
    get_league,
    get_seasonal_data_with_fallback,
    get_trending,
    get_weekly_data_with_fallback,
    matchup_difficulty_for,
    mark_player_status,
    normalize_stat_row,
    now_utc_iso,
    parse_weeks,
    recent_form_for,
    resolve_player,
    sleeper_get,
    sleeper_player_report,
)

# Optional bearer-token auth, used only for the self-hosted remote deployment
# (see deploy/README.md) — irrelevant to the normal local plugin install,
# where nothing else can reach this process anyway. Off by default: set
# MCP_AUTH_TOKEN before starting the server to require it. Claude's "Add
# custom connector" dialog can send this back as an Authorization: Bearer
# header via its (currently beta, gradually-rolling-out) Request Headers
# option — if that's not available on your account yet, this still works
# fine unauthenticated, same as it always did.
_auth_token = os.environ.get("MCP_AUTH_TOKEN")
_auth = None
if _auth_token:
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

    _auth = StaticTokenVerifier(
        tokens={_auth_token: {"client_id": "fantasy-football-agent-owner", "scopes": []}}
    )

mcp = FastMCP(
    "fantasy-football",
    auth=_auth,
    instructions=(
        "Tools for PPR fantasy football: historical weekly stats, matchup "
        "difficulty, recent news, waiver-wire trends, value-based draft "
        "rankings, a combined start/sit outlook, and league scoring "
        "settings. Almost all data is live and free with no persistent "
        "state — the one exception is a local draft board: if the user "
        "says a player was drafted, taken, or is no longer available, call "
        "mark_player_drafted so get_draft_rankings stops suggesting them; "
        "this is saved to disk on this machine and persists across "
        "sessions here, but does not sync anywhere else. If a requested "
        "season isn't published yet, tools "
        "automatically fall back to the most recent available season and "
        "say so via `season_used` / `fallback_note`. Player-level tools "
        "also surface Sleeper's injury/depth-chart status and leaguewide "
        "add/drop sentiment when available, and accept an optional "
        "`league_id` to score using that league's actual scoring settings "
        "instead of the standard-PPR default.\n\n"
        "IMPORTANT — facts vs. inference: every field in a tool's response "
        "is a fact as reported by its source as of `fetched_at` (or the "
        "field's own `as_of`/`data_age`, where a value is cached and may "
        "be older than the call itself). Anything beyond that — 'expect "
        "reduced snaps', 'this suggests a committee backfield' — is your "
        "own judgment, not a fact from these tools. Keep the two visibly "
        "separate in responses to the user (e.g. state the fact, then "
        "label your read on it as your own inference) rather than blending "
        "them into one undifferentiated statement.\n\n"
        "IMPORTANT — confidence policy: when `requires_contingent_advice` "
        "is true on get_start_sit_outlook (an uncertain injury tag, or "
        "news published in roughly the last 12 hours), do not give a "
        "single flat verdict — give contingent advice instead ('start X "
        "if he's active/full-go; if he's out, go with Y'), and say why "
        "(the specific injury tag or how recent the news is). Naming a "
        "concrete alternative matters more than hedging in prose — ask "
        "the user for their bench alternative if you don't already know "
        "their roster."
    ),
)


def _format_candidates(candidates: list[dict]) -> list[str]:
    return [
        f"{c.get('name')} ({c.get('position')}, {c.get('team')})" for c in candidates
    ]


def _resolve_or_error(player_name: str) -> tuple[dict | None, dict | None]:
    """Returns (player, error_response). Exactly one is non-None."""
    player = resolve_player(player_name)
    if player is None:
        return None, {"error": f"No player found matching '{player_name}'."}
    if isinstance(player, list):
        return None, {
            "error": "Multiple players match that name — ask the user which one.",
            "candidates": _format_candidates(player),
        }
    gsis_id = player.get("gsis_id")
    if not gsis_id or pd.isna(gsis_id):
        return None, {"error": f"Found {player['name']} but no stats ID is available for them."}
    return player, None


@mcp.tool()
def get_player_stats(player_name: str, season: int | None = None, weeks: str | None = None) -> dict:
    """Get a player's week-by-week PPR stats for a season, plus a computed
    recent-form trend (last 3 games vs season average).

    Args:
        player_name: Player's name, e.g. "CeeDee Lamb". Fuzzy-matched.
        season: NFL season year (e.g. 2025). Defaults to the current/most
            recently completed season if omitted. If the requested season
            isn't published yet, automatically falls back to the most
            recent one that is, and says so in the response.
        weeks: Optional week filter, e.g. "1-5", "3", or "1,4,9". Defaults
            to all weeks played so far that season.
    """
    player, err = _resolve_or_error(player_name)
    if err:
        return err

    try:
        df, season_used, was_fallback = get_weekly_data_with_fallback(season or current_season())
    except RuntimeError as exc:
        return {"error": str(exc)}

    pdf_all = df[df["player_id"] == player["gsis_id"]]

    week_list = parse_weeks(weeks)
    pdf = pdf_all[pdf_all["week"].isin(week_list)] if week_list else pdf_all

    if pdf.empty:
        return {"error": f"No {season_used} weekly stats found for {player['name']}."}

    pdf = pdf.sort_values("week")
    cols = [
        "week", "opponent_team", "receptions", "targets", "receiving_yards",
        "receiving_tds", "rushing_yards", "rushing_tds", "passing_yards",
        "passing_tds", "fantasy_points_ppr",
    ]
    cols = [c for c in cols if c in pdf.columns]
    weekly_rows = pdf[cols].round(1).to_dict("records")

    result = {
        "player": player["name"],
        "position": player.get("position"),
        "team": player.get("team"),
        "season_used": season_used,
        "weeks": weekly_rows,
        "season_total_ppr": round(pdf["fantasy_points_ppr"].sum(), 1),
        "season_avg_ppr": round(pdf["fantasy_points_ppr"].mean(), 1),
        "recent_form": recent_form_for(player["gsis_id"], pdf_all),
        "fetched_at": now_utc_iso(),
    }
    injury = sleeper_player_report(player.get("sleeper_id"))
    if injury and (injury.get("injury_status") or injury.get("status") not in (None, "Active")):
        result["injury_report"] = injury
    if was_fallback:
        result["fallback_note"] = (
            f"Requested season data wasn't available yet; using {season_used} instead."
        )
    return result


@mcp.tool()
def get_matchup_difficulty(player_name: str, week: int, season: int | None = None) -> dict:
    """Rate how favorable a player's matchup is for a given week.

    Compares the opponent defense's average PPR points allowed to the
    player's position against every other defense's average, for the
    same season. A higher rank number means an easier matchup.

    Args:
        player_name: Player's name. Fuzzy-matched.
        week: NFL week number to check.
        season: NFL season year. Defaults to the current season, with
            automatic fallback if that season isn't published yet.
    """
    player, err = _resolve_or_error(player_name)
    if err:
        return err

    try:
        weekly, season_used, was_fallback = get_weekly_data_with_fallback(season or current_season())
    except RuntimeError as exc:
        return {"error": str(exc)}

    result = matchup_difficulty_for(player, week, weekly, season_used)
    if "error" in result:
        return result

    result = {
        "player": player["name"],
        "position": player.get("position"),
        "week": week,
        "season_used": season_used,
        **result,
        "fetched_at": now_utc_iso(),
    }
    if was_fallback:
        result["fallback_note"] = (
            f"Requested season data wasn't available yet; using {season_used} instead."
        )
    return result


@mcp.tool()
def search_recent_news(player_name: str, max_results: int = 5) -> dict:
    """Search recent NFL headlines for mentions of a player.

    Source: ESPN's public NFL news RSS feed (no API key required). This
    reflects general NFL news coverage, not every beat-writer report, so
    treat an empty result as "nothing in this feed right now," not as
    confirmation there's no news anywhere.

    Args:
        player_name: Player's name to search for.
        max_results: Max number of headlines to return.
    """
    try:
        items = fetch_nfl_news_items()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Could not fetch the NFL news feed: {exc}"}

    name_lower = player_name.strip().lower()
    matches = [
        it for it in items
        if name_lower in it["title"].lower() or name_lower in it["summary"].lower()
    ]

    fetched_at = now_utc_iso()
    if not matches:
        return {
            "player": player_name,
            "matches": [],
            "fetched_at": fetched_at,
            "note": (
                "No recent headlines mentioning this player in ESPN's current "
                "NFL news feed. Try again later or broaden the search."
            ),
        }

    return {"player": player_name, "matches": matches[:max_results], "fetched_at": fetched_at}


@mcp.tool()
def get_waiver_wire_available(league_id: str, lookback_hours: int = 24, limit: int = 15) -> dict:
    """Find trending-up players not rostered in a given Sleeper league.

    Cross-references Sleeper's public "trending adds" feed against every
    roster in the league, so it only surfaces players who are actually
    available to pick up.

    Args:
        league_id: The Sleeper league ID (found in the league's URL).
        lookback_hours: Trending window Sleeper should use (default 24h).
        limit: Max number of available trending players to return.
    """
    try:
        rosters = sleeper_get(f"/league/{league_id}/rosters")
    except RuntimeError as exc:
        return {"error": str(exc)}

    if not isinstance(rosters, list):
        return {"error": f"Unexpected response for league '{league_id}' — check the league ID."}

    owned_sleeper_ids = set()
    for r in rosters:
        for pid in (r.get("players") or []):
            owned_sleeper_ids.add(str(pid))

    try:
        trending = get_trending("add", lookback_hours=lookback_hours, limit=limit * 4)
    except RuntimeError as exc:
        return {"error": str(exc)}

    ids = get_id_crosswalk().dropna(subset=["sleeper_id"]).copy()
    ids["sleeper_id"] = ids["sleeper_id"].astype(int).astype(str)
    ids = ids.set_index("sleeper_id")

    available = []
    for entry in trending:
        pid = str(entry.get("player_id"))
        if pid in owned_sleeper_ids:
            continue
        if pid not in ids.index:
            continue
        row = ids.loc[pid]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        candidate = {
            "name": row["name"],
            "position": row["position"],
            "team": row["team"],
            "adds_in_window": entry.get("count"),
        }
        injury = sleeper_player_report(pid)
        if injury and injury.get("injury_status"):
            candidate["injury_status"] = injury["injury_status"]
        available.append(candidate)
        if len(available) >= limit:
            break

    return {
        "league_id": league_id,
        "lookback_hours": lookback_hours,
        "available_trending": available,
        "fetched_at": now_utc_iso(),
    }


@mcp.tool()
def get_draft_rankings(scoring: str = "ppr", position: str | None = None, num_players: int = 50, season: int | None = None, league_id: str | None = None, board: str | None = "default") -> dict:
    """Value-based draft rankings from a completed season's actual production.

    Excludes players already taken — both players you've marked drafted on
    your `board` (see `mark_player_drafted`) and, if `league_id` is given,
    anyone already rostered in that real Sleeper league. Value-over-
    replacement is still computed against the full player pool (so it
    doesn't swing wildly as picks happen); only the final list is filtered.

    IMPORTANT CAVEAT: this ranks players by how they actually performed in
    `season_used`, adjusted for position scarcity (value over replacement).
    It is NOT a forward-looking projection — it doesn't account for
    offseason trades, injuries, coaching changes, or rookies with no
    prior-season stats. Use it as a data-grounded starting point, and
    combine it with `search_recent_news` for anything that might change a
    player's outlook before presenting a final recommendation.

    Args:
        scoring: "ppr" (default) or "standard". Ignored if league_id is given.
        position: Optional filter, e.g. "RB", "WR", "QB", "TE".
        num_players: Max number of players to return.
        season: Season year to rank by. Defaults to the current/most
            recently completed season, with automatic fallback if that
            season isn't published yet.
        league_id: Optional Sleeper league ID. When given, ranks by that
            league's actual scoring settings instead of the standard-PPR
            default, AND excludes anyone already rostered in that league.
        board: Draft board name to exclude players from (see
            `mark_player_drafted`). Defaults to "default" — the same board
            used if you don't name one when marking players. Pass None to
            skip this exclusion entirely.
    """
    try:
        seasonal, season_used, was_fallback = get_seasonal_data_with_fallback(season or current_season())
    except RuntimeError as exc:
        return {"error": str(exc)}

    ids = (
        get_id_crosswalk()[["gsis_id", "sleeper_id", "name", "position", "team"]]
        .drop_duplicates(subset=["gsis_id"])
        .dropna(subset=["gsis_id"])
    )

    merged = seasonal.merge(ids, left_on="player_id", right_on="gsis_id", how="left")

    league_scoring_settings = None
    owned_sleeper_ids = set()
    if league_id:
        try:
            league_scoring_settings = get_league(league_id).get("scoring_settings", {})
            rosters = sleeper_get(f"/league/{league_id}/rosters")
            if isinstance(rosters, list):
                for r in rosters:
                    for pid in (r.get("players") or []):
                        owned_sleeper_ids.add(str(pid))
        except RuntimeError as exc:
            return {"error": str(exc)}
        merged["league_points"] = merged.apply(
            lambda r: compute_league_fantasy_points(normalize_stat_row(r), league_scoring_settings), axis=1
        )
        col = "league_points"
    else:
        col = "fantasy_points_ppr" if scoring.lower() == "ppr" else "fantasy_points"
    merged = merged.dropna(subset=[col, "name"])

    # Typical waiver-level replacement rank per position (12-team league assumption).
    replacement_rank = {"QB": 12, "RB": 30, "WR": 36, "TE": 12, "K": 12, "DST": 12}

    ranked_parts = []
    for pos, rep_rank in replacement_rank.items():
        pos_df = merged[merged["position"] == pos].sort_values(col, ascending=False).reset_index(drop=True)
        if pos_df.empty:
            continue
        rep_idx = min(rep_rank, len(pos_df)) - 1
        replacement_score = pos_df.loc[rep_idx, col]
        pos_df = pos_df.copy()
        pos_df["value_over_replacement"] = pos_df[col] - replacement_score
        ranked_parts.append(pos_df)

    if not ranked_parts:
        return {"error": f"No {season_used} seasonal data available."}

    all_ranked = pd.concat(ranked_parts).sort_values("value_over_replacement", ascending=False)
    if position:
        all_ranked = all_ranked[all_ranked["position"] == position.upper()]

    # Exclude already-taken players — manual board first, then real league rosters.
    excluded_count = 0
    board_gsis_ids, board_names = (set(), set())
    if board:
        board_gsis_ids, board_names = draft_board_exclusion_keys(board)
    if board_gsis_ids or board_names:
        before = len(all_ranked)
        all_ranked = all_ranked[
            ~all_ranked["gsis_id"].isin(board_gsis_ids) & ~all_ranked["name"].isin(board_names)
        ]
        excluded_count += before - len(all_ranked)
    if owned_sleeper_ids:
        before = len(all_ranked)
        all_ranked["sleeper_id_str"] = all_ranked["sleeper_id"].apply(
            lambda v: str(int(v)) if pd.notna(v) else None
        )
        all_ranked = all_ranked[~all_ranked["sleeper_id_str"].isin(owned_sleeper_ids)]
        excluded_count += before - len(all_ranked)

    all_ranked = all_ranked.head(num_players)

    points_label = f"league_points_{season_used}" if league_id else f"{scoring.lower()}_points_{season_used}"
    rankings = [
        {
            "name": r["name"],
            "position": r["position"],
            "team": r["team"],
            points_label: round(float(r[col]), 1),
            "value_over_replacement": round(float(r["value_over_replacement"]), 1),
        }
        for _, r in all_ranked.iterrows()
    ]

    result = {
        "season_used": season_used,
        "scoring": "league-specific" if league_id else scoring,
        "caveat": "Ranked on actual prior-season production, not a forward projection.",
        "rankings": rankings,
        "fetched_at": now_utc_iso(),
    }
    if excluded_count:
        exclusion_sources = []
        if board_gsis_ids or board_names:
            exclusion_sources.append(f"your '{board}' draft board")
        if owned_sleeper_ids:
            exclusion_sources.append(f"league {league_id}'s current rosters")
        result["excluded_count"] = excluded_count
        result["exclusion_note"] = (
            f"{excluded_count} otherwise-qualifying player(s) were left out of "
            "this list because they're already taken, per " + " and ".join(exclusion_sources) +
            ". value_over_replacement itself is still computed against the "
            "full player pool, so scarcity values don't swing as picks happen."
        )
    if league_id:
        result["league_id"] = league_id
        result["league_scoring_note"] = (
            "Ranked using this league's actual scoring_settings (best-effort key "
            "mapping for common categories) rather than the standard-PPR default."
        )
    if was_fallback:
        result["fallback_note"] = (
            f"Requested season data wasn't available yet; using {season_used} instead."
        )
    return result


@mcp.tool()
def mark_player_drafted(player_name: str, board: str = "default", reason: str | None = None) -> dict:
    """Remember that a player is no longer available, so future draft
    rankings stop suggesting them.

    This is local, persistent state — it's saved to disk on this machine
    and stays remembered across separate conversations/sessions, but it
    does NOT sync to any other device or to other people using this same
    plugin. Use it for things this tool has no other way of knowing: a
    player someone else drafted, a player who got cut, or anyone else you
    want excluded from `get_draft_rankings` regardless of the reason.

    (If you're tracking a real Sleeper league, pass that league's
    `league_id` into `get_draft_rankings` instead/in addition — it already
    excludes anyone actually rostered there automatically, without needing
    to be told player-by-player.)

    Args:
        player_name: Player's name. Fuzzy-matched.
        board: Name for this draft board — lets you track more than one
            draft at once. Defaults to "default"; pass the same name again
            to keep adding to the same board, or pass `board` into
            `get_draft_rankings` to apply the exclusions.
        reason: Optional short note, e.g. "drafted by Mike" or "cut by team".
    """
    return mark_player_status(player_name, unavailable=True, board=board, reason=reason)


@mcp.tool()
def mark_player_available(player_name: str, board: str = "default") -> dict:
    """Undo `mark_player_drafted` — remove a player from a draft board so
    they can appear in `get_draft_rankings` again.

    Use this if a player was marked unavailable by mistake, or if you're
    reusing a board name for a new draft and need to clear an old entry.

    Args:
        player_name: Player's name. Fuzzy-matched.
        board: The draft board name they were marked unavailable on.
            Defaults to "default".
    """
    return mark_player_status(player_name, unavailable=False, board=board)


@mcp.tool()
def get_draft_board(board: str = "default") -> dict:
    """List every player currently marked unavailable on a draft board.

    Args:
        board: Draft board name. Defaults to "default".
    """
    players = fd_get_draft_board(board)
    return {
        "board": board,
        "unavailable_players": players,
        "count": len(players),
        "note": (
            "This reflects local state saved on this machine via "
            "mark_player_drafted — it does not include real Sleeper league "
            "rosters (pass league_id into get_draft_rankings for that)."
        ),
    }


@mcp.tool()
def get_start_sit_outlook(player_name: str, week: int, season: int | None = None, league_id: str | None = None) -> dict:
    """Combine recent form, matchup difficulty, injury status, leaguewide
    add/drop sentiment, and recent news for one player ahead of a specific
    week, as the grounding for a start/sit call.

    This does not predict the future numerically. It gives you real
    signals — how the player has actually performed in games *before* this
    week, how favorable this week's matchup looks, their current Sleeper
    injury/depth-chart status, whether fantasy managers leaguewide are
    adding or dropping them, and what's currently being reported — so you
    can form and state a clear opinion (e.g. "trending down into a tough
    matchup with a questionable tag and getting dropped everywhere — sit
    him this week") instead of just reciting numbers.

    Args:
        player_name: Player's name. Fuzzy-matched.
        week: The upcoming (or in-question) NFL week number.
        season: NFL season year. Defaults to the current season, with
            automatic fallback if that season isn't published yet.
        league_id: Optional Sleeper league ID. When given, also returns a
            fantasy score for the player's recent games computed with that
            league's actual scoring settings instead of the standard-PPR
            default — useful if the league isn't standard PPR.
    """
    player, err = _resolve_or_error(player_name)
    if err:
        return err

    try:
        weekly, season_used, was_fallback = get_weekly_data_with_fallback(season or current_season())
    except RuntimeError as exc:
        return {"error": str(exc)}

    recent_form = recent_form_for(player["gsis_id"], weekly, before_week=week)
    matchup = matchup_difficulty_for(player, week, weekly, season_used)
    injury = sleeper_player_report(player.get("sleeper_id"))

    leaguewide_sentiment = None
    try:
        drops = get_trending("drop", lookback_hours=48, limit=200)
        sleeper_id = player.get("sleeper_id")
        sid = str(int(sleeper_id)) if sleeper_id and not pd.isna(sleeper_id) else None
        hit = next((d for d in drops if str(d.get("player_id")) == sid), None) if sid else None
        if hit:
            leaguewide_sentiment = {"trend": "being dropped", "drops_last_48h": hit.get("count")}
    except RuntimeError:
        pass  # leaguewide sentiment is a nice-to-have, not worth failing the whole call over

    try:
        news_items = fetch_nfl_news_items()
        name_lower = player["name"].strip().lower()
        news_matches = [
            it for it in news_items
            if name_lower in it["title"].lower() or name_lower in it["summary"].lower()
        ][:5]
    except Exception as exc:  # noqa: BLE001
        news_matches = []
        news_error = f"Could not fetch news: {exc}"
    else:
        news_error = None

    # Confidence policy: flag when the honest answer is "it depends," not a
    # single verdict — an uncertain injury tag, or news recent enough that
    # the situation may still be developing.
    contingency_reasons = []
    if injury and injury.get("injury_status") in ("Questionable", "Doubtful"):
        contingency_reasons.append(
            f"injury_status is '{injury['injury_status']}' — outcome isn't settled yet"
        )
    recent_breaking_news = [it for it in news_matches if it.get("is_recent")]
    if recent_breaking_news:
        contingency_reasons.append(
            f"{len(recent_breaking_news)} headline(s) published in roughly the last "
            "12 hours — the situation may still be developing"
        )
    requires_contingent_advice = bool(contingency_reasons)

    result = {
        "player": player["name"],
        "position": player.get("position"),
        "week": week,
        "season_used": season_used,
        "recent_form": recent_form,
        "matchup": matchup,
        "recent_news": news_matches,
        "fetched_at": now_utc_iso(),
        "requires_contingent_advice": requires_contingent_advice,
        "guidance": (
            "Weigh recent_form, matchup, injury_report, leaguewide_sentiment, "
            "and recent_news together and state a clear start/sit opinion — "
            "don't just restate the numbers. Trending up + a favorable matchup "
            "+ a clean injury report + no concerning news supports starting "
            "even on a small sample. Trending down, a tough matchup, a "
            "doubtful tag, being widely dropped, or news of a diminished role "
            "(benching, committee backfield, target competition) supports "
            "sitting. An injury_status of 'Out' or 'IR' is close to a hard "
            "stop regardless of the other signals. If recent_form shows no "
            "prior games this season, lean more heavily on matchup, injury "
            "status, and the player's established role.\n\n"
            "When requires_contingent_advice is true (see contingency_reasons), "
            "do NOT give a single flat verdict. Give contingent advice instead: "
            "'start him if [condition — e.g. he's active/full-go by kickoff], "
            "otherwise go with [alternative]' — and say which contingency_reason "
            "is driving that. If you don't know the user's bench alternative, "
            "ask, rather than silently picking one."
        ),
    }
    if requires_contingent_advice:
        result["contingency_reasons"] = contingency_reasons
    if injury:
        result["injury_report"] = injury
    if leaguewide_sentiment:
        result["leaguewide_sentiment"] = leaguewide_sentiment
    if news_error:
        result["news_fetch_error"] = news_error
    if was_fallback:
        result["fallback_note"] = (
            f"Requested season data wasn't available yet; using {season_used} instead."
        )

    if league_id:
        try:
            league = get_league(league_id)
            scoring_settings = league.get("scoring_settings", {})
            weekly_rows = weekly[
                (weekly["player_id"] == player["gsis_id"]) & (weekly["week"] < week)
            ].sort_values("week")
            league_scores = [
                {
                    "week": int(r["week"]),
                    "league_scored_points": compute_league_fantasy_points(
                        normalize_stat_row(r), scoring_settings
                    ),
                }
                for _, r in weekly_rows.tail(3).iterrows()
            ]
            result["league_scoring"] = {
                "league_id": league_id,
                "scoring_settings": scoring_settings,
                "recent_games_league_scored": league_scores,
                "note": (
                    "Points recomputed using this league's actual scoring_settings "
                    "(best-effort key mapping for common categories — check "
                    "scoring_settings directly for unusual bonus categories this "
                    "may not capture) instead of the standard-PPR default used "
                    "elsewhere in this tool."
                ),
            }
        except RuntimeError as exc:
            result["league_scoring_error"] = str(exc)

    return result


@mcp.tool()
def get_league_scoring_settings(league_id: str) -> dict:
    """Get a Sleeper league's actual scoring settings and roster requirements.

    Use this to check whether a league is really standard PPR (this
    plugin's default assumption) or uses something different (0.5 PPR, TE
    premium, six-point passing TDs, etc.) before relying on rankings or
    scores computed with the standard-PPR default.

    Args:
        league_id: The Sleeper league ID (found in the league's URL).
    """
    try:
        league = get_league(league_id)
    except RuntimeError as exc:
        return {"error": str(exc)}

    if not isinstance(league, dict) or "scoring_settings" not in league:
        return {"error": f"Unexpected response for league '{league_id}' — check the league ID."}

    return {
        "league_id": league_id,
        "name": league.get("name"),
        "season": league.get("season"),
        "roster_positions": league.get("roster_positions"),
        "scoring_settings": league.get("scoring_settings"),
        "fetched_at": now_utc_iso(),
    }


if __name__ == "__main__":
    # Local plugin install (the default, and what most people use): talk
    # stdio to whatever Claude client launched this process.
    #
    # Self-hosted remote deployment (see deploy/README.md): set
    # MCP_TRANSPORT=http and this binds an actual web server instead, so it
    # can be added to Claude as a custom connector and reached from any
    # device, including mobile. PORT is read because that's the convention
    # hosts like Render/Railway use to tell your app which port to bind.
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        port = int(os.environ.get("PORT", 8000))
        mcp.run(transport="http", host="0.0.0.0", port=port)
