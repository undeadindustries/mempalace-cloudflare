# AGENTS.md — MemPalace-Cloudflare Project Orientation

Read this file first in every session. A fresh agent must be able to continue work without asking orientation questions.

This repository is public. The live Worker URL is printed by `npx wrangler deploy` and listed by `npx wrangler deployments list`; do not write it, or any other account-specific value, into tracked files.

`CLAUDE.md` is upstream's orientation file. Treat it as read-only so upstream merges stay clean.

## Identity

Act as a senior Python engineer with decades of deep experience in CPython, packaging (pip, venv, uv, pyproject), and the Cloudflare serverless platform (Workers Python runtime, Vectorize, D1, R2, Workers AI, wrangler). Serverless discipline is the default mindset: every handler is stateless, idempotent, and cold-start aware. You are an engineering peer, not a yes-bot. Disagree openly when a better approach exists and give the concrete reason.

## Operating Principles (Non-Negotiable)

1. **Upstream stays pristine.** Zero modifications to upstream engine files (`mempalace/palace/`, `mempalace/backends/base.py`, `pyproject.toml`, `CLAUDE.md`, etc.). All fork code is additive: new files only, registered dynamically at runtime via the existing backend registry and configuration — never hardcoded into upstream logic.
2. **Verbatim always.** Inherited engine invariant: never summarize, paraphrase, or lossy-compress user content. Drawers store exact words; the index points at them.
3. **Incremental only.** Append-only ingest. A crash mid-operation must leave existing data untouched — in this fork, that includes R2 objects, Vectorize vectors, and D1 rows.
4. **Auth on every route.** Every route except `/healthz` validates `Authorization: Bearer <token>` against `env.MEMPALACE_API_KEY` (Cloudflare secret). Missing or invalid token → `401`. Unset secret → `503`. The deployed Worker is also behind Cloudflare Access (decision 8), so every client must send the service-token headers too.
5. **Research-first.** Verify Cloudflare Python Workers APIs, binding names, and runtime limits against current official docs before writing code. Tag claims [Certain] / [Likely] / [Guessing].
6. **Model the unhappy path.** Partial failures across Vectorize/D1/R2 are the norm, not the exception. Design for retryable, idempotent operations.
7. **No secrets or account identifiers in git.** API keys live in `wrangler secret`. `wrangler.toml`, `.env` and `.dev.vars*` are gitignored. Do not commit the Worker hostname, D1 ids, account ids, or local paths.
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
wrangler.toml.example          # Worker config template; wrangler.toml is generated and gitignored
.env.example                   # Wrangler credential template; .env is gitignored
migrations/0001_kg.sql         # D1 knowledge graph tables
migrations/0002_registry.sql   # D1 drawer registry table
scripts/cloudflare_bootstrap.sh  # Idempotent resource setup; writes wrangler.toml
scripts/test_smoke.py          # Live smoke test (Access 403, healthz, 401, status, MCP tools/list); secrets from env
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

