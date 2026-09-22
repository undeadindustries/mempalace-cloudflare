#!/usr/bin/env bash
# ==============================================================================
# Cloudflare Infrastructure Bootstrap Script for MemPalace
# ==============================================================================
# Sets up Vectorize (384d, cosine), metadata indexes, D1 database & migrations,
# R2 bucket, and secret API key.
# ==============================================================================

set -euo pipefail

echo "==> 1. Checking Wrangler CLI..."
if ! command -v wrangler &> /dev/null && ! command -v npx &> /dev/null; then
    echo "Error: wrangler or npx not found. Please install node/npm or wrangler."
    exit 1
fi

WRANGLER="npx wrangler"
if command -v wrangler &> /dev/null; then
    WRANGLER="wrangler"
fi

echo "Using: $WRANGLER"

echo "==> 2. Creating Cloudflare Vectorize Index (384-dimensional, cosine)..."
$WRANGLER vectorize create mempalace-index --dimensions=384 --metric=cosine || true

echo "==> 3. Creating Vectorize Metadata Indexes (must be created before first insert)..."
$WRANGLER vectorize create-metadata-index mempalace-index --property-name=wing --type=string || true
$WRANGLER vectorize create-metadata-index mempalace-index --property-name=room --type=string || true
$WRANGLER vectorize create-metadata-index mempalace-index --property-name=source_file --type=string || true

echo "==> 4. Creating Cloudflare D1 Database..."
D1_OUTPUT=$($WRANGLER d1 create mempalace-kg || true)
echo "$D1_OUTPUT"
echo "Note: If newly created, copy the database_id printed above into wrangler.toml under [[d1_databases]]."

echo "==> 5. Applying D1 Database Migrations..."
$WRANGLER d1 execute mempalace-kg --file=migrations/0001_kg.sql --yes || true
$WRANGLER d1 execute mempalace-kg --file=migrations/0002_registry.sql --yes || true

echo "==> 6. Creating Cloudflare R2 Bucket for verbatim drawer bodies..."
$WRANGLER r2 bucket create mempalace-drawers || true

echo "==> 7. Setting MEMPALACE_API_KEY secret..."
echo "Enter the secret Bearer token for MemPalace access:"
$WRANGLER secret put MEMPALACE_API_KEY

echo "==> Bootstrap complete! Deploy using: $WRANGLER deploy"
