<!-- BEGIN mempalace-cloudflare fork section. Keep this block when merging upstream. -->

# MemPalace on Cloudflare

This is a fork of [MemPalace](https://github.com/MemPalace/mempalace) that runs the palace as a Cloudflare Worker. Every machine you work on connects to the same palace over HTTPS. You do not install MemPalace, ChromaDB, or an embedding model on each machine.

It is for developers who move between a laptop, a desktop, remote servers, and cloud VMs, and who want one memory that follows them without a local install everywhere.

The upstream MemPalace README starts [below](#mempalace). It describes the engine, the concepts, and the local install.

### Read this first: your data is in your Cloudflare account

Upstream MemPalace is local-first. By default, nothing leaves your machine. This fork changes that:

- Drawer text is stored in your R2 bucket. Metadata and the knowledge graph are stored in your D1 database. Vectors are stored in your Vectorize index. All three are in your own Cloudflare account.
- Embeddings are computed by Workers AI (`@cf/baai/bge-small-en-v1.5`), so drawer text and search queries are sent to Cloudflare for embedding.
- A single bearer token protects the Worker. Anyone with the token can read and write the palace.

If you do not want your memory on Cloudflare, use upstream MemPalace instead.

The verbatim rule does not change. Drawers are stored exactly as written. Nothing is summarized or rewritten.

### How it maps to Cloudflare

| MemPalace part | Upstream (local) | This fork |
| --- | --- | --- |
| Drawer text (verbatim) | ChromaDB | R2 bucket `mempalace-drawers` |
| Vector search | ChromaDB | Vectorize index `mempalace-index` (384 dimensions, cosine) |
| Keyword ranking | SQLite BM25 | BM25 re-rank of Vectorize candidates, in the Worker |
| Drawer registry and taxonomy | ChromaDB metadata | D1 database `mempalace-kg`, table `drawers` |
| Knowledge graph | Local SQLite | D1 database `mempalace-kg`, tables `entities` and `triples` |
| Embeddings | Local ONNX model | Workers AI |
| MCP server | Local stdio or HTTP process | Worker at `/mcp` (MCP Streamable HTTP, JSON responses) |

### What works today

The Worker exposes 25 MCP tools:

- Palace: `mempalace_status`, `mempalace_list_wings`, `mempalace_list_rooms`, `mempalace_get_taxonomy`, `mempalace_get_aaak_spec`
- Search and filing: `mempalace_search`, `mempalace_check_duplicate`, `mempalace_add_drawer`, `mempalace_checkpoint`
- Drawers: `mempalace_get_drawer`, `mempalace_get_drawers`, `mempalace_list_drawers`, `mempalace_update_drawer`, `mempalace_delete_drawer`, `mempalace_delete_drawers`, `mempalace_delete_by_source`
- Diary: `mempalace_diary_write`, `mempalace_diary_read`, `mempalace_memories_filed_away`
- Knowledge graph: `mempalace_kg_query`, `mempalace_kg_add`, `mempalace_kg_invalidate`, `mempalace_kg_supersede`, `mempalace_kg_timeline`, `mempalace_kg_stats`

Upstream's local MCP server has more tools. These are not in the Worker yet:

- Mining and sync (`mempalace_mine`, `mempalace_sync`), and anything that reads local files
- Tunnels, hallways, and graph traversal
- Mesh peers
- Hook settings and reconnect
- Agent coordination (logstream events, tasks, artifacts, patches)

The Worker also has REST routes: `/healthz` (no auth), `/api/status`, `/api/taxonomy`, `/api/search`, and `/api/drawers`.

### Deploy

You need:

- A Cloudflare account
- Node.js, so that you can run Wrangler with `npx wrangler`
- Python 3, for the smoke test

**1. Get the code.**

```bash
git clone https://github.com/undeadindustries/mempalace-cloudflare.git
cd mempalace-cloudflare
```

**2. Sign in to Cloudflare.** Use one of these:

- **API token (works without a browser).** Copy the template and fill in both values. `.env.example` lists the token permissions it needs. Wrangler reads `.env` from the project root on every command.

  ```bash
  cp .env.example .env
  ```

- **Browser login.**

  ```bash
  npx wrangler login
  ```

`.env` and `wrangler.toml` are gitignored. Only the `.example` templates are committed.

**3. Create the Cloudflare resources and `wrangler.toml`.**

```bash
scripts/cloudflare_bootstrap.sh
```

The script:

- creates the Vectorize index and its `wing`, `room`, and `source_file` metadata indexes
- creates the D1 database and its tables
- creates the R2 bucket
- writes `wrangler.toml` from `wrangler.toml.example` with your D1 database id
- asks for the API key and stores it as the `MEMPALACE_API_KEY` Worker secret

It checks each resource first and creates only what is missing, so you can run it again after a failure. It never deletes anything. If `wrangler.toml` already points at a different D1 database, the script stops instead of overwriting the file.

For the API key, generate a long random token, for example with `openssl rand -hex 32`, and keep a copy in your password manager. Do not put it in `wrangler.toml` or `.env`, and do not commit it.

<details>
<summary>Doing step 3 by hand</summary>

Create the metadata indexes before you insert any vectors. Vectorize can only filter on a metadata field if its index existed when the vectors were inserted.

```bash
npx wrangler vectorize create mempalace-index --dimensions=384 --metric=cosine
npx wrangler vectorize create-metadata-index mempalace-index --property-name=wing --type=string
npx wrangler vectorize create-metadata-index mempalace-index --property-name=room --type=string
npx wrangler vectorize create-metadata-index mempalace-index --property-name=source_file --type=string

npx wrangler d1 create mempalace-kg
cp wrangler.toml.example wrangler.toml
# Paste the database_id printed by `d1 create` into wrangler.toml.

npx wrangler d1 execute mempalace-kg --remote --file=migrations/0001_kg.sql
npx wrangler d1 execute mempalace-kg --remote --file=migrations/0002_registry.sql

npx wrangler r2 bucket create mempalace-drawers
npx wrangler secret put MEMPALACE_API_KEY
```

Use `--remote` on `d1 execute`. Without it, Wrangler can write to a local development copy, and the deployed Worker then fails because the tables do not exist.

</details>

**4. Deploy.**

```bash
npx wrangler deploy
```

Wrangler prints the URL, for example `https://mempalace-cf.<your-subdomain>.workers.dev`.

**5. Run the smoke test.**

```bash
export MEMPALACE_API_KEY=<your-api-key>
python3 scripts/test_smoke.py --url https://mempalace-cf.<your-subdomain>.workers.dev
```

It checks `/healthz`, checks that a request without the token gets `401`, calls `/api/status` with the token, and lists the MCP tools. The script reads the key from `MEMPALACE_API_KEY` so it does not end up in your shell history. If you set up Cloudflare Access (below), also export `CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET`, and the script checks that a request without the service token is refused.

### The URL does not change when you redeploy

The workers.dev URL is `<worker name>.<account subdomain>.workers.dev`. `npx wrangler deploy` replaces the code behind that URL and keeps the URL. The URL changes only if you rename the Worker (`name` in `wrangler.toml`), change your account's workers.dev subdomain, move to another Cloudflare account, or set `workers_dev = false`.

You do not need a custom domain or a DNS record.

Cloudflare can also serve a separate preview URL for each version, `<version>-mempalace-cf.<subdomain>.workers.dev`. Those URLs change with every deploy, and they expose the same Worker on more hostnames. `wrangler.toml.example` sets `preview_urls = false`, which turns them off. If your `wrangler.toml` was generated before that line existed, add it and deploy again.

### Put Cloudflare Access in front (recommended)

Without Access, anyone who knows the URL can send requests. A wrong token gets `401`, but the Worker still runs for each one, and every request counts toward the free plan's daily Workers limit. A flood of junk requests could use it up and lock you out until the limit resets.

Cloudflare Access fixes this. It checks every request at Cloudflare's edge before the Worker runs. Your machines send a service token in two extra headers, and anything without a valid token is refused at the edge with `403`. The bearer token still applies behind Access, so a caller needs both.

1. In the Cloudflare dashboard, go to **Zero Trust** > **Access** > **Service credentials** > **Service Tokens** and create a token. Copy the Client ID and the Client Secret right away. The secret is shown only once.
2. Create an Access policy with the action **Service Auth** (not **Allow**, which sends callers to a browser login page) and include that service token. A session duration of 15 minutes is fine.
3. Go to **Workers & Pages**, select the Worker, and open its Access settings. Protect **all traffic** (production and previews) with that policy.
4. Check it. Without the service token, `curl -i https://mempalace-cf.<your-subdomain>.workers.dev/healthz` should return `403`.

Keep the Client ID and Client Secret with your API key, in your password manager. To rotate them, create a new service token, add it to the policy, update your machines, then revoke the old one.

### Connect your machines

**Cursor.** Add the Worker to your user-level `~/.cursor/mcp.json` and paste in your values. Leave out the two `CF-Access-*` lines if you do not use Access. Do not put this entry in a project's `.cursor/mcp.json`, which is easy to commit by mistake.

```json
{
  "mcpServers": {
    "mempalace": {
      "url": "https://mempalace-cf.<your-subdomain>.workers.dev/mcp",
      "headers": {
        "Authorization": "Bearer <your-api-key>",
        "CF-Access-Client-Id": "<service-token-client-id>",
        "CF-Access-Client-Secret": "<service-token-client-secret>"
      }
    }
  }
}
```

If you would rather keep the values out of the file, Cursor also accepts `${env:NAME}` in any header value, for example `"Bearer ${env:MEMPALACE_API_KEY}"`. The variable then has to be set in the environment Cursor starts with.

**Other MCP clients.** Any client that supports MCP over Streamable HTTP and custom request headers can connect. Point it at `https://mempalace-cf.<your-subdomain>.workers.dev/mcp` and send `Authorization: Bearer <your-api-key>`, plus `CF-Access-Client-Id` and `CF-Access-Client-Secret` if you use Access.

**The `mempalace` CLI (optional).** If you want the CLI on a machine, install upstream MemPalace and the client plugin in this repo into the same environment. The plugin adds a `cloudflare-remote` backend that sends reads and writes to the Worker.

```bash
pip install -e client/
export MEMPALACE_BACKEND=cloudflare-remote
export MEMPALACE_CLOUDFLARE_URL=https://mempalace-cf.<your-subdomain>.workers.dev
export MEMPALACE_CLOUDFLARE_TOKEN=<your-api-key>
export CF_ACCESS_CLIENT_ID=<service-token-client-id>
export CF_ACCESS_CLIENT_SECRET=<service-token-client-secret>
```

The plugin sends the Access service token when `CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET` are set. Set both or neither. If only one is set, the plugin stops with an error rather than sending requests that Access would refuse.

This installs MemPalace on that machine. Skip it on machines where an MCP client is enough.

### Security

- Every route except `/healthz` requires `Authorization: Bearer <token>`. A missing or wrong token gets `401`. If the secret is not set, the Worker returns `503` and does not serve data.
- With Cloudflare Access in front, every route, including `/healthz`, also requires the service token. Requests without it never reach the Worker.
- To rotate the key, run `npx wrangler secret put MEMPALACE_API_KEY` with a new value, then update `mcp.json` (and `MEMPALACE_CLOUDFLARE_TOKEN`, if you use the CLI) on each machine.
- Without Access, the bearer token is the only access control. With Access, a caller needs both the service token and the bearer token. Treat both like passwords.

### Cost

The fork is built to stay inside Cloudflare's free allowances for one person's use: Workers, Workers AI, D1, R2, and Vectorize. Free limits change, so check [Cloudflare's Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/) against your own usage.

### Getting upstream fixes

The fork is additive. All Cloudflare code is in new files, and no upstream engine file is changed:

- `mempalace/cloudflare/`: the Worker entrypoint and the Cloudflare adapters
- `mempalace/backends/cloudflare_vectorize.py`: a backend registered through the upstream backend registry
- `client/`: the `cloudflare-remote` client plugin
- `migrations/`, `wrangler.toml.example`, `.env.example`, `scripts/cloudflare_bootstrap.sh`, `scripts/test_smoke.py`
- `tests/test_cloudflare_*.py`

Three upstream files differ in the fork:

- `README.md`: this block, kept at the top so that merge conflicts, if any, stay in one place.
- `.gitignore`: a marked block that ignores `wrangler.toml`, `.dev.vars*`, and `.wrangler/`, and un-ignores `.env.example`.
- `AGENTS.md`: upstream has a symlink to `CLAUDE.md` here. The fork replaces it with its own agent orientation file.

The fork lives on `main`, which is this repository's default branch. `develop` mirrors upstream and has no Cloudflare code. To pull upstream changes into the fork:

```bash
git remote add upstream https://github.com/MemPalace/mempalace.git   # first time only
git checkout main
git fetch upstream
git merge upstream/develop
uv sync --extra dev
uv run pytest tests/test_cloudflare_*.py
npx wrangler deploy
```

If `README.md` conflicts, keep this block and take upstream's version of everything below it. If `.gitignore` conflicts, keep both upstream's lines and the fork block. If `AGENTS.md` conflicts, keep the fork's file.

To confirm that the fork is still additive, run `git diff upstream/develop...main --stat`. It should list only the files above.

One limit: Cloudflare's Python Workers bundle only the files in `mempalace/cloudflare/`. The Worker cannot import the upstream engine at runtime. The fork has its own copies of a few helpers, such as drawer and triple ID hashing and ISO date validation, and it has its own search ranking. Upstream fixes to the CLI, the hooks, and the backend contract reach you through the merge. Upstream fixes to those copied helpers or to upstream search ranking do not change the Worker automatically. After a merge, check the diff of `mempalace/ids.py`, `mempalace/knowledge_graph.py`, and `mempalace/searcher/`, and port relevant changes into `mempalace/cloudflare/`.

<!-- END mempalace-cloudflare fork section -->

---

<div align="center">

<img src="assets/mempalace_logo.png" alt="MemPalace" width="240">

# MemPalace

Local-first AI memory. Verbatim storage, pluggable backend, 96.6% R@5 raw on LongMemEval — zero API calls.

[![][version-shield]][release-link]
[![][python-shield]][python-link]
[![][license-shield]][license-link]
[![][discord-shield]][discord-link]

</div>

> [!CAUTION]
> **Beware of impostor sites.** MemPalace has no other official websites. The **only** official sources are this **[GitHub repository](https://github.com/MemPalace/mempalace)**, the **[PyPI package](https://pypi.org/project/mempalace/)**, and the docs at **[mempalaceofficial.com](https://mempalaceofficial.com)**. Any other domain (including `.tech`, `.net`, or other `.com` variants) is an impostor and may distribute malware. Details and timeline: [docs/HISTORY.md](docs/HISTORY.md).

> [!IMPORTANT]
> **Claude Code sessions expire in 30 days without auto-save hooks wired.** [Read this →](https://github.com/MemPalace/mempalace/discussions/1388)
>
> Need the shortest recovery/setup path? Use the [Claude Code retention setup checklist](https://mempalaceofficial.com/guide/claude-code-retention.html).

---

## What it is

MemPalace stores your conversation history as verbatim text and retrieves
it with semantic search. It does not summarize, extract, or paraphrase.
The index is structured — people and projects become *wings*, topics
become *rooms*, and original content lives in *drawers* — so searches
can be scoped rather than run against a flat corpus.

The retrieval layer is pluggable. The current default is ChromaDB; the
interface is defined in [`mempalace/backends/base.py`](mempalace/backends/base.py)
and alternative backends can be dropped in without touching the rest of
the system.

Nothing leaves your machine unless you opt in.

Architecture, concepts, and mining flows:
[mempalaceofficial.com/concepts/the-palace](https://mempalaceofficial.com/concepts/the-palace.html).

---

## Install

### Agent-guided setup

Install the MemPalace skills first, then ask your coding agent to set up
MemPalace. The setup skill detects your system, installs the Python package,
configures MCP, and asks whether you want a private local palace, a shared-brain
hub, or a client connected to an existing hub:

```bash
npx skills add MemPalace/mempalace
```

The repository exposes three skills: `mempalace` for guided installation and
operations, `mempalace-recall` for search-before-answer recall, and
`mempalace-task` for logstream delegation. Installing a skill does not by
itself install the MemPalace CLI or MCP server; the setup skill guides the
agent through those system changes and verifies the live connection.

During guided setup the agent can offer weekly stable-release checks. They are
disabled by default, contact only PyPI when enabled, and never install updates
automatically. Cached availability appears in scoped `mempalace_status` fields
for the serving runtime and, when a local proxy is present, its client runtime,
allowing the agent to explain the release and request authorization before showing an exact
upgrade plan. Setup records whether the runtime came from `uv tool`, `pipx`, or
`pip` so the plan never proposes an upgrade command for the wrong installation.

### Direct CLI setup

MemPalace ships a CLI, so install it in an isolated environment to avoid
PEP 668 errors on Debian/Ubuntu/Homebrew Pythons and to keep mempalace's
deps (`chromadb`, `numpy`, `grpcio`, …) from conflicting with anything
else in your global site-packages.

We recommend [`uv`](https://docs.astral.sh/uv/) — `uv tool install` puts
the `mempalace` CLI in an isolated environment on your PATH:

```bash
uv tool install mempalace
mempalace init ~/projects/myapp
```

[`pipx`](https://pipx.pypa.io/) works the same way if you prefer it:
`pipx install mempalace`.

Prefer plain `pip` only inside an activated virtualenv where you
explicitly want `import mempalace` available:

```bash
python -m venv .venv && source .venv/bin/activate
pip install mempalace
```

### Android / Termux

Native Termux installation is not currently supported because compiled
dependencies such as ChromaDB and ONNX Runtime publish Linux wheels, not
Android wheels. Android ARM64 users can run the regular Linux packages in an
isolated Debian PRoot container instead. See the
[Termux installation guide](website/guide/termux.md) for the tested setup and
an argv-preserving launcher.

### Docker

A container image is also available for running the MCP server or the CLI
without a local Python toolchain. Multi-arch (amd64 + arm64), so it runs
natively on Apple Silicon:

```bash
docker pull ghcr.io/mempalace/mempalace:latest
```

Everything persists under `/data` — palace, config, and the cached embedding
model — so mount a volume there and reuse it across runs:

```bash
# MCP server over stdio — note the `-i` flag (JSON-RPC needs stdin)
docker run -i --rm -v mempalace-data:/data ghcr.io/mempalace/mempalace

# Run any CLI command instead. The container only sees what you mount, so
# mount the directory you want to mine — read-only is enough, mining never
# writes to the source.
docker run --rm -v mempalace-data:/data -v /path/to/project:/work:ro \
  ghcr.io/mempalace/mempalace mine /work
docker run --rm -v mempalace-data:/data ghcr.io/mempalace/mempalace search "why GraphQL"
```

The first command that needs embeddings downloads the model into `/data`
(~80 MB for the default `minilm`, ~300 MB for `embeddinggemma`). It is a
one-off as long as the volume persists, but it does mean the first call is
slow and needs network — worth knowing before assuming a hung container.

Wire it into an MCP client (e.g. Claude Code) as a stdio server. Mount
anything you want the server to be able to mine — it cannot reach your
transcripts otherwise:

```json
{
  "mcpServers": {
    "mempalace": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-v", "mempalace-data:/data",
        "-v", "/absolute/path/to/.claude/projects:/transcripts:ro",
        "ghcr.io/mempalace/mempalace"
      ]
    }
  }
}
```

Use a real absolute path there — `~` and `$HOME` are not expanded by every
MCP client. Paths are container paths from then on: mine `/transcripts`, not
`~/.claude/projects`.

**Mount permissions on Linux.** The image runs as uid 1000 and bind mounts
keep their host ownership, so a mounted directory has to be readable by that
uid — an ordinary `0755` checkout is fine, a `0700` directory is not, and the
failure surfaces as `PermissionError: [Errno 13]` rather than anything about
Docker. Docker Desktop maps uids on macOS and Windows, so this only bites on
Linux. Do **not** work around it with `--user`: `/data` is owned by uid 1000
inside the image, so another uid cannot write the palace at all.

`docker compose run --rm mcp` works too (see `docker-compose.yml`), and
`deploy/docker-compose.server.yml` stands up the team server. To build the
image yourself instead of pulling — required for the GPU variant, which is not
published:

```bash
docker build -t mempalace .                                  # CPU
docker build --build-arg EXTRAS="extract,spellcheck" -t mempalace .
docker build -f Dockerfile.gpu -t mempalace:gpu .            # CUDA; run with --gpus all
```

The GPU image is x86_64-only: `onnxruntime-gpu` publishes no aarch64 Linux
wheels, so that last build fails on an ARM host (including Apple Silicon) with
a dependency-resolution error rather than an obvious one.

Note that a build from a clone uses whatever branch you checked out; `develop`
is the default branch, so pull the published image if you want the released
version.

## Storage backends

ChromaDB is the default and needs no configuration. MemPalace also ships a
pluggable backend contract, exercised across deliberately different substrates
so the contract is never accidentally shaped around one vendor. Every
non-default backend is opt-in.

| Backend | Mode | Install | Namespaces | Lexical | Configure with |
| ------- | ---- | ------- | :--------: | :-----: | -------------- |
| `chroma` _(default)_ | Local (embedded) | bundled | – | ✓ | – |
| `sqlite_exact` | Local (exact NumPy) | bundled | – | ✓ | – |
| `rust_exact` | Local (native vectors) | wheel / compiled | – | ✓ | – |
| `milvus` | Local (Lite) · Server opt-in | `mempalace[milvus]` | ✓ | ✓ | `MEMPALACE_MILVUS_URI` |
| `qdrant` | Server (REST) | bundled | ✓ | ✓ | `MEMPALACE_QDRANT_URL` |
| `pgvector` | Server (Postgres) | `mempalace[pgvector]` | ✓ | ✓ | `MEMPALACE_PGVECTOR_DSN` |

Select with `--backend <name>`, `MEMPALACE_BACKEND=<name>`, or
`"backend": "<name>"` in `config.json`. `rust_exact` uses the exact same `sqlite_exact.sqlite3` file on disk as `sqlite_exact` with zero data migration. See [native installation and vector CLI usage](crates/README.md) for the separately distributed wheel and executables.

### Native vector search

`rust_exact` and the standalone `mempalace-native` CLI scan the same `sqlite_exact` database with a native Rust engine. The `rust_exact` adapter falls back to the Python backend for complex filters, requests for returned embeddings, and installs without the native extension; the `mempalace-native` executable is Rust-only and has no Python fallback. No benchmark figures are published for this release; `mempalace-native bench --db <sqlite_exact.sqlite3>` measures it on your own data. See [`crates/`](crates/) for the core workspace, PyO3 bindings, and native CLI.

## Quickstart

```bash
# Mine content into the palace
mempalace mine ~/projects/myapp                    # project files
mempalace mine ~/.claude/projects/ --mode convos   # Claude Code sessions (scope with --wing per project)

# Search
mempalace search "why did we switch to GraphQL"

# Load context for a new session
mempalace wake-up
```

For Claude Code, Gemini CLI, [Antigravity](https://mempalaceofficial.com/guide/antigravity.html),
MCP-compatible tools, and local models, see
[mempalaceofficial.com/guide/getting-started](https://mempalaceofficial.com/guide/getting-started.html).

---

## Benchmarks

All numbers below are reproducible from this repository with the commands
in [`benchmarks/BENCHMARKS.md`](benchmarks/BENCHMARKS.md). Full
per-question result files are committed under `benchmarks/results_*`.

**LongMemEval — retrieval recall (R@5, 500 questions):**

| Mode | R@5 | LLM required |
|---|---|---|
| Raw (semantic search, no heuristics, no LLM) | **96.6%** | None |
| Hybrid v4, held-out 450q (tuned on 50 dev, not seen during training) | **98.4%** | None |
| Hybrid v4 + LLM rerank (full 500) | ≥99% | Any capable model |

The raw 96.6% requires no API key, no cloud, and no LLM at any stage. The
hybrid pipeline adds keyword boosting, temporal-proximity boosting, and
preference-pattern extraction; the held-out 98.4% is the honest
generalisable figure.

The rerank pipeline promotes the best candidate out of the top-20
retrieved sessions using an LLM reader. It works with any reasonably
capable model — we have reproduced it with Claude Haiku, Claude Sonnet,
and minimax-m2.7 via Ollama Cloud (no Anthropic dependency). The gap
between raw and reranked is model-agnostic; we do not headline a "100%"
number because the last 0.6% was reached by inspecting specific wrong
answers, which `benchmarks/BENCHMARKS.md` flags as teaching to the test.

**Other benchmarks (full results in [`benchmarks/BENCHMARKS.md`](benchmarks/BENCHMARKS.md)):**

| Benchmark | Metric | Score | Notes |
|---|---|---|---|
| LoCoMo (session, top-10, no rerank) | R@10 | 60.3% | 1,986 questions |
| LoCoMo (hybrid v5, top-10, no rerank) | R@10 | 88.9% | Same set |
| ConvoMem (all categories, 250 items) | Avg recall | 92.9% | 50 per category |
| MemBench (ACL 2025, 8,500 items) | R@5 | 80.3% | All categories |

We deliberately do not include a side-by-side comparison against Mem0,
Mastra, Hindsight, Supermemory, or Zep. Those projects publish different
metrics on different splits, and placing retrieval recall next to
end-to-end QA accuracy is not an honest comparison. See each project's
own research page for their published numbers.

**Reproducing every result:**

```bash
git clone https://github.com/MemPalace/mempalace.git
cd mempalace
uv sync --extra dev   # or: pip install -e ".[dev]"
# see benchmarks/README.md for dataset download commands
uv run python benchmarks/longmemeval_bench.py /path/to/longmemeval_s_cleaned.json
```

---

## Knowledge graph

MemPalace includes a temporal entity-relationship graph with validity
windows — add, query, invalidate, timeline — backed by local SQLite.
Usage and tool reference:
[mempalaceofficial.com/concepts/knowledge-graph](https://mempalaceofficial.com/concepts/knowledge-graph.html).

## MCP server

45 MCP tools cover palace reads/writes, knowledge-graph operations,
cross-wing navigation, drawer management, agent diaries, and agent
coordination (logstream events + artifact handoffs). Installation
and the full tool list:
[mempalaceofficial.com/reference/mcp-tools](https://mempalaceofficial.com/reference/mcp-tools.html).

## Agents

Each specialist agent gets its own wing and diary in the palace.
Discoverable at runtime via `mempalace_list_agents` — no bloat in your
system prompt:
[mempalaceofficial.com/concepts/agents](https://mempalaceofficial.com/concepts/agents.html).

## Auto-save hooks

Auto-save hooks for **Claude Code, Codex CLI, and Cursor IDE** save
periodically and before context compression:

- Claude Code + Codex →
  [mempalaceofficial.com/guide/hooks](https://mempalaceofficial.com/guide/hooks.html)
- Cursor IDE (adds session-start recall and a transcript snapshot before
  compaction) →
  [mempalaceofficial.com/guide/cursor-hooks](https://mempalaceofficial.com/guide/cursor-hooks.html)

If you are installing under time pressure, start with the
[Claude Code retention setup checklist](https://mempalaceofficial.com/guide/claude-code-retention.html):
wire the hooks, back up existing JSONL transcripts, and backfill them with
`mempalace mine ~/.claude/projects/ --mode convos`.

For per-message recall on top of the file-level chunks the hooks produce,
run `mempalace sweep <transcript-dir>` periodically — it stores one
verbatim drawer per user/assistant message, idempotent and resume-safe.

---

## Requirements

- Python 3.9+
- A vector-store backend (ChromaDB by default)
- ~300 MB disk for the embedding model. Onboarding (`python -m mempalace.onboarding`) offers `embeddinggemma-300m` (multilingual, 100+ languages, recommended) or `all-MiniLM-L6-v2` (English-only, ~30 MB). See the docstring at [`mempalace/embedding.py`](mempalace/embedding.py) for details and migration notes.
- Optional — compute embeddings on a server instead of locally. Set `embedding_model: "openai-compat"` in `~/.mempalace/config.json` together with `embedding_api_url` / `embedding_api_model` (and `embedding_api_key` if the server needs auth) to use any OpenAI-compatible `/v1/embeddings` endpoint — LM Studio, llama.cpp, vLLM, Ollama's OpenAI shim, or a self-hosted server (e.g. a larger multilingual or GPU-served embedder). Each key is overridable via the matching `MEMPALACE_EMBEDDING_API_*` env var. When the endpoint is on your machine or LAN, no content leaves your network. Switching to it requires `mempalace repair rebuild-index` (different vector space).

No API key is required for the core benchmark path.

## Docs

- Getting started → [mempalaceofficial.com/guide/getting-started](https://mempalaceofficial.com/guide/getting-started.html)
- CLI reference → [mempalaceofficial.com/reference/cli](https://mempalaceofficial.com/reference/cli.html)
- Python API → [mempalaceofficial.com/reference/python-api](https://mempalaceofficial.com/reference/python-api.html)
- Full benchmark methodology → [benchmarks/BENCHMARKS.md](benchmarks/BENCHMARKS.md)
- Release notes → [CHANGELOG.md](CHANGELOG.md)
- Corrections and public notices → [docs/HISTORY.md](docs/HISTORY.md)

## Contributing

PRs welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).

<!-- Link Definitions -->
[version-shield]: https://img.shields.io/badge/version-3.10.0-4dc9f6?style=flat-square&labelColor=0a0e14
[release-link]: https://github.com/MemPalace/mempalace/releases
[python-shield]: https://img.shields.io/badge/python-3.9+-7dd8f8?style=flat-square&labelColor=0a0e14&logo=python&logoColor=7dd8f8
[python-link]: https://www.python.org/
[license-shield]: https://img.shields.io/badge/license-MIT-b0e8ff?style=flat-square&labelColor=0a0e14
[license-link]: https://github.com/MemPalace/mempalace/blob/main/LICENSE
[discord-shield]: https://img.shields.io/badge/discord-join-5865F2?style=flat-square&labelColor=0a0e14&logo=discord&logoColor=5865F2
[discord-link]: https://discord.com/invite/ycTQQCu6kn
