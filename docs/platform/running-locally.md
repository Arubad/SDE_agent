# Running the roadmap tutor platform locally

The platform runs as a Docker Compose stack on your own machine: the API (which also serves the built web
app), Postgres with pgvector, Qdrant, SearXNG (search) and a Playwright MCP browser. Phoenix (tracing) and a Cloudflare tunnel are
optional add-ons. Every published port binds to `127.0.0.1`, so nothing is reachable from your network unless
you start the tunnel.

## Prerequisites

- Docker Desktop (or Docker Engine) with Compose v2.24 or newer. Images exist for `linux/amd64` and
  `linux/arm64`, so Intel and Apple Silicon machines both work.
- About 2 GB of free RAM (the idle stack uses ~300 MB, plus ~800 MB with Phoenix) and 6 GB of disk for
  the images.
- Free host ports `8000` (API), `6333` (Qdrant), `8888` (SearXNG) and `8931` (Playwright MCP), and `6006` for
  Phoenix. Postgres publishes no host port.

## Start the stack

Compose refuses to start without a SearXNG secret (see [below](#search-searxng-and-searxng_secret)), so set
one before the first `docker compose up`:

```bash
cp backend/.env.example backend/.env           # fill in the keys you have; every one is optional for a first run
export SEARXNG_SECRET=$(openssl rand -hex 32)   # or put SEARXNG_SECRET=... in a .env next to compose.yaml
docker compose up -d --wait                    # builds the API image on the first run
```

With `export`, the secret lasts for that shell only; put it in the `.env` next to `compose.yaml` to keep it.

Open http://127.0.0.1:8000. `curl http://127.0.0.1:8000/api/health` should report `"status": "ok"` and
`"db": "ok"`.

Outside public mode the API answers `/api/*` only for a loopback `Host` (`127.0.0.1`, `localhost` or `[::1]`, any
port) and refuses a cross-site `Origin` (403), like the `/mcp/*` endpoints. Open the app at one of those names, not
at a LAN address or a custom hostname. `/api/health` and the web app's static files are exempt. The Vite dev server
(`npm run dev`) keeps the `localhost:5173` Host when it proxies `/api`, so it works unchanged.

`backend/.env` reaches the API container only as environment variables (`env_file`). It is never copied into
the image. Compose sets `DATABASE_URL`, `PLATFORM_QDRANT_URL`, `SEARXNG_URL`, `HOST`, `PORT` and `WEB_DIST`
itself, so those always point at the containers whatever `.env` says.

Stop with `docker compose down`. Data lives in the `pgdata` and `qdrant_data` volumes. `docker compose down -v`
deletes them too.

## Search (SearXNG) and `SEARXNG_SECRET`

The stack includes a SearXNG service that the resource resolver and the ingestion agent use to find pages and
videos. Its secret (`SEARXNG_SECRET`, set in [Start the stack](#start-the-stack)) is read from the environment,
never from the settings file. SearXNG listens on http://127.0.0.1:8888 and the API reaches it at
`http://searxng:8080`. Its settings are in `docker/searxng/settings.yml`, which keeps three engines: duckduckgo,
brave and youtube. DuckDuckGo often answers a self-hosted instance with a CAPTCHA and returns 0 results, which is
why Brave is enabled too. Even so, repeated queries can return 0 results for a while. When a resource shows no
candidates, paste its URL in the spec review.

## MCP servers

`compose.yaml` also starts `playwright-mcp` (headless Chromium, `mcr.microsoft.com/playwright/mcp` v0.0.86), the
browser the MCP gateway uses. It sits on its own Docker network `browser`, shared only with the API, and is published
on `127.0.0.1:8931` only. Inside the stack the API reaches it at `PLAYWRIGHT_MCP_URL=http://playwright-mcp:8931/mcp`
(set by Compose); outside Compose the default is `http://127.0.0.1:8931/mcp`, so
`docker compose up -d playwright-mcp` is enough for a virtualenv run. If it is down, the browser tool errors, the
crawler skips pages that need JavaScript, and everything else works. The threat model is in [mcp.md](mcp.md).

Two external servers (arXiv and Wikipedia) run as child processes through `uvx`, which comes with the `uv` package
in `requirements-platform.txt`. The Docker image has it; for a virtualenv run it is installed with the platform
requirements. The first start of each downloads its package, which can take a while, and the gateway allows at least
60 seconds for that connect. `mcp-check` warms them:

```bash
python -m backend.tutor mcp-check [--json]
```

It builds the API's registry and gateway, connects to every server once and prints name, state (`ok`, `down`,
`missing_env`, `disabled`), tool count, latency and error. It is a report, not a gate: the exit code is always 0,
and the same data is on the Settings page. Context7 and GitHub show `missing_env` until `CONTEXT7_API_KEY` and
`GITHUB_TOKEN` are set in `backend/.env`; the other servers need no key. Use the stack's own servers from Claude
Desktop or Claude Code as described in [mcp.md](mcp.md#use-the-platform-from-claude-desktop-or-claude-code).

## Adding a roadmap

The web app is the normal way:

1. Paste the roadmap text. It is parsed into stages, skills and resources.
2. In the spec review, edit the spec and pick a search candidate or paste a URL for each resource.
3. Approve. The workers start ingesting, and the progress view follows the events.
4. Read the ingestion report: a state for every resource and video, plus the agent's actions.
5. Approve or dismiss the ingestion agent's proposed resources. Nothing it proposes is ingested before you approve it.

The same steps are the API routes `POST /api/roadmaps`, `POST /api/roadmaps/{slug}/parse`,
`PUT /api/roadmaps/{slug}/spec`, `POST /api/roadmaps/{slug}/approve`, `GET /api/roadmaps/{slug}/events` (server-sent
events), `GET /api/roadmaps/{slug}/report` and `POST /api/roadmaps/{slug}/resources/{key}/approve` or `/dismiss`.

## Ingestion settings

Set these in `backend/.env` (they are listed there, commented, with their defaults). Nothing here is required.

| Variable | Default | Meaning |
|---|---|---|
| `SEARXNG_URL` | `http://127.0.0.1:8888` | Search service. Compose sets it to the `searxng` container. An empty value turns search off. |
| `YOUTUBE_API_KEY` | none | Free YouTube Data API key. Official Data API for metadata, playlists and top comments (the source of comment timestamps). Without it, metadata comes from yt-dlp (title, channel, description, duration, chapters; no cookies, no download), then oEmbed (title and channel only), and no comments are read. |
| `TRANSCRIPT_ROUTES` | `captions` | Comma list from `captions`, `ytdlp_cookies`, `gemini`, tried in order. `gemini` uses `GEMINI_API_KEY`. |
| `YTDLP_COOKIES_BROWSER` | none | Browser whose cookies yt-dlp uses (for example `chrome`). Required when `ytdlp_cookies` is on. |
| `INGEST_WORKERS` | `2` | Worker threads in the API. `0` starts none (and then the tutor's curator and summaries do not run). |
| `CRAWL_MAX_DEPTH` / `CRAWL_MAX_PAGES` | `2` / `50` | Crawl limits for one documentation resource. |
| `DOMAIN_INTERVAL_S` | `1.0` | Minimum seconds between requests to one host. |
| `PLAYLIST_MAX_VIDEOS` | `50` | Videos taken from one playlist. |
| `EMBEDDER` | `bge` | `bge` (local model) or `hash` (for tests). |
| `GITHUB_TOKEN` | none | Raises GitHub API rate limits when repo READMEs are read. |
| `INGEST_AGENT` | `true` | The Claude Haiku ingestion agent. Its budget is fixed at $0.05 per roadmap. |
| `BROWSER_RENDER` | `true` | Pages whose HTML has no text (`needs_browser`) are rendered in the MCP gateway's browser (the `playwright` server) and then crawled like any page. With it off, or the browser down, they are skipped as before. |

**Transcript routes and YouTube's terms.** Sanctioned by YouTube: the Data API (with `YOUTUBE_API_KEY`), oEmbed, and
the opt-in `gemini` route (Google's official API with the video URL, using `GEMINI_API_KEY`). Not sanctioned by
YouTube's terms: the default `captions` route (youtube-transcript-api reads an undocumented endpoint, not the
official API), the yt-dlp metadata fallback used when no key is set, and the opt-in `ytdlp_cookies` route (yt-dlp
with your browser's cookies). You are responsible for following YouTube's terms. To stay on official paths, set
`YOUTUBE_API_KEY` and `TRANSCRIPT_ROUTES=gemini`.

## Tutor settings

Also in `backend/.env`, commented, with their defaults. How the tutor uses them is in [tutor.md](tutor.md).

| Variable | Default | Meaning |
|---|---|---|
| `TUTOR_MAX_STEPS` | `6` | Model turns in one tutor tool loop. |
| `TUTOR_MAX_TOKENS` | `60000` | Input + output + cache tokens one turn may use. Past it the turn ends with a budget message; the session is saved. |
| `TUTOR_MAX_USD` | `0.02` | Dollars one turn may spend, with the same message. |
| `WORKING_MEMORY_TOKENS` | `12000` | How much of the session's conversation the tutor keeps in context before it folds the oldest turns into a summary. |
| `INGEST_SUMMARIES` | `true` | Summarise each document's sections after ingestion and write starter quiz questions. |
| `INGEST_SUMMARY_BUDGET_USD` | `0.50` | Per-roadmap cap on summary spend; once it is reached, summary jobs are skipped. |

The teacher's and the researcher's effort is `AGENT_EFFORT` (default `low`), the setting the HF demo's agent loop
also reads. The memory curator (`curate_session`) and the summaries run on the ingestion workers, so they need
`INGEST_WORKERS` of at least 1. With `0` a session's summary and facts are never written, and without raw-message
consent its messages are dropped a day after it ends.

## Command line

The same module that serves the API also works on one roadmap from the terminal. It needs the same environment as
the API: Postgres and Qdrant, and SearXNG for anything that searches.

```bash
python -m backend.tutor [--create-db]                  # serve the API
python -m backend.tutor ingest <slug> [--until-idle]   # run the ingestion workers in the foreground
python -m backend.tutor recheck <slug>                 # re-check every resource URL (ok / changed / dead)
python -m backend.tutor report <slug>                  # print the ingestion report as JSON
python -m backend.tutor import-sde [--limit N]         # import the demo SDE knowledge base as roadmap sde-mastery
python -m backend.tutor mcp-check [--json]             # connect to every MCP server and print its health
python -m backend.tutor mcp <knowledge|learner|ingestion|web>   # serve one own MCP server on stdio
```

`ingest` refuses a roadmap that is still `draft` or `parsed`; approve it first. `--until-idle` runs inline and
stops once no job is due, instead of waiting for a final status. `import-sde` loads the demo's knowledge base into
the platform without crawling. It skips cross-document near-duplicate removal, because the demo corpus is already
cleaned.

### Running from a virtualenv

To run the platform outside Docker, start only the services (`docker compose up -d postgres qdrant searxng`; the
Postgres service publishes no host port, so add one with an override file or run your own Postgres), then run
`python -m backend.tutor --create-db` from the repo root with the defaults pointing at `127.0.0.1`: Postgres on
5432, Qdrant on 6333 and SearXNG on 8888.

## Evals and results

- Ingestion eval, no network and no LLM: `python -m backend.tutor.eval.ingestion_eval` (add `--ci` for the
  committed fixtures only). Latest: `backend/tutor/eval/results/ingestion_latest.md`.
- Ingestion agent eval: `python -m backend.tutor.eval.ingest_agent_eval` replays recorded Haiku runs for free.
  `--record` calls Haiku live and costs money. CI replays the recordings with `--strict`, which exits 1 when a
  request has no recording (re-record after changing the agent's prompts or tools). Latest:
  `backend/tutor/eval/results/ingest_agent_latest.md`.
- A live Kubernetes ingestion, written up in [ingestion-report-kubernetes.md](ingestion-report-kubernetes.md).
- SDE retrieval: `backend/tutor/eval/results/sde_retrieval_test_latest.md`. The platform's parent-child chunking
  scores below the demo's `v2_hybrid` on the demo's own questions, so this is a negative result.

## Tracing with Phoenix

```bash
docker compose --profile observability up -d --wait
```

Phoenix runs at http://127.0.0.1:6006 and receives the API's traces over OTLP HTTP
(`http://phoenix:6006/v1/traces` inside the stack). Every request shows up as a span, for example
`GET /api/health`. Without the profile the API still starts and serves normally, and the failed export is
logged once and ignored. Phoenix's own login is off because it is reachable only from this machine. Stop it
with `docker compose --profile observability down`.

## Sharing it publicly with a Cloudflare tunnel

This publishes the app on a public HTTPS URL for as long as it runs. Only do it on purpose.

1. Set `PREMIUM_PASSCODE` in `backend/.env` to a long random value (for example the output of
   `openssl rand -base64 24`). The URL is public, so anyone can guess at it. The tunnel override sets
   `PUBLIC_MODE=true`, and the API refuses to start in public mode without a passcode.
2. Start the stack with the tunnel override:

   ```bash
   docker compose -f compose.yaml -f compose.tunnel.yaml up -d --wait
   ```

3. Find the public URL (a free quick tunnel, so it changes on every start):

   ```bash
   docker compose -f compose.yaml -f compose.tunnel.yaml logs cloudflared | grep trycloudflare.com
   ```

4. Share the URL and the passcode separately. In public mode every `/api/*` route except `/api/health` needs
   the passcode (sent as `X-Access-Code`), and repeated wrong guesses lock that IP out (5 failures per
   15 minutes).
5. Stop sharing:

   ```bash
   docker compose -f compose.yaml -f compose.tunnel.yaml down --remove-orphans
   ```

   Always pass both `-f` files while the tunnel is up, and keep `--remove-orphans` when you stop it.
   A plain `docker compose up` or `docker compose down` reads only `compose.yaml`. It doesn't know about
   `cloudflared`, so it leaves that container running as an orphan, and a plain `up` also recreates the API
   without `PUBLIC_MODE`, so the tunnel would forward the public URL to an API with no passcode. As a
   backstop, an API that isn't in public mode refuses every request carrying Cloudflare's `CF-Connecting-IP`
   or `CF-Ray` header with a 403. Still, check `docker ps` shows no `cloudflared` container after you stop.

   Stopping the `cloudflared` container alone (`docker compose -f compose.yaml -f compose.tunnel.yaml stop
   cloudflared`) also takes the URL offline, but leaves the API in public mode until you restart it with plain
   `docker compose up -d`.

## Why not hosted?

The full platform needs an always-on host with about 8 GB of RAM (the API with its embedding models,
Qdrant, Postgres, search, a headless browser, Phoenix and headroom for Whisper), a persistent disk, Docker
for the code sandbox, HTTPS, and per-visitor isolation and abuse limits, because it runs strangers' code and
stores memory. The free hosts don't fit. A Hugging Face Space is one container with no Docker, no persistent
disk and a YouTube-blocked IP, and this one must stay on ZeroGPU. Render's free tier sleeps and has 512 MB of
RAM and no Docker. GCP's free e2-micro has 1 GB of RAM, and AWS's free plan is time-limited credits. Paying
for it costs, roughly, $8–15 a month for a budget 4 vCPU / 8 GB VPS (Hetzner-class) or $40–50 on mainstream
clouds (DigitalOcean, AWS Lightsail). Managed Postgres (Neon) and Qdrant Cloud have free tiers, then about $20
and $25+ a month, or $0 more self-hosted on the VPS. The LLM (Claude Haiku 5.5 at $0.10 / $0.50 per million
input / output tokens) is cheap per turn, especially with prompt caching, but public traffic makes it
unbounded, which is why the passcode gate stays. Oracle Cloud's Always Free Ampere ARM VM could run the stack,
but it needs card verification, is often "out of capacity", can reclaim idle instances, and its datacenter IP
is blocked by YouTube. So the demo route is this laptop plus a free Cloudflare Tunnel: the complete platform
at a public HTTPS link while the machine is on, from a home IP that YouTube is friendlier to. Prices are
approximate as of November 2026.
