# Running the roadmap tutor platform locally

The platform runs as a Docker Compose stack on your own machine: the API (which also serves the built web
app), Postgres with pgvector, and Qdrant. Phoenix (tracing) and a Cloudflare tunnel are optional add-ons.
Every published port binds to `127.0.0.1`, so nothing is reachable from your network unless you start the
tunnel.

## Prerequisites

- Docker Desktop (or Docker Engine) with Compose v2.24 or newer. Images exist for `linux/amd64` and
  `linux/arm64`, so Intel and Apple Silicon machines both work.
- About 2 GB of free RAM (the idle stack uses ~300 MB, plus ~800 MB with Phoenix) and 6 GB of disk for
  the images.
- Free host ports `8000` and `6333` (and `6006` for Phoenix). Postgres publishes no host port.

## Start the stack

```bash
cp backend/.env.example backend/.env   # fill in the keys you have; every one is optional for a first run
docker compose up -d --wait            # builds the API image on the first run
```

Open http://127.0.0.1:8000. `curl http://127.0.0.1:8000/api/health` should report `"status": "ok"` and
`"db": "ok"`.

`backend/.env` reaches the API container only as environment variables (`env_file`). It is never copied into
the image. Compose sets `DATABASE_URL`, `PLATFORM_QDRANT_URL`, `HOST`, `PORT` and `WEB_DIST` itself, so
those always point at the containers whatever `.env` says.

Stop with `docker compose down`. Data lives in the `pgdata` and `qdrant_data` volumes. `docker compose down -v`
deletes them too.

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
