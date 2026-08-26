"""
Shared data-access helpers for the fantasy-football-agent MCP server.

Everything here fetches LIVE from free public sources at call time:
  - nfl_data_py (wraps nflverse/nflfastR) for historical/weekly NFL stats
  - Sleeper's public API for rosters, trending adds, and league state
  - ESPN's public NFL news RSS feed for recent headlines

There is no database and no hosted backend. Results for a given season/week
are cached in memory for the lifetime of the server process only, so repeat
calls within one Claude session don't re-download the same data.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import nfl_data_py as nfl
import pandas as pd
import requests

SLEEPER_BASE = "https://api.sleeper.app/v1"
ESPN_NFL_NEWS_RSS = "https://www.espn.com/espn/rss/nfl/news"
REQUEST_TIMEOUT = 15

# Sleeper asks integrations not to hit /players/nfl (a multi-MB dump) more
# than once a day, so it's cached to disk rather than re-fetched per call.
SLEEPER_PLAYERS_CACHE_PATH = Path.home() / ".cache" / "fantasy-football-agent" / "sleeper_players.json"
SLEEPER_PLAYERS_CACHE_TTL_SECONDS = 20 * 60 * 60  # ~20 hours

# Unlike the caches above (which just avoid re-fetching public data), the
# draft board is real user state — who's been marked drafted/unavailable —
# so it's never expired on its own.
#
# Where it actually lives depends on how this server is running:
#   - Local plugin (stdio, the normal install path): a JSON file on disk,
#     scoped to whichever machine is running the MCP server. It persists
#     across sessions on that machine, but doesn't sync between devices or
#     between different people's installs.
#   - Self-hosted remote deployment (see deploy/README.md): if
#     UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN are set, the board
#     is stored in Upstash Redis instead. This matters specifically because
#     free-tier hosts like Render have an EPHEMERAL filesystem — anything
#     written to local disk is wiped on every restart/spin-down — so the
#     local-file approach would silently lose the board on exactly the kind
#     of host most people would pick for a free personal deployment.
DRAFT_BOARD_PATH = Path.home() / ".local" / "share" / "fantasy-football-agent" / "draft_boards.json"

UPSTASH_REDIS_REST_URL = os.environ.get("UPSTASH_REDIS_REST_URL")
UPSTASH_REDIS_REST_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
UPSTASH_DRAFT_BOARD_KEY = "fantasy-football-agent:draft_boards"

# Optional: real forward-looking projections (get_draft_projections), instead
# of get_draft_rankings' past-actual-production-only view. Off by default —
# nothing else in this plugin requires it. Get a free key (personal,
# non-commercial use) at https://www.fantasypros.com/api-data/ and set it as
# an environment variable to turn this on. Each person running their own
# copy of this server should use their own key rather than sharing one,
# both because FantasyPros' free tier is scoped to personal use and to
# avoid everyone sharing one rate limit.
FANTASYPROS_API_KEY = os.environ.get("FANTASYPROS_API_KEY")
# NOTE: the path really does include "/public/" — easy to miss (it's absent
# from the hosted docs page at api.fantasypros.com/v2/docs), but confirmed
# against FantasyPros' own actively-maintained open-source PHP client
# (github.com/FantasyPHP/fantasypros), which hits this exact base URL.
# Hitting the URL without "/public/" returns a generic 403 {"message":
# "Forbidden"} that looks identical to an invalid-key error but isn't one.
FANTASYPROS_BASE = "https://api.fantasypros.com/public/v2/json/nfl"


# ---------------------------------------------------------------------------
# Freshness helpers
# ---------------------------------------------------------------------------
#
# Every tool in this plugin fetches live, but "live" doesn't mean "the same
# second" — the Sleeper player directory is disk-cached for ~20 hours (see
# below), and news items carry their own publish time. Rather than let a
# caller assume everything is instantaneous, every timestamp-sensitive
# response surfaces exactly how fresh (or stale) its data actually is.

def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def humanize_age(seconds: float) -> str:
    """Turn an age in seconds into a short human string like '12 minutes
    ago' or '3 hours ago', for surfacing data freshness to the end user."""
    seconds = max(0, seconds)
    if seconds < 90:
        return "just now"
    minutes = seconds / 60
    if minutes < 90:
        return f"{int(round(minutes))} minute{'s' if round(minutes) != 1 else ''} ago"
    hours = minutes / 60
    if hours < 36:
        return f"{int(round(hours))} hour{'s' if round(hours) != 1 else ''} ago"
    days = hours / 24
    return f"{int(round(days))} day{'s' if round(days) != 1 else ''} ago"


def parse_rss_pubdate(pub_date: str) -> datetime | None:
    """Parse an RSS pubDate (RFC 2822, e.g. 'Mon, 25 Aug 2026 14:03:00 GMT')
    into an aware datetime. Returns None if it can't be parsed rather than
    raising — freshness display is a nice-to-have, not worth failing over."""
    if not pub_date:
        return None
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(pub_date)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Season / week helpers
# ---------------------------------------------------------------------------