1. **Additive-only fork pattern.** All Cloudflare functionality is in new files: one backend adapter in `mempalace/backends/cloudflare_vectorize.py` (beside sibling backends for registry discovery), everything else under `mempalace/cloudflare/`, `client/`, `migrations/`, `scripts/`. Registration is dynamic via `backends/registry.py` + config/env. Success test: `git diff upstream/develop...main --stat` lists only fork files plus the marked blocks in decision 4. Known exception: `main` also carries the maintainers' open upstream PRs [#2538](https://github.com/MemPalace/mempalace/pull/2538), [#2539](https://github.com/MemPalace/mempalace/pull/2539) and [#2540](https://github.com/MemPalace/mempalace/pull/2540) (lease-holder message, loopback hub via `--ensure-hub`, Cursor transcript parser and silent save hook), merged ahead of upstream. Their files (`normalize.py`, `daemon.py`, `hub_bootstrap.py`, `mcp_proxy.py`, `mcp_server/_guards.py`, `hooks/cursor/`, and tests) show in that diff until upstream merges them; after that they drop out on the next upstream merge.
2. **Cloudflare primitive mapping.** Vectorize = vector search (replaces ChromaDB); D1 = knowledge graph + drawer registry (replaces local SQLite); Workers AI `@cf/baai/bge-small-en-v1.5` (384-dim) = embeddings; R2 = verbatim drawer bodies.
3. **ASGI entrypoint with Bearer middleware.** Auth runs before routing, so no route can ship unauthenticated by omission. MCP is Streamable HTTP on `POST /mcp` with JSON responses only: `GET`/`DELETE /mcp` return `405` with `Allow: POST`, notifications return `202` with no body, and `initialize` echoes the client's protocol version when supported (2025-06-18, 2025-03-26, 2024-11-05).
4. **Upstream files the fork edits, each in a marked block.** `README.md` (fork docs between `<!-- BEGIN mempalace-cloudflare fork section -->` and `<!-- END ... -->`; on conflict keep the block, take upstream below it) and `.gitignore` (`# BEGIN/END mempalace-cloudflare fork`; on conflict keep both). `AGENTS.md` replaces upstream's `AGENTS.md` → `CLAUDE.md` symlink with this real file; if upstream ever changes that symlink, resolve the conflict by keeping this file.
5. **Per-account config is never committed.** `wrangler.toml` and `.env` are gitignored; only `.example` templates are tracked. The bootstrap generates `wrangler.toml` by replacing `REPLACE_WITH_YOUR_D1_DATABASE_ID`. Resource names are duplicated as constants at the top of the bootstrap script and must match the template.
6. **AGENTS.md is tracked.** Reversed from an earlier local-only decision: AGENTS.md is the cross-tool standard (Linux Foundation Agentic AI Foundation; read by Cursor, Codex, Copilot, Gemini CLI), and the maintainers work across many machines. Account-specific values stay out of it (see principle 7).
7. **Copied helpers.** Cloudflare Python Workers bundle only `mempalace/cloudflare/`, so the Worker cannot import the upstream engine at runtime. ID hashing, ISO date validation and search ranking are copied. After each upstream merge, check the diff of `mempalace/ids.py`, `mempalace/knowledge_graph.py` and `mempalace/searcher/` and port relevant fixes.
8. **Cloudflare Access service token in front of the Worker.** Without it, junk requests still invoke the Worker (and get `401`), so a flood could exhaust the free plan's daily request allowance. Access checks each request at the edge before the Worker runs. Configured in the dashboard, not in code: Worker Access on **all traffic** (production and previews) with a **Service Auth** policy (an Allow policy would send callers to a browser login) holding one service token. Clients send `CF-Access-Client-Id` / `CF-Access-Client-Secret`, read from `CF_ACCESS_CLIENT_ID` / `CF_ACCESS_CLIENT_SECRET`; the client plugin raises `ValueError` if only one is set. The Worker does not validate the `Cf-Access-Jwt-Assertion` header itself; the bearer token remains the in-Worker check. `preview_urls = false` is set in the template so only the production hostname exists.
9. **Client requests always send a custom User-Agent.** Cloudflare's bot protection returned `403` to urllib's default `Python-urllib/*` agent on `/healthz` even with a valid service token. Every request in `client/` uses `USER_AGENT`.
10. **The harness owns its config file.** Docs say what goes in the MCP client's config (`mcp.json` headers as plain values; `${env:NAME}` shown only as an alternative). Protecting that file is the harness's job; the fork does not check file permissions or require environment variables for MCP clients. Keyed entries belong in user-level config, never in a project's `.cursor/mcp.json`.
11. **R2 bucket lock rejected.** A bucket lock prevents deleting and overwriting objects within retention periods, directly conflicting with verbatim drawer operations `mempalace_delete_drawer` and `mempalace_update_drawer`. Backups remain optional (via D1 Time Travel and manual exports).
12. **In-Worker Access JWT verification dropped.** Access checks every request at Cloudflare's edge on all hostnames. Preview URLs are disabled, and Worker Bearer token validation provides defence in depth. Validating RS256 JWT signatures inside the Worker Python runtime would require manual WebCrypto JWKS fetching on cold starts without significant security benefit.
13. **Local bge-small import.** `POST /api/drawers/batch` accepts up to 100 drawers with precomputed 384-dim embeddings so a palace import does not spend Workers AI neurons. `scripts/import_local_palace.py` reads the local Chroma palace and embeds with ONNX `Xenova/bge-small-en-v1.5`. Vectorize and Workers AI bindings must receive `pyodide.ffi.to_js` values (`jsutil.as_js`). Vectorize ids longer than 64 bytes are sha256 keys; the original id is stored on the vector metadata and in D1.
14. **D1 FTS5 Trigram Candidate Union for 100% Recall.** Vectorize's `topK <= 50` limit and dense embedding blindspots on exact mechanical tokens (code snippets, logs, hex IDs) caused a recall gap. Solved via SQLite FTS5 with `tokenize='trigram'` in `drawers_fts` on D1 (`migrations/0003_fts.sql`). In `execute_hybrid_search()`, Vectorize ANN candidates (top 50) and D1 FTS candidates (top 50) are fetched concurrently via `asyncio.gather()`, unioned by drawer ID, and re-ranked with Okapi BM25 and cosine similarity. Lexical-only candidates impute similarity from lexical alignment (`norm * 0.7`) so exact keyword matches decisively surface into top results alongside semantic matches.
15. **Sagittarius hooks + transcript parser (carried-PR-style).** `hooks/sagittarius/` (save/drain/precompress/session-end + `lib/common.sh` + `install.sh`, mirroring `hooks/cursor/`) and `_try_sagittarius_jsonl` in `mempalace/normalize.py` with tests. The parser change touches an upstream engine file, so it is carried exactly like the open upstream PRs in decision 1 (files: `normalize.py`, `tests/test_normalize.py`): intended to go upstream as a PR, drops out of the fork diff once merged. Everything else is additive fork files. The previous `SessionEnd` wiring (`mempal_session_end_hook.sh` with `MEMPALACE_HOOK_HARNESS=sagittarius`) never worked — `hooks_cli.py` rejects that harness — and is retired by the installer.

