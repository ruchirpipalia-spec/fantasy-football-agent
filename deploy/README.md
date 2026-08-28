# Deploying your own remote copy (optional)

The normal way to use this plugin is the local install described in the
main [README](../README.md): Claude starts the MCP server on your own
machine, and it only runs while that machine does. That's the simplest,
truly-zero-cost setup, and it's all most people need.

This guide is for the specific case where you want to reach the agent
from your **phone**, or from any device, **without your laptop being on**
— by deploying your own copy as a small always-on web service and adding
it to Claude as a [custom connector](https://claude.com/docs/connectors/custom/remote-mcp),
which works from claude.ai, Claude Desktop, Cowork, and the mobile apps.

**This is entirely optional and separate from the plugin.** It costs
$0/month on the free tiers used below, no credit card on either service —
but it is a second thing to set up and maintain, and it's a deployment
that's yours alone: it doesn't replace or interfere with the plugin, and
if a friend wants the same phone access, they deploy their own copy the
same way — nobody's inference cost or hosting bill is shared.

## Why two free services, not one

Render (used to run the server) has a genuinely free tier, but its
filesystem is **ephemeral** — anything written to local disk is wiped
every time the free service restarts, which happens automatically after
~15 minutes of no traffic. If the draft board (the whole point of this —
"don't recommend players I've already drafted") lived in a local file
there, it would silently reset all the time.

So the draft board is stored in [Upstash](https://upstash.com) Redis
instead when this is deployed remotely — also genuinely free (256MB,
500K commands/month, no card), and unlike Render's disk, it isn't wiped
on restart. Everything else (stats, matchups, news, waiver trends) still
fetches live and needs no storage either way.

## Setup

### 1. Get your own copy of the repo

Fork this repo on GitHub (or push your own clone) — Render deploys from
a repo you control.

### 2. Create a free Upstash Redis database

1. Go to [console.upstash.com](https://console.upstash.com) and sign up
   (email or GitHub — no card required).
2. Create a database — any region is fine, the free tier is selected by
   default.
3. On the database's page, find the **REST API** section and copy the
   `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` values. You'll
   paste these into Render next.

### 3. Deploy to Render

1. Go to [Render](https://render.com) and sign up (no card required).
2. **New > Blueprint**, and point it at your fork of this repo. Render
   reads `render.yaml` at the repo root and sets up the service
   automatically — free plan, correct build/start commands.
3. When prompted for environment variables, paste in the
   `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` from step 2.
   Optionally, also paste in a `FANTASYPROS_API_KEY` (free, personal/
   non-commercial tier — get one at
   [fantasypros.com/api-data](https://www.fantasypros.com/api-data/)) to
   turn on `get_draft_projections` (real forward-looking projections,
   including rookies — see the main README for what that adds over the
   default `get_draft_rankings`). Skip it and everything else still works
   normally; that one tool just returns a "not configured" message.
4. Deploy. Once it's live, your server's URL is
   `https://<your-service-name>.onrender.com`.

**A build note worth knowing if you hit it**: Render's Python build
environment can get cached across deploys. If a deploy fails on a fresh
`render.yaml` change with something like `ModuleNotFoundError: No module
named 'pkg_resources'`, that's usually Render still running an old cached
Python version rather than picking up the `PYTHON_VERSION` pin — use
**Manual Deploy > Clear build cache & deploy** once to force a truly
fresh environment, rather than assuming the pin itself is wrong.

**Free-tier cold starts**: after 15 minutes of no traffic, Render spins
the service down; the next request wakes it back up, which takes roughly
a minute. The first message you send after a while idle may just take a
bit longer to answer — this is normal, not a bug.

### 4. Add it to Claude as a custom connector

1. In Claude, go to **Customize > Connectors > Add custom connector**
   (on Team/Enterprise, an organization owner adds it under
   **Organization settings > Connectors** first, then members connect
   individually).
2. Enter your server's MCP URL: `https://<your-service-name>.onrender.com/mcp`
3. Save. It should now show up as available in the connector list on
   every Claude client, including the mobile apps.

### 5. (Optional) Add authentication

Right now, anyone who has your server's URL could call its tools and
read or change your draft board — there's no login screen on a personal
MCP server by default. For a casual hobby project this is a reasonable
tradeoff (your Render URL isn't indexed or guessable), but if you want to
lock it down:

1. In the "Add custom connector" dialog, check **Advanced settings** for
   a **Request Headers** section. This feature is a gradual rollout on
   Claude's side, so it may or may not be visible on your account yet —
   if you don't see it, skip this section; there's currently no other
   supported way for Claude to send a credential to a server like this
   one, so leaving it unauthenticated is the only option for now.
2. If you do see it: in Render, go to your service's **Environment**
   tab, add a variable `MCP_AUTH_TOKEN` with any long random value you
   choose, and save (this redeploys the service).
3. Back in Claude's Request Headers section, add a header named
   `authorization` with the value `Bearer <the same token>` (include the
   word "Bearer" and the space).
4. Reconnect the connector. From now on, only requests carrying that
   header will be accepted.

## Local plugin vs. this deployment — what's different

| | Local plugin | This deployment |
|---|---|---|
| Runs on | your machine, while it's open | Render, continuously |
| Reachable from | wherever the plugin is installed | any device, including phone, via the Claude app |
| Draft board storage | a local file | Upstash Redis |
| Cost | $0, no accounts needed | $0 on free tiers, two accounts (Render, Upstash) |
| Setup | install the plugin | this guide |

Both read and write the exact same tool logic in `servers/` — this
isn't a fork, just a different way of running the same server.

## Engineering notes: integrating a real third-party API

The FantasyPros integration (`get_draft_projections`) went through a few
rounds of real debugging worth documenting, in case this pattern is
useful elsewhere or this integration ever needs revisiting:

- **A generic `403 {"message":"Forbidden"}` isn't always what it looks
  like.** It's the classic fingerprint of an AWS API Gateway rejection,
  which reads identically whether the cause is an invalid key, a WAF
  block, or (what it actually was here) a wrong base URL. The fix —
  the base URL needs a `/public/` segment
  (`api.fantasypros.com/public/v2/json`) that's easy to miss because it's
  absent from FantasyPros' own hosted interactive docs page — was found
  by reading FantasyPros' real open-source PHP client on GitHub rather
  than trusting the docs page, and confirmed by the 403 disappearing.
- **A 200 OK with zero results can still mean the request shape is
  wrong**, not that the data doesn't exist. The `/projections` endpoint
  requires `position` to be one real value (`WR`, `RB`, etc.) — not
  `ALL` and not a list, both reasonable-looking guesses that returned
  silently empty results with no error. Confirmed against FantasyPros'
  own docs example and fixed by making one request per position instead
  of trying to fetch every position in a single call.
- **A free tier's limits aren't always documented up front.** FantasyPros
  caps results at roughly 10 players per position per request on the
  free plan — discovered empirically (asking for 60 consistently
  returned exactly 10), not from any docs page, and now surfaced
  explicitly in the tool's own response (`shallow_pool_note`) rather than
  silently truncating.