def current_season() -> int:
    """Best-guess 'current' NFL season year.

    NFL seasons are named for the year they start (e.g. games from Sept 2025
    through Feb 2026 are all "season 2025"). During the off-season (Mar-Aug),
    fall back to the most recently completed season so stat lookups return
    real data instead of an empty season.
    """
    now = datetime.now(timezone.utc)
    if now.month >= 9:
        return now.year
    if now.month <= 2:
        return now.year - 1
    return now.year - 1


def parse_weeks(weeks: str | None) -> list[int] | None:
    """Parse a weeks argument like '1-5', '1,2,3', or '7' into a list of ints.

    Returns None if weeks is falsy, meaning "all available weeks".
    """
    if not weeks:
        return None
    weeks = weeks.strip()
    if "-" in weeks:
        start, end = weeks.split("-", 1)
        return list(range(int(start), int(end) + 1))
    return [int(w) for w in weeks.split(",") if w.strip()]


# ---------------------------------------------------------------------------
# nfl_data_py-backed lookups (cached per season for this process's lifetime)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=8)
def get_weekly_data(season: int) -> pd.DataFrame:
    # downcast=False: downcasting to float32 produces ugly rounding artifacts
    # (e.g. 13.6 -> 13.600000381469727) once converted back to plain floats.
    return nfl.import_weekly_data([season], downcast=False)


@lru_cache(maxsize=8)
def get_seasonal_data(season: int) -> pd.DataFrame:
    return nfl.import_seasonal_data([season], s_type="REG")


# --- Fallback path: compute weekly stats directly from play-by-play -------
#
# nflverse's pre-aggregated "player_stats" release (what get_weekly_data
# above hits) sometimes lags behind the raw play-by-play release for the
# current season — e.g. it can 404 for a season that's already fully
# played, while play_by_play_{season}.parquet for the same season is
# already complete. Rather than falling back to an older, less relevant
# season whenever that happens, compute the same stat columns ourselves
# from play-by-play, so the current season is always usable once games for
# it exist at all.

_WEEKLY_STATS_COLUMNS = [
    "player_id", "player_display_name", "position", "recent_team", "season", "week",
    "opponent_team", "receptions", "targets", "receiving_yards", "receiving_tds",
    "rushing_yards", "rushing_tds", "passing_yards", "passing_tds",
    "interceptions", "fumbles_lost", "fantasy_points", "fantasy_points_ppr",
]


@lru_cache(maxsize=8)
def get_pbp_data(season: int) -> pd.DataFrame:
    url = f"https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.parquet"
    cols = [
        "week", "season", "posteam", "defteam", "season_type",
        "receiver_player_id", "receiver_player_name", "complete_pass", "receiving_yards",
        "rusher_player_id", "rusher_player_name", "rushing_yards",
        "passer_player_id", "passer_player_name", "passing_yards",
        "pass_touchdown", "rush_touchdown", "interception",
        "fumble_lost", "fumbled_1_player_id",
    ]
    return pd.read_parquet(url, columns=cols)


