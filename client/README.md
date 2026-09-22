# MemPalace Cloudflare Remote Backend Plugin

Allows local MemPalace tools (CLI, local MCP server, hooks) to speak directly to a
remote MemPalace instance deployed on Cloudflare Workers.

## Installation

```bash
pip install -e client/
```

## Configuration

Set environment variables:

```bash
export MEMPALACE_BACKEND=cloudflare-remote
export MEMPALACE_CLOUDFLARE_URL=https://mempalace-cf.<your-subdomain>.workers.dev
export MEMPALACE_CLOUDFLARE_TOKEN=your-secret-api-key
```

Now `mempalace search`, `mempalace status`, and MCP tools will route transparently
to your serverless Cloudflare Workers deployment.