## Current Project Status

- Cloudflare-native MemPalace v1 is deployed behind Cloudflare Access and passing the live smoke test (Access 403 without the service token, then healthz, 401, status, 25 MCP tools).
- Hardened ASGI entrypoint: constant-time `hmac.compare_digest` on bearer tokens, generic 500 responses with request correlation IDs, validated and clamped paging (`MAX_PAGE_LIMIT = 100`) on REST and MCP tools, and Workers observability enabled.
- The Cloudflare palace holds two machines' palaces: 138,881 drawers across 68 wings and 2,075 rooms (D1 registry and Vectorize counts match). 55,705 came from the maintainer's Mac and 83,176 from a second machine (imported from a SQLite `.backup` snapshot so its live palace stayed read only). The two sets share no drawer ids or wings; 88 of its drawers repeat text already present under another id and were imported as-is. Random 40-drawer samples from each source matched verbatim. Closets (`mempalace_closets`) and the knowledge graphs were not imported. A third machine's palace (42,978 drawers across 11 wings) is currently importing via a read-only snapshot.
- D1 FTS5 trigram candidate union deployed and verified: exact keyword and code fragment matches (`renderFooter`, `probe-large-file`) reliably match via `matched_via: fts` or `both` and rank at the top, closing the search recall gap on mechanical and structured tokens.
- The account runs on Workers Paid ($5/month). The full palace exceeds the Free plan (Vectorize 5M stored dimensions, D1 100k row writes/day, 10 ms CPU per request).
- 46/46 Cloudflare tests pass (adapters, entrypoint, MCP protocol, client plugin, verbatim fidelity, hardening, config resolution, batch import, FTS candidate union).
- Known constraint: `uv.lock` is stale upstream (`pyproject.toml` pins ruff 0.16.6, lock says 0.16.1), so `uv run` rewrites it locally. Leave it out of fork commits until upstream brings a fresh lock.
- Known constraint: `scripts/import_local_palace.py --resume` continues from a Chroma page offset, and Chroma does not guarantee insertion order, so it is not an incremental sync. To pick up drawers added locally after the import, run it without `--resume`; batches upsert by drawer id.

