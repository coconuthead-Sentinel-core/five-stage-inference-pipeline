# Strata Core

> A local-first NLP inference pipeline. A request flows through five
> observable processing stages — input classification, routing, context
> management, style conditioning, and response synthesis — behind a FastAPI
> service. Inference backends are pluggable: deterministic heuristics by
> default, a **local LLM via Ollama** when enabled (no cloud, no API key, no
> metered cost). A cloud backend can be added later by config, without a rewrite.

![Status](https://img.shields.io/badge/status-public-success)
![Version](https://img.shields.io/badge/version-0.3.0-informational)
![License](https://img.shields.io/badge/license-MIT-green)
![Backend tests](https://img.shields.io/badge/backend%20tests-100%20passing-brightgreen)
![Frontend tests](https://img.shields.io/badge/frontend%20tests-15%20passing-brightgreen)
[![CI](https://github.com/coconuthead-Sentinel-core/cognitive-neural-overlay/actions/workflows/ci.yml/badge.svg)](https://github.com/coconuthead-Sentinel-core/cognitive-neural-overlay/actions/workflows/ci.yml)

> **Naming note.** The product/project name is **Strata Core**. The Python
> package and HTTP route namespace are `cno` (the service's internal module
> name) — product name and module name differ, which is normal. This project
> began as a personal exploration and was refactored to professional standards;
> it is applied systems engineering, not novel research.

---

## The five stages

```text
                ┌────────────────────┐
       request →│  INPUT (classify)  │  modality, request type, tone (+confidence)
                └─────────┬──────────┘
                          ▼
                ┌────────────────────┐
                │  ROUTER (dispatch) │  directs to an analysis / reflection / output sublayer
                └─────────┬──────────┘
                          ▼
                ┌────────────────────┐
                │  MEMORY (context)  │  anchors prior context; live context stack
                └─────────┬──────────┘
                          ▼
                ┌────────────────────┐
                │  PERSONA (style)   │  style conditioning: technical / direct / etc.
                └─────────┬──────────┘
                          ▼
                ┌────────────────────┐
                │  OUTPUT (synthesis)│  refines clarity, tone, structure → response
                └────────────────────┘
```

Each stage annotates the request and is independently observable. Stages run on
scored heuristics by default; the input classifier can additionally use an LLM
backend (see below), falling back to heuristics whenever the backend is
unavailable or returns an out-of-range value.

## Local LLM backend (offline, zero cost)

The input classifier accepts an optional backend implementing a one-method
Protocol (`cno/llm.py`):

```python
class LLMClassifier(Protocol):
    def classify(self, text: str, schema: str) -> dict[str, Any]: ...
```

`OllamaClassifier` is a ready implementation that runs a **local** model via
Ollama. It is 100% offline — the only network touch is the loopback to the
Ollama daemon on the same machine. There is no API key and no per-token cost.

Enable it with environment variables (off by default):

```bash
pip install ollama                 # optional dependency
ollama pull llama3.2:3b            # or any local model
export STRATA_LLM_BACKEND=local    # turn the backend on
export STRATA_MODEL=llama3.2:3b    # model id (default shown)
export STRATA_NUM_CTX=2048         # bound the KV cache to fit low-RAM machines
```

Left unset, the service behaves exactly as before on heuristics and needs no new
dependency. To add a cloud model later, write another class with the same
`.classify()` method and select it in `load_classifier_from_env()` — the rest of
the pipeline is unchanged.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/cno/process` | Full pipeline (batch) — request crosses all 5 stages; returns `run_id` + classification/routing/anchor/persona/synthesis |
| POST | `/cno/process/stream` | Same pipeline as Server-Sent Events; emits `start`, 5× `node` (with `elapsed_ms`), `complete` |
| POST | `/cno/node/{node_name}` | Run one stage in isolation (`input` / `router` / `memory` / `persona` / `synth`) |
| GET | `/cno/state` | Inspect recent context anchors (live context stack) |
| POST | `/cno/state/reset` | Clear the context stack |
| GET | `/cno/audit` | List recent runs (header summary, most recent first) |
| GET | `/cno/audit/stats` | Aggregated metrics for dashboard widgets |
| GET | `/cno/audit/{run_id}` | Drill-down: full per-stage trace for a single run |
| POST | `/cno/audit/backup` | Online SQLite snapshot of the audit DB. **Auth-gated** when `CNO_API_KEY` is set. |
| GET | `/cno/artifact/{run_id}` | Emitted artifact: markdown body with 10-field YAML frontmatter |
| GET | `/healthz` | Liveness — reports `auth_enabled` + `rate_limit_per_minute` |
| GET | `/` | Service banner + endpoint list |
| GET | `/docs` | Swagger UI |
| GET | `/ui/` | Browser dashboard (Console + Audit tabs) — only if `cno/static/` is built |

## Quick start

```bash
# Backend
pip install -r requirements.txt
uvicorn cno.app:app --reload
pytest -v
# open http://localhost:8000/docs

# (Optional) Dashboard at /ui — requires Node 18+
cd frontend
npm install
npm run build       # bundles into ../cno/static/
npm test            # vitest + jsdom + testing-library
```

## Persistence + observability

- **SQLite audit log** (`cno_audit.db`, configurable via `CNO_DB_PATH`). Tables:
  `runs` (one row per pipeline call, includes `total_ms`) + `audit_log` (one row
  per stage crossing). Append-only; idempotent migrations on startup.
- **Structured JSON logs** to stderr, one object per line, with `run_id`,
  `session_id`, `sublayer`, `modality` on every run.
- **Memory stage** keeps an in-process FIFO (max 50) and ships every anchor to a
  configured `ArchivalSink`; sink failures never break the pipeline.

## Archival sinks

`MemoryNode` accepts an optional `ArchivalSink` (`cno/sinks.py`): `NullSink`
(default, drops anchors), `JsonlSink` (threadsafe append to a JSONL file), or
`HttpAMCSink` (POSTs anchors to an external store — contract pending). Configure
via `CNO_AMC_SINK=null|jsonl|http` and related `CNO_AMC_*` vars.

## Auth + rate limiting

Disabled by default (single-user dev mode). Enable via env:

```bash
CNO_API_KEY=your-secret           # X-API-Key required on all routes except the bypass list
CNO_ALLOWED_ORIGINS=https://...   # CSV CORS allow-list; defaults to "*"
CNO_RATE_LIMIT_PER_MINUTE=120     # 0 disables; sliding 60s window per key/IP
```

Bypass list (always reachable): `/healthz`, `/`, `/docs`, `/redoc`,
`/openapi.json`, `/ui*`. `/cno/audit/backup` is intentionally **not** bypassed.

## Deployment

### Docker (recommended)

```bash
cp .env.example .env        # set CNO_API_KEY, CORS, etc.
docker compose up -d --build
docker compose logs -f cno
# open http://localhost:8000/ui/
```

Multi-stage build (React bundle → Python runtime), persistent volume for the
SQLite DB, non-root user, built-in healthcheck. The image is platform-portable
(Fly.io / Railway / Render / Fargate / Cloud Run / a VPS) — only `.env` and the
volume binding change.

### Bare metal / VM

```bash
pip install -r requirements.txt
cp .env.example .env
./scripts/run_prod.sh       # builds the frontend if missing, runs uvicorn --proxy-headers
```

Terminate TLS at a reverse proxy (nginx / Caddy / your platform LB), not in
uvicorn. Keep `UVICORN_WORKERS=1` until the in-process context stack is moved to
shared storage (the audit log is multi-worker safe; the live stack is per-process).

## Continuous integration

`.github/workflows/ci.yml` runs on every push/PR to `main`:

1. **backend** — Python 3.12, `pip install -r requirements.txt`, `pytest -v`.
2. **frontend** — Node 20, `npm ci`, `npm test`, `npm run build`.
3. **docker** — builds the multi-stage Dockerfile end-to-end.

## Backup / restore

`POST /cno/audit/backup` uses SQLite's native online `backup()` API (safe while
serving) and writes a snapshot under `backups/`. `scripts/backup_db.sh` hits the
same code path for cron. Restore is a file copy: stop the service, swap the DB,
restart.

## License

MIT — see `LICENSE`.

## Author

**Shannon Brian Kelly.** Built with AI assistance. This is an applied
systems-engineering project — a working demonstration of pipeline architecture,
pluggable inference backends, observability, and local-first deployment — not a
research contribution.