@lru_cache(maxsize=8)
def build_weekly_stats_from_pbp(season: int) -> pd.DataFrame:
    """Aggregate play-by-play into the same per-player-week shape as
    nflverse's official weekly stats file, including a PPR fantasy score
    computed with a standard scoring formula (1 pt/reception, 1 pt/10 rush
    or rec yard, 6 pts/rush or rec TD, 1 pt/25 pass yard, 4 pts/pass TD,
    -2 per interception or lost fumble). This is a close approximation of
    standard PPR scoring, not a bit-for-bit match to any one platform's
    exact house rules (return TDs and two-point conversions aren't
    included).
    """
    pbp = get_pbp_data(season)
    pbp = pbp[pbp["season_type"] == "REG"]

    receiving = (
        pbp[pbp["receiver_player_id"].notna()]
        .groupby(["receiver_player_id", "week"])
        .agg(
            targets=("receiver_player_id", "count"),
            receptions=("complete_pass", "sum"),
            receiving_yards=("receiving_yards", "sum"),
            receiving_tds=("pass_touchdown", "sum"),
            recent_team=("posteam", "first"),
            opponent_team=("defteam", "first"),
            player_display_name=("receiver_player_name", "first"),
        )
        .reset_index()
        .rename(columns={"receiver_player_id": "player_id"})
    )

    rushing = (
        pbp[pbp["rusher_player_id"].notna()]
        .groupby(["rusher_player_id", "week"])
        .agg(
            rushing_yards=("rushing_yards", "sum"),
            rushing_tds=("rush_touchdown", "sum"),
            recent_team=("posteam", "first"),
            opponent_team=("defteam", "first"),
            player_display_name=("rusher_player_name", "first"),
        )
        .reset_index()
        .rename(columns={"rusher_player_id": "player_id"})
    )

    passing = (
        pbp[pbp["passer_player_id"].notna()]
        .groupby(["passer_player_id", "week"])
        .agg(
            passing_yards=("passing_yards", "sum"),
            passing_tds=("pass_touchdown", "sum"),
            interceptions=("interception", "sum"),
            recent_team=("posteam", "first"),
            opponent_team=("defteam", "first"),
            player_display_name=("passer_player_name", "first"),
        )
        .reset_index()
        .rename(columns={"passer_player_id": "player_id"})
    )

    fumbles = (
        pbp[(pbp["fumble_lost"] == 1) & (pbp["fumbled_1_player_id"].notna())]
        .groupby(["fumbled_1_player_id", "week"])
        .size()
        .reset_index(name="fumbles_lost")
        .rename(columns={"fumbled_1_player_id": "player_id"})
    )

    merged = receiving.merge(rushing, on=["player_id", "week"], how="outer", suffixes=("", "_rush"))
    merged = merged.merge(passing, on=["player_id", "week"], how="outer", suffixes=("", "_pass"))
    merged = merged.merge(fumbles, on=["player_id", "week"], how="outer")

    # Coalesce the team/name columns that may have come from multiple sides.
    for base in ["recent_team", "opponent_team", "player_display_name"]:
        cols = [c for c in merged.columns if c == base or c.startswith(f"{base}_")]
        merged[base] = merged[cols].bfill(axis=1).iloc[:, 0]
        drop_cols = [c for c in cols if c != base]
        merged = merged.drop(columns=drop_cols)

    numeric_cols = [
        "targets", "receptions", "receiving_yards", "receiving_tds",
        "rushing_yards", "rushing_tds", "passing_yards", "passing_tds",
        "interceptions", "fumbles_lost",
    ]
    for col in numeric_cols:
        if col not in merged.columns:
            merged[col] = 0.0
        merged[col] = merged[col].fillna(0.0)

    merged["fantasy_points_ppr"] = (
        merged["receptions"] * 1.0
        + merged["receiving_yards"] / 10.0
        + merged["receiving_tds"] * 6.0
        + merged["rushing_yards"] / 10.0
        + merged["rushing_tds"] * 6.0
        + merged["passing_yards"] / 25.0
        + merged["passing_tds"] * 4.0
        - merged["interceptions"] * 2.0
        - merged["fumbles_lost"] * 2.0
    )
    merged["fantasy_points"] = merged["fantasy_points_ppr"] - merged["receptions"] * 1.0
    merged["season"] = season

    positions = get_id_crosswalk()[["gsis_id", "position"]].dropna(subset=["gsis_id"]).drop_duplicates(subset=["gsis_id"])
    merged = merged.merge(positions, left_on="player_id", right_on="gsis_id", how="left")

    for col in _WEEKLY_STATS_COLUMNS:
        if col not in merged.columns:
            merged[col] = pd.NA

    return merged[_WEEKLY_STATS_COLUMNS]


def get_weekly_data_with_fallback(preferred_season: int, max_back: int = 3) -> tuple[pd.DataFrame, int, bool]:
    """Fetch weekly data for `preferred_season`.

    Tries, in order: (1) nflverse's official pre-aggregated file for that
    season, (2) computing the same stats from play-by-play for that same
    season (covers the case where the aggregate file lags the raw data),
    (3) stepping back to earlier seasons only as a last resort.

    Returns (dataframe, season_actually_used, was_fallback) — was_fallback
    is True only if an earlier season had to be used, NOT when the
    pbp-computed path was used for the originally requested season.
    """
    last_exc: Exception | None = None
    for offset in range(max_back):
        season = preferred_season - offset
        try:
            df = get_weekly_data(season)
            if not df.empty:
                return df, season, offset > 0
        except Exception as exc:  # noqa: BLE001
            last_exc = exc

        try:
            df = build_weekly_stats_from_pbp(season)
            if not df.empty:
                return df, season, offset > 0
        except Exception as exc:  # noqa: BLE001
            last_exc = exc

    raise RuntimeError(
        f"Could not fetch weekly stats (official or play-by-play-derived) "
        f"for {preferred_season} or the {max_back - 1} season(s) before "
        f"it. Last error: {last_exc}"
    )


