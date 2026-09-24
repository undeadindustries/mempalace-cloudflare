# AGENTS.md — MemPalace-Cloudflare Project Orientation

Read this file first in every session. A fresh agent must be able to continue work without asking orientation questions.

If `AGENTS.local.md` exists, read it next. It is gitignored and holds machine- and account-specific notes (live Worker URL, local paths, pending credential work). Never copy its contents into tracked files: this repository is public.

`CLAUDE.md` is upstream's orientation file. Treat it as read-only so upstream merges stay clean.

## Identity

Act as a senior Python engineer with decades of deep experience in CPython, packaging (pip, venv, uv, pyproject), and the Cloudflare serverless platform (Workers Python runtime, Vectorize, D1, R2, Workers AI, wrangler). Serverless discipline is the default mindset: every handler is stateless, idempotent, and cold-start aware. You are an engineering peer, not a yes-bot. Disagree openly when a better approach exists and give the concrete reason.

## Operating Principles (Non-Negotiable)

1. **Upstream stays pristine.** Zero modifications to upstream engine files (`mempalace/palace/`, `mempalace/backends/base.py`, `pyproject.toml`, `CLAUDE.md`, etc.). All fork code is additive: new files only, registered dynamically at runtime via the existing backend registry and configuration — never hardcoded into upstream logic.
2. **Verbatim always.** Inherited engine invariant: never summarize, paraphrase, or lossy-compress user content. Drawers store exact words; the index points at them.
3. **Incremental only.** Append-only ingest. A crash mid-operation must leave existing data untouched — in this fork, that includes R2 objects, Vectorize vectors, and D1 rows.
4. **Auth on every route.** Every route except `/healthz` validates `Authorization: Bearer <token>` against `env.MEMPALACE_API_KEY` (Cloudflare secret). Missing or invalid token → `401`. Unset secret → `503`.
5. **Research-first.** Verify Cloudflare Python Workers APIs, binding names, and runtime limits against current official docs before writing code. Tag claims [Certain] / [Likely] / [Guessing].
6. **Model the unhappy path.** Partial failures across Vectorize/D1/R2 are the norm, not the exception. Design for retryable, idempotent operations.
7. **No secrets or account identifiers in git.** API keys live in `wrangler secret`. `wrangler.toml`, `.env`, `.dev.vars*` and `AGENTS.local.md` are gitignored. Do not commit the Worker hostname, D1 ids, account ids, or local paths.
8. **Concise.** No filler, no preamble. Conventional commits (`fix:`, `feat:`, `docs:`, `ci:`). Never hard-reset.

## Project Overview

| Field | Value |
|---|---|
| Name | mempalace-cloudflare |
| Origin | https://github.com/undeadindustries/mempalace-cloudflare (public) |
| Upstream | https://github.com/MemPalace/mempalace |
| Fork trunk | `main` (GitHub default branch) |
| Purpose | Host MemPalace on Cloudflare Workers (Python runtime) so developers who work on many machines keep one palace in their own Cloudflare account instead of installing MemPalace everywhere |
| License | MIT |

The fork is never contributed back upstream. Only the maintainers develop and use it.

## Branch Model

- `main` — the fork. All fork work lands here. Upstream fixes arrive by `git fetch upstream && git merge upstream/develop` into `main`.
- `origin/develop` — a mirror of `upstream/develop`. Do not commit fork code to it.
- `origin/main` before the fork landed was upstream's stale 3.3.6 release branch; it is now the fork trunk.
- Feature branches branch from `main` and merge back into it.

## What MemPalace Is (Upstream Engine)

MemPalace is a verbatim memory system for AI — not a search engine or RAG wrapper. It stores every word exactly as said and returns exact words. Structure: **Wings** (people/projects) → **Rooms** (days/sessions) → **Drawers** (verbatim chunks), with an **AAAK** compressed index layer and a temporal entity-relationship **knowledge graph**. 100% recall is the design requirement. Full engine details live in `CLAUDE.md`.

## Repository Layout (fork files only)

```
AGENTS.md                      # This file (tracked)
AGENTS.local.md                # Machine/account notes (gitignored)
wrangler.toml.example          # Worker config template; wrangler.toml is generated and gitignored
.env.example                   # Wrangler credential template; .env is gitignored
migrations/0001_kg.sql         # D1 knowledge graph tables
migrations/0002_registry.sql   # D1 drawer registry table
scripts/cloudflare_bootstrap.sh  # Idempotent resource setup; writes wrangler.toml
scripts/test_smoke.py          # Live smoke test (healthz, 401, status, MCP tools/list)
mempalace/backends/cloudflare_vectorize.py  # BaseBackend adapter, registered via the upstream registry
mempalace/cloudflare/          # Everything bundled into the Worker
  entrypoint.py                # ASGI app, Bearer middleware, REST routes, MCP Streamable HTTP
  tools.py                     # 25 MCP tools
  vectorize_collection.py      # Vectorize + R2 + D1 coordination
  search.py                    # Vectorize ANN candidates + BM25 re-ranking
  d1_kg.py / d1_registry.py    # Knowledge graph and drawer registry on D1
  r2_storage.py                # Verbatim drawer bodies in R2
  workers_ai.py                # @cf/baai/bge-small-en-v1.5 embeddings (384-dim)
  _shims.py / results.py       # Pyodide import shims, result shapes
client/                        # cloudflare-remote backend plugin for the mempalace CLI
tests/test_cloudflare_*.py     # Fork tests
```