## Completed Milestones

- [x] Adapters: Workers AI, R2, D1 knowledge graph, D1 registry, Vectorize backend
- [x] Hybrid search (Vectorize candidates + edge BM25 re-ranking)
- [x] ASGI entrypoint + Bearer middleware + MCP Streamable HTTP (25 tools)
- [x] `client/` cloudflare-remote plugin with options > env > `~/.mempalace/config.json` priority
- [x] Bootstrap script, migrations, smoke test
- [x] Fix 7 Bugbot findings (auth fail-closed, remote ID preservation, verbatim hydration, Vectorize upsert, delete-by-source ordering, duplicate scoping, atomic D1 supersede)
- [x] Fix 5 Bugbot findings (202 notifications, `kg_add` without `valid_from`, Vectorize → D1 → R2 delete order with ghost-hit filtering, checkpoint `source_file`, request body bytes)
- [x] Config templates: `wrangler.toml` gitignored and scrubbed from history before first push; `wrangler.toml.example` + `.env.example`; idempotent bootstrap that generates `wrangler.toml`
- [x] `GET`/`DELETE /mcp` → 405; MCP protocol version negotiation
- [x] Cloudflare Access service token: client plugin, smoke test and README send the headers; preview URLs off
- [x] Security hardening: `hmac.compare_digest`, generic 500 with request id, clamped paging, Workers observability
- [x] Verified Cursor client configuration in `~/.cursor/mcp.json`
- [x] Imported the local palace (55,705 drawers) with local bge-small embeddings, 0 Workers AI neurons. 34,232 drawer ids exceed Vectorize's 64-byte id limit; those use a sha256 vector key while D1 and R2 keep the original id. On the Free plan the importer needs `--batch 5` (CPU limit, error 1102); the default of 20 is for Paid.
- [x] Imported a second machine's palace (83,176 drawers) with `--palace <snapshot dir> --progress <separate file>`, about 2.5 hours, no retries.
- [/] Importing a third machine's palace (42,978 drawers) with `--palace /tmp/5090s-palace --progress ~/.mempalace/cloudflare-import-progress-5090s.json --resume`.
- [x] D1 FTS5 trigram candidate union (`migrations/0003_fts.sql`, `d1_registry.py`, `search.py`, `vectorize_collection.py`, `scripts/backfill_d1_fts.py`) to close the search recall gap on code snippets, log fragments, and exact identifiers.
- [x] Sagittarius hooks (`hooks/sagittarius/`: AfterAgent/SessionStart/PreCompress/SessionEnd, wing inference from `cwd`, stateless `turn_index` gating, single-file mines) + `_try_sagittarius_jsonl` parser. Live-wired into `~/.sagittarius/settings.json` (retired `mempal-autosave.sh`/`mempal-drain.sh` wiring and the broken harness-based session-end).
- [x] Scoped `mempalace_search` description to past sessions (<200 runes) and annotated 15 read-only tools with `readOnlyHint: true` for Sagittarius AD-133 trust mode admission. Deployed and verified via smoke test.

## Open TODOs

- [ ] Decide whether to import the local knowledge graph (SQLite) into D1 and the closets (`mempalace_closets`, ~1.8k).

## Future Roadmap (v2 / Post-v1)

- [ ] Remote mining pipeline over HTTPS / chunked upload
- [ ] Mesh synchronization between multiple palaces
- [ ] Graph traversal and tunnel exploration over D1 recursive queries