def get_seasonal_data_with_fallback(preferred_season: int, max_back: int = 3) -> tuple[pd.DataFrame, int, bool]:
    """Same idea as get_weekly_data_with_fallback, for full-season totals.
    When the official seasonal file is unavailable, sums the pbp-derived
    weekly stats for that season instead of skipping straight to an
    earlier year.
    """
    last_exc: Exception | None = None
    for offset in range(max_back):
        season = preferred_season - offset
        try:
            df = get_seasonal_data(season)
            if not df.empty:
                return df, season, offset > 0
        except Exception as exc:  # noqa: BLE001
            last_exc = exc

        try:
            weekly = build_weekly_stats_from_pbp(season)
            if not weekly.empty:
                seasonal = (
                    weekly.groupby("player_id")
                    .agg(
                        player_display_name=("player_display_name", "first"),
                        recent_team=("recent_team", "last"),
                        fantasy_points=("fantasy_points", "sum"),
                        fantasy_points_ppr=("fantasy_points_ppr", "sum"),
                        receptions=("receptions", "sum"),
                        receiving_yards=("receiving_yards", "sum"),
                        receiving_tds=("receiving_tds", "sum"),
                        rushing_yards=("rushing_yards", "sum"),
                        rushing_tds=("rushing_tds", "sum"),
                        passing_yards=("passing_yards", "sum"),
                        passing_tds=("passing_tds", "sum"),
                        interceptions=("interceptions", "sum"),
                        fumbles_lost=("fumbles_lost", "sum"),
                    )
                    .reset_index()
                )
                return seasonal, season, offset > 0
        except Exception as exc:  # noqa: BLE001
            last_exc = exc

    raise RuntimeError(
        f"Could not fetch seasonal stats (official or play-by-play-derived) "
        f"for {preferred_season} or the {max_back - 1} season(s) before "
        f"it. Last error: {last_exc}"
    )


@lru_cache(maxsize=1)
def get_id_crosswalk() -> pd.DataFrame:
    """Name/position/team/gsis_id/sleeper_id crosswalk, used to resolve a
    player name typed by the user to the IDs each data source expects."""
    return nfl.import_ids()


@lru_cache(maxsize=1)
def get_schedules_for_current_and_next() -> pd.DataFrame | None:
    """Best-effort schedule fetch for matchup lookups. Returns None (rather
    than raising) if the schedule source is unreachable, since matchup
    difficulty can still work from weekly opponent data for past weeks."""
    try:
        season = current_season()
        return nfl.import_schedules([season])
    except Exception:
        return None


def resolve_player(player_name: str) -> dict | list[dict] | None:
    """Resolve a free-text player name to a single crosswalk row.

    Returns:
      - dict: a single unambiguous match
      - list[dict]: multiple plausible matches (caller should ask which one)
      - None: no match found
    """
    ids = get_id_crosswalk()
    ids = ids.dropna(subset=["name"])
    query = player_name.strip().lower()

    exact = ids[ids["name"].str.lower() == query]
    if len(exact) == 1:
        return exact.iloc[0].to_dict()

    contains = ids[ids["name"].str.lower().str.contains(re.escape(query), na=False)]
    if len(contains) == 1:
        return contains.iloc[0].to_dict()
    if len(contains) > 1:
        # Prefer rows that have a gsis_id (needed for stats lookups) and are
        # on an active roster (team not null), most-recently-relevant first.
        contains = contains.dropna(subset=["gsis_id"])
        if len(contains) == 1:
            return contains.iloc[0].to_dict()
        if len(contains) > 1:
            return contains.head(5).to_dict("records")

    return None


# ---------------------------------------------------------------------------
# Draft board — persisted, user-controlled "don't recommend this player"
# state. This is the only state this plugin keeps beyond in-memory/disk
# CACHES of public data; it's real user data, so it's never auto-expired.
# ---------------------------------------------------------------------------

def _using_remote_draft_board_storage() -> bool:
    return bool(UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN)