## Meta-Skills and Governance

These live on the maintainers' machines, not in the repo. Use them when present.

| Skill / rule | Role |
|---|---|
| `~/.cursor/rules/agent-standards.mdc` | Always-on baseline: persona, hard stops, code quality, git discipline |
| `CLAUDE.md` (repo root) | Upstream engine architecture, invariants, conventions — read-only |
| `~/.cursor/skills/cloudflare-engineering/SKILL.md` | Workers/Vectorize/D1/R2 domain expertise — apply to all fork code |
| `~/.cursor/skills/python3/SKILL.md` | Python idioms and packaging standards |
| `~/.cursor/skills/systems-architect-expertise/SKILL.md` | Architecture and scale review |
| `~/.cursor/skills/mempalace/SKILL.md` | Engine domain knowledge (wings/rooms/drawers, AAAK, KG) |

## Architectural Decisions

1. **Additive-only fork pattern.** All Cloudflare functionality is in new files: one backend adapter in `mempalace/backends/cloudflare_vectorize.py` (beside sibling backends for registry discovery), everything else under `mempalace/cloudflare/`, `client/`, `migrations/`, `scripts/`. Registration is dynamic via `backends/registry.py` + config/env. Success test: `git diff upstream/develop...main --stat` lists only fork files plus the marked blocks in decision 4.
2. **Cloudflare primitive mapping.** Vectorize = vector search (replaces ChromaDB); D1 = knowledge graph + drawer registry (replaces local SQLite); Workers AI `@cf/baai/bge-small-en-v1.5` (384-dim) = embeddings; R2 = verbatim drawer bodies.
3. **ASGI entrypoint with Bearer middleware.** Auth runs before routing, so no route can ship unauthenticated by omission. MCP is Streamable HTTP on `POST /mcp` with JSON responses only: `GET`/`DELETE /mcp` return `405` with `Allow: POST`, notifications return `202` with no body, and `initialize` echoes the client's protocol version when supported (2025-06-18, 2025-03-26, 2024-11-05).
4. **Upstream files the fork edits, each in a marked block.** `README.md` (fork docs between `<!-- BEGIN mempalace-cloudflare fork section -->` and `<!-- END ... -->`; on conflict keep the block, take upstream below it) and `.gitignore` (`# BEGIN/END mempalace-cloudflare fork`; on conflict keep both). `AGENTS.md` replaces upstream's `AGENTS.md` → `CLAUDE.md` symlink with this real file; if upstream ever changes that symlink, resolve the conflict by keeping this file.
5. **Per-account config is never committed.** `wrangler.toml` and `.env` are gitignored; only `.example` templates are tracked. The bootstrap generates `wrangler.toml` by replacing `REPLACE_WITH_YOUR_D1_DATABASE_ID`. Resource names are duplicated as constants at the top of the bootstrap script and must match the template.
6. **AGENTS.md is tracked.** Reversed from an earlier local-only decision: AGENTS.md is the cross-tool standard (Linux Foundation Agentic AI Foundation; read by Cursor, Codex, Copilot, Gemini CLI), and the maintainers work across many machines. Account-specific details go in the gitignored `AGENTS.local.md`.
7. **Copied helpers.** Cloudflare Python Workers bundle only `mempalace/cloudflare/`, so the Worker cannot import the upstream engine at runtime. ID hashing, ISO date validation and search ranking are copied. After each upstream merge, check the diff of `mempalace/ids.py`, `mempalace/knowledge_graph.py` and `mempalace/searcher/` and port relevant fixes.

## Current Project Status

- Cloudflare-native MemPalace v1 is deployed and passing the live smoke test.
- 27/27 Cloudflare tests pass (adapters, entrypoint, MCP protocol, client plugin, verbatim fidelity). Upstream suite last run: 5644 passed, 1 pre-existing failure in `tests/test_embedding.py` (ChromaDB ONNX provider mock, unrelated to the fork).
- Free-tier cost target: $0/month for one developer.

## Completed Milestones

- [x] Adapters: Workers AI, R2, D1 knowledge graph, D1 registry, Vectorize backend
- [x] Hybrid search (Vectorize candidates + edge BM25 re-ranking)
- [x] ASGI entrypoint + Bearer middleware + MCP Streamable HTTP (25 tools)
- [x] `client/` cloudflare-remote plugin
- [x] Bootstrap script, migrations, smoke test
- [x] Fix 7 Bugbot findings (auth fail-closed, remote ID preservation, verbatim hydration, Vectorize upsert, delete-by-source ordering, duplicate scoping, atomic D1 supersede)
- [x] Fix 5 Bugbot findings (202 notifications, `kg_add` without `valid_from`, Vectorize → D1 → R2 delete order with ghost-hit filtering, checkpoint `source_file`, request body bytes)
- [x] Config templates: `wrangler.toml` gitignored and scrubbed from history before first push; `wrangler.toml.example` + `.env.example`; idempotent bootstrap that generates `wrangler.toml`
- [x] `GET`/`DELETE /mcp` → 405; MCP protocol version negotiation

## Open TODOs

- [ ] Verify Cursor end-to-end against the live Worker.
- [ ] `on_fetch` returns `str(exc)` in the 500 body; consider logging only and returning a generic message.

## Future Roadmap (v2 / Post-v1)

- [ ] Remote mining pipeline over HTTPS / chunked upload
- [ ] Mesh synchronization between multiple palaces
- [ ] Graph traversal and tunnel exploration over D1 recursive queries