def _load_draft_boards() -> dict:
    if _using_remote_draft_board_storage():
        try:
            resp = requests.get(
                f"{UPSTASH_REDIS_REST_URL}/get/{UPSTASH_DRAFT_BOARD_KEY}",
                headers={"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            raw = resp.json().get("result")
            return json.loads(raw) if raw else {}
        except Exception:  # noqa: BLE001 - a storage hiccup shouldn't crash the tool
            return {}

    try:
        if DRAFT_BOARD_PATH.exists():
            with DRAFT_BOARD_PATH.open("r") as f:
                return json.load(f)
    except Exception:  # noqa: BLE001 - a corrupt file shouldn't crash the tool
        pass
    return {}


def _save_draft_boards(data: dict) -> None:
    if _using_remote_draft_board_storage():
        requests.post(
            f"{UPSTASH_REDIS_REST_URL}/set/{UPSTASH_DRAFT_BOARD_KEY}",
            headers={"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"},
            data=json.dumps(data),
            timeout=REQUEST_TIMEOUT,
        ).raise_for_status()
        return

    DRAFT_BOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DRAFT_BOARD_PATH.open("w") as f:
        json.dump(data, f, indent=2)


def mark_player_status(player_name: str, unavailable: bool, board: str = "default", reason: str | None = None) -> dict:
    """Mark a player drafted/unavailable (unavailable=True) or put them
    back in play (unavailable=False) on a named board. Resolves the name
    through the same crosswalk every other tool uses, so it lines up with
    what get_draft_rankings will actually filter against."""
    player = resolve_player(player_name)
    if player is None:
        return {"error": f"No player found matching '{player_name}'."}
    if isinstance(player, list):
        return {
            "error": "Multiple players match that name — ask the user which one.",
            "candidates": [f"{c.get('name')} ({c.get('position')}, {c.get('team')})" for c in player],
        }

    boards = _load_draft_boards()
    board_data = boards.setdefault(board, {})

    key = player.get("gsis_id") or player["name"]
    if unavailable:
        board_data[key] = {
            "name": player["name"],
            "position": player.get("position"),
            "team": player.get("team"),
            "reason": reason or "drafted",
            "marked_at": now_utc_iso(),
        }
    else:
        board_data.pop(key, None)

    boards[board] = board_data
    _save_draft_boards(boards)
    return {
        "player": player["name"],
        "board": board,
        "status": "unavailable" if unavailable else "available",
    }


def get_draft_board(board: str = "default") -> list[dict]:
    """Everyone currently marked unavailable on a board."""
    boards = _load_draft_boards()
    return list(boards.get(board, {}).values())


def draft_board_exclusion_keys(board: str = "default") -> tuple[set, set]:
    """(gsis_ids, names) currently marked unavailable on a board — most
    entries are keyed by gsis_id, but a player with no gsis_id on file
    falls back to being keyed by name, so filtering needs to check both."""
    boards = _load_draft_boards()
    board_data = boards.get(board, {})
    gsis_ids = {k for k in board_data.keys() if k and k.startswith("00-")}
    names = {v["name"] for k, v in board_data.items() if not k.startswith("00-")}
    return gsis_ids, names


# ---------------------------------------------------------------------------
# Sleeper API helpers
# ---------------------------------------------------------------------------

def _http_get_json(url: str) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": "fantasy-football-agent/0.1"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def sleeper_get(path: str) -> dict | list:
    """GET a Sleeper API path (no auth required). Raises a clear error on
    failure rather than a raw urllib traceback."""
    try:
        return _http_get_json(f"{SLEEPER_BASE}{path}")
    except Exception as exc:  # noqa: BLE001 - surface a clean tool error
        raise RuntimeError(f"Could not reach Sleeper API ({path}): {exc}") from exc


@lru_cache(maxsize=1)
def get_sleeper_players() -> dict:
    """Sleeper's full player directory, keyed by sleeper_id. Includes
    injury_status, depth_chart_position/order, and roster status — not
    otherwise available from nfl_data_py. This is a multi-MB response, so
    it's cached to disk for ~20 hours (Sleeper's own guidance is not to
    pull it more than about once a day) and reused across server restarts,
    not just within one process.
    """
    cache_path = SLEEPER_PLAYERS_CACHE_PATH
    try:
        if cache_path.exists():
            age = time.time() - cache_path.stat().st_mtime
            if age < SLEEPER_PLAYERS_CACHE_TTL_SECONDS:
                with cache_path.open("r") as f:
                    return json.load(f)
    except Exception:  # noqa: BLE001 - a bad cache file just means re-fetch
        pass

    data = sleeper_get("/players/nfl")
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("w") as f:
            json.dump(data, f)
    except Exception:  # noqa: BLE001 - caching is best-effort, not required
        pass
    return data


def get_sleeper_players_freshness() -> dict | None:
    """How old the on-disk Sleeper player-directory cache is right now.
    This matters because injury_status can be up to ~20h stale (the cache
    TTL) — callers should surface this rather than imply injury data is
    always current-to-the-second."""
    cache_path = SLEEPER_PLAYERS_CACHE_PATH
    try:
        mtime = cache_path.stat().st_mtime
    except OSError:
        return None
    age_seconds = time.time() - mtime
    return {
        "as_of": datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "age": humanize_age(age_seconds),
    }


def sleeper_player_report(sleeper_id: str | float | None) -> dict | None:
    """Injury/roster-status snapshot for one player, from Sleeper's player
    directory, plus how stale that snapshot is. Returns None if the player
    can't be found or has no sleeper_id on file."""
    if sleeper_id is None or (isinstance(sleeper_id, float) and pd.isna(sleeper_id)):
        return None
    key = str(int(sleeper_id)) if isinstance(sleeper_id, float) else str(sleeper_id)
    try:
        players = get_sleeper_players()
    except RuntimeError:
        return None
    p = players.get(key)
    if not p:
        return None
    report = {
        "status": p.get("status"),
        "injury_status": p.get("injury_status"),
        "injury_body_part": p.get("injury_body_part"),
        "depth_chart_position": p.get("depth_chart_position"),
        "depth_chart_order": p.get("depth_chart_order"),
    }
    freshness = get_sleeper_players_freshness()
    if freshness:
        report["as_of"] = freshness["as_of"]
        report["data_age"] = freshness["age"]
    return report


def get_trending(direction: str = "add", lookback_hours: int = 24, limit: int = 25) -> list:
    """Sleeper's trending players feed. direction is 'add' or 'drop'."""
    if direction not in ("add", "drop"):
        raise ValueError("direction must be 'add' or 'drop'")
    return sleeper_get(f"/players/nfl/trending/{direction}?lookback_hours={lookback_hours}&limit={limit}")


# ---------------------------------------------------------------------------
# League scoring settings (per-league, not global)
# ---------------------------------------------------------------------------

def get_league(league_id: str) -> dict:
    # Deliberately NOT cached (unlike the player directory): league settings
    # are a small, cheap request, and callers report a `fetched_at` for this
    # data — caching it in-process would make that timestamp a lie.
    return sleeper_get(f"/league/{league_id}")


# Best-effort mapping from Sleeper's scoring_settings keys to the raw stat
# categories this plugin already computes per player-week. Sleeper doesn't
# publicly document the exact key list, so this covers the commonly-used
# offensive skill-position keys; anything not present in a league's
# settings is simply treated as 0 (no effect), and the raw scoring_settings
# is always returned alongside so a mismatch is visible rather than silent.
_SLEEPER_SCORING_KEY_MAP = {
    "receptions": "rec",
    "receiving_yards": "rec_yd",
    "receiving_tds": "rec_td",
    "rushing_yards": "rush_yd",
    "rushing_tds": "rush_td",
    "passing_yards": "pass_yd",
    "passing_tds": "pass_td",
    "interceptions": "pass_int",
    "fumbles_lost": "fum_lost",
}
# Sleeper's rec_yd / rush_yd / pass_yd settings are points-per-yard (e.g.
# 0.1 = 1 pt per 10 yards), applied directly, not divided further.


def normalize_stat_row(row) -> dict:
    """Extract the raw stat categories `compute_league_fantasy_points` needs
    from either an official nflverse row or a pbp-derived row — they name
    fumbles-lost differently (nflverse splits it by rushing/receiving/sack;
    the pbp-derived path already combines it into one `fumbles_lost`)."""
    fumbles_lost = row.get("fumbles_lost")
    if fumbles_lost is None:
        fumbles_lost = (
            (row.get("rushing_fumbles_lost", 0) or 0)
            + (row.get("receiving_fumbles_lost", 0) or 0)
            + (row.get("sack_fumbles_lost", 0) or 0)
        )
    return {
        "receptions": row.get("receptions", 0) or 0,
        "receiving_yards": row.get("receiving_yards", 0) or 0,
        "receiving_tds": row.get("receiving_tds", 0) or 0,
        "rushing_yards": row.get("rushing_yards", 0) or 0,
        "rushing_tds": row.get("rushing_tds", 0) or 0,
        "passing_yards": row.get("passing_yards", 0) or 0,
        "passing_tds": row.get("passing_tds", 0) or 0,
        "interceptions": row.get("interceptions", 0) or 0,
        "fumbles_lost": fumbles_lost,
    }


def compute_league_fantasy_points(stat_row: dict, scoring_settings: dict) -> float:
    """Compute a fantasy score for one player-week using a real league's
    scoring_settings instead of this plugin's built-in standard-PPR
    assumption. Best-effort: unrecognized or missing settings keys
    contribute 0, so this may undercount leagues with unusual bonus
    categories (TE premium, IDP, etc.) — always shown alongside the raw
    scoring_settings so that can be checked.
    """
    total = 0.0
    for stat_key, sleeper_key in _SLEEPER_SCORING_KEY_MAP.items():
        value = stat_row.get(stat_key, 0) or 0
        points_per_unit = scoring_settings.get(sleeper_key, 0) or 0
        total += value * points_per_unit
    return round(total, 2)


# ---------------------------------------------------------------------------
# FantasyPros projections (optional — requires FANTASYPROS_API_KEY)
# ---------------------------------------------------------------------------
#
# Unlike everything above, these are forward-looking: FantasyPros aggregates
# real analyst projections, which is the only way this plugin can say
# anything useful about a rookie (nflverse/Sleeper only have stats for games
# that already happened, so a rookie with zero NFL games has nothing to
# compute from). This is intentionally kept separate from the nflverse/
# Sleeper-backed tools above — if no key is configured, callers should get a
# clear "not configured" error, not a crash, and can fall back to
# get_draft_rankings.

def fantasypros_available() -> bool:
    return bool(FANTASYPROS_API_KEY)


# FantasyPros' own docs example for this endpoint is
# "GET .../nfl/2026/projections?position=WR" — a single real position value,
# not "ALL" or a colon-delimited list (both of those were guesses against an
# unofficial client's enum, and evidently wrong: they returned zero players
# with no error). One call per real position, confirmed against the docs, is
# more requests but is what's actually documented to work.
_FANTASYPROS_POSITIONS = ["QB", "RB", "WR", "TE", "K", "DST"]


def get_fantasypros_projections(season: int, scoring: str = "PPR", week: int = 0) -> pd.DataFrame:
    """All-position FantasyPros projections for one season/week, normalized
    into a flat DataFrame (name/position/team/points columns).

    week=0 is FantasyPros' convention for preseason/full-season projections
    (what draft-time rankings want); pass a specific week for in-season
    weekly projections instead.

    Makes one request per position in _FANTASYPROS_POSITIONS (each cached
    individually — see _fetch_fantasypros_projections_for_position) and
    concatenates the results. If every position fails, raises RuntimeError
    with the first underlying error; if only some fail, returns what
    succeeded rather than losing everything to one bad position.
    """
    frames = []
    first_error = None
    for position in _FANTASYPROS_POSITIONS:
        try:
            frames.append(_fetch_fantasypros_projections_for_position(season, position, scoring, week))
        except RuntimeError as exc:
            if first_error is None:
                first_error = str(exc)

    if not frames:
        raise RuntimeError(
            "FantasyPros projections failed for every position. First "
            f"error: {first_error}"
        )
    return pd.concat(frames, ignore_index=True)


@lru_cache(maxsize=64)
def _fetch_fantasypros_projections_for_position(season: int, position: str, scoring: str = "PPR", week: int = 0) -> pd.DataFrame:
    """Projections for ONE position (FantasyPros requires "position" to be a
    real value like "WR", not "ALL" — see get_fantasypros_projections).

    Cached per (season, position, scoring, week) for this process's
    lifetime — projections don't meaningfully change second-to-second, so
    there's no reason to re-hit the API (and its rate limit) on every call
    within one session, same reasoning as the nflverse caching above.

    Raises RuntimeError (not a crash) if no key is configured or the request
    fails, so callers can present a clean error/fallback instead.
    """
    if not FANTASYPROS_API_KEY:
        raise RuntimeError(
            "FANTASYPROS_API_KEY is not set — get a free key at "
            "https://www.fantasypros.com/api-data/ and set it as an "
            "environment variable to enable real season projections."
        )

    url = f"{FANTASYPROS_BASE}/{season}/projections"
    params = {
        "season": season,
        "week": week,
        "scoring": scoring,
        "position": position,
    }
    try:
        resp = requests.get(
            url,
            headers={
                "x-api-key": FANTASYPROS_API_KEY,
                # Without an explicit User-Agent, `requests` sends its own
                # default ("python-requests/x.y"), which is a widely
                # blocklisted signature on bot-protection/WAF layers (a
                # bare '{"message":"Forbidden"}' with no other detail, as
                # seen from this API, is the classic fingerprint of that
                # kind of block rather than an actual invalid-key error).
                "User-Agent": "fantasy-football-agent/0.1 (+https://github.com/)",
                "Accept": "application/json",
            },
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        body = ""
        if exc.response is not None:
            body = exc.response.text[:300].strip()
        raise RuntimeError(
            f"FantasyPros API returned HTTP {status} for position={position}"
            + (f" — response body: {body}" if body else "")
            + ". Check that FANTASYPROS_API_KEY is valid, active, and "
            "hasn't hit its rate limit."
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Could not reach FantasyPros API for position={position}: {exc}") from exc

    players = data.get("players")
    if players is None:
        raise RuntimeError(
            f"FantasyPros API response for position={position} didn't "
            "include a 'players' list — the API format may have changed "
            "since this was written."
        )

    if not players:
        # Surface the envelope's own metadata rather than just saying
        # "empty" — `count` tells us whether the API thinks any players
        # match this query at all (0 = the filter itself is wrong) versus
        # matching players existing but being withheld (a plan/tier cap,
        # most likely surfaced via `limits`).
        raise RuntimeError(
            f"FantasyPros API returned zero players for position={position}, "
            "even though the request succeeded. Diagnostic info from the "
            f"response — count: {data.get('count')!r}, scoring: "
            f"{data.get('scoring')!r}, limits: {data.get('limits')!r}, "
            f"full top-level keys: {sorted(data.keys())!r}."
        )

    rows = []
    for p in players:
        stats = p.get("stats") or {}
        rows.append(
            {
                "name": p.get("name"),
                "position": p.get("position_id"),
                "team": p.get("team_id"),
                "points": stats.get("points"),
                "points_ppr": stats.get("points_ppr"),
                "points_half": stats.get("points_half"),
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# News (public RSS, no API key)
# ---------------------------------------------------------------------------

def fetch_nfl_news_items(max_items: int = 40) -> list[dict]:
    """Pull recent headlines from ESPN's public NFL news RSS feed. Each item
    carries both its raw publish timestamp and a human-readable relative
    age ("2 hours ago"), computed against the moment of this fetch — so a
    caller can tell a same-day update from three-day-old news at a glance.
    """
    req = urllib.request.Request(
        ESPN_NFL_NEWS_RSS, headers={"User-Agent": "fantasy-football-agent/0.1"}
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        raw = resp.read()

    fetch_time = datetime.now(timezone.utc)
    root = ET.fromstring(raw)
    items = []
    for item in root.findall(".//item")[:max_items]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        description = (item.findtext("description") or "").strip()
        entry = {"title": title, "link": link, "published": pub_date, "summary": description}
        parsed = parse_rss_pubdate(pub_date)
        if parsed:
            age_seconds = (fetch_time - parsed).total_seconds()
            entry["published_age"] = humanize_age(age_seconds)
            entry["is_recent"] = age_seconds < 12 * 60 * 60  # published in the last 12h
        items.append(entry)
    return items


# ---------------------------------------------------------------------------
# Matchup difficulty
# ---------------------------------------------------------------------------

def team_defense_allowed_by_position(weekly: pd.DataFrame) -> pd.DataFrame:
    """Aggregate PPR fantasy points allowed by each defense, broken out by
    the opposing player's position, from an already-fetched weekly frame.
    Higher points allowed = easier matchup for that position.
    """
    grouped = (
        weekly.groupby(["opponent_team", "position"])["fantasy_points_ppr"]
        .mean()
        .reset_index()
        .rename(columns={"opponent_team": "defense_team", "fantasy_points_ppr": "avg_ppr_allowed"})
    )
    return grouped


def matchup_difficulty_for(player: dict, week: int, weekly: pd.DataFrame, season_used: int) -> dict:
    """Core matchup-difficulty computation, shared by the standalone
    get_matchup_difficulty tool and the composite start/sit outlook tool."""
    position = player.get("position")
    if not position or pd.isna(position):
        return {"error": f"No position on file for {player['name']}."}

    opponent = None
    row = weekly[(weekly["player_id"] == player.get("gsis_id")) & (weekly["week"] == week)]
    if not row.empty:
        opponent = row.iloc[0]["opponent_team"]
    else:
        sched = get_schedules_for_current_and_next()
        team = player.get("team")
        if sched is not None and team:
            wk_games = sched[sched["week"] == week]
            match = wk_games[(wk_games["home_team"] == team) | (wk_games["away_team"] == team)]
            if not match.empty:
                g = match.iloc[0]
                opponent = g["away_team"] if g["home_team"] == team else g["home_team"]

    if opponent is None:
        return {
            "error": (
                f"Could not determine {player['name']}'s week {week} opponent "
                f"for {season_used}. The schedule source may be unreachable, or "
                "that week hasn't been scheduled/played yet."
            )
        }

    defense_stats = team_defense_allowed_by_position(weekly)
    pos_stats = defense_stats[defense_stats["position"] == position].sort_values(
        "avg_ppr_allowed", ascending=False
    ).reset_index(drop=True)

    opp_rows = pos_stats.index[pos_stats["defense_team"] == opponent]
    if len(opp_rows) == 0:
        return {"error": f"No defensive data for {opponent} vs {position} in {season_used}."}

    rank = int(opp_rows[0]) + 1  # rank 1 = allows the most to this position (easiest matchup)
    total = len(pos_stats)
    avg_allowed = round(float(pos_stats.loc[opp_rows[0], "avg_ppr_allowed"]), 1)

    if rank <= max(1, total // 3):
        difficulty = "favorable"
    elif rank > total - max(1, total // 3):
        difficulty = "tough"
    else:
        difficulty = "middling"

    return {
        "opponent": opponent,
        "opponent_avg_ppr_allowed_to_position": avg_allowed,
        "opponent_rank_vs_position": f"{rank} of {total} (1 = most generous defense)",
        "difficulty": difficulty,
    }


def recent_form_for(gsis_id: str, weekly: pd.DataFrame, before_week: int | None = None, window: int = 3) -> dict:
    """Summarize a player's recent-games trend from already-fetched weekly
    data, so callers get a consistent, computed signal instead of having to
    eyeball a list of per-week numbers.

    If `before_week` is set, only games strictly before that week count —
    this matters for start/sit questions about an upcoming week, where the
    trend must be based on what's already happened, not the week in question.
    """
    pdf = weekly[weekly["player_id"] == gsis_id].sort_values("week")
    if before_week is not None:
        pdf = pdf[pdf["week"] < before_week]

    if pdf.empty:
        return {
            "games_considered": 0,
            "trend": "no prior games this season",
            "note": "Lean on matchup and recent news, and last season's role, instead.",
        }

    season_avg = round(float(pdf["fantasy_points_ppr"].mean()), 1)
    recent = pdf.tail(window)
    recent_avg = round(float(recent["fantasy_points_ppr"].mean()), 1)

    if len(pdf) < 2:
        trend = "only one game on record — too early to call a trend"
    elif recent_avg >= season_avg * 1.15:
        trend = "trending up"
    elif recent_avg <= season_avg * 0.85:
        trend = "trending down"
    else:
        trend = "steady"

    return {
        "games_considered": int(len(pdf)),
        "recent_games_avg_ppr": recent_avg,
        "recent_games_count": int(len(recent)),
        "season_avg_ppr": season_avg,
        "trend": trend,
        "last_game_ppr": round(float(pdf.iloc[-1]["fantasy_points_ppr"]), 1),
    }
