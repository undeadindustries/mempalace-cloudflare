#!/usr/bin/env bash
# ==============================================================================
# Cloudflare bootstrap for the MemPalace Worker
# ==============================================================================
# Creates the Vectorize index and its metadata indexes, the D1 database and its
# tables, the R2 bucket, and the MEMPALACE_API_KEY secret, then writes
# wrangler.toml from wrangler.toml.example with your D1 database id.
#
# Safe to re-run. Each step checks whether its resource already exists and
# creates only what is missing, so a run that fails halfway can be repeated.
# Nothing is deleted or overwritten; a wrangler.toml that points at a
# different D1 database stops the script instead of being replaced.
#
# Auth: a .env file with CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID (see
# .env.example), or a prior `npx wrangler login`.
#
# Usage: scripts/cloudflare_bootstrap.sh
# ==============================================================================

set -euo pipefail

# Keep these in sync with wrangler.toml.example.
readonly VECTORIZE_INDEX="mempalace-index"
readonly VECTOR_DIMENSIONS=384
readonly VECTOR_METRIC="cosine"
readonly METADATA_PROPERTIES=(wing room source_file)
readonly D1_DATABASE="mempalace-kg"
readonly R2_BUCKET="mempalace-drawers"
readonly SECRET_NAME="MEMPALACE_API_KEY"

readonly CONFIG_FILE="wrangler.toml"
readonly CONFIG_TEMPLATE="wrangler.toml.example"
readonly D1_ID_PLACEHOLDER="REPLACE_WITH_YOUR_D1_DATABASE_ID"
readonly MIGRATION_FILES=(migrations/0001_kg.sql migrations/0002_registry.sql)
readonly UUID_PATTERN='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'

step() { printf '\n==> %s\n' "$*"; }
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if command -v wrangler >/dev/null 2>&1; then
    WRANGLER=(wrangler)
elif command -v npx >/dev/null 2>&1; then
    WRANGLER=(npx --yes wrangler)
else
    die "neither wrangler nor npx is on PATH. Install Node.js first."
fi
command -v python3 >/dev/null 2>&1 || die "python3 is required to read Wrangler's JSON output."
[[ -f "$CONFIG_TEMPLATE" ]] || die "$CONFIG_TEMPLATE not found. Run this from a full checkout."

wr() { "${WRANGLER[@]}" "$@"; }

# Prints one value per line: the given key from each object in a JSON array
# read on stdin. Wrangler's --json output is the only stable way to check
# whether a resource exists; its table output changes between releases.
json_array_values() {
    python3 -c '
import json, sys
key = sys.argv[1]
for item in json.load(sys.stdin):
    print(item.get(key, ""))
' "$1"
}

# Prints the uuid of the named D1 database, or nothing if it does not exist.
d1_database_id() {
    wr d1 list --json | python3 -c '
import json, sys
name = sys.argv[1]
print(next((db["uuid"] for db in json.load(sys.stdin) if db.get("name") == name), ""))
' "$D1_DATABASE"
}

echo "Using: ${WRANGLER[*]}"

step "1/7 Vectorize index $VECTORIZE_INDEX (${VECTOR_DIMENSIONS}d, $VECTOR_METRIC)"
if wr vectorize get "$VECTORIZE_INDEX" >/dev/null 2>&1; then
    echo "Already exists."
else
    wr vectorize create "$VECTORIZE_INDEX" --dimensions="$VECTOR_DIMENSIONS" --metric="$VECTOR_METRIC"
fi

step "2/7 Vectorize metadata indexes (${METADATA_PROPERTIES[*]})"
existing_properties=$(wr vectorize list-metadata-index "$VECTORIZE_INDEX" --json | json_array_values propertyName)
for property in "${METADATA_PROPERTIES[@]}"; do
    if grep -qxF "$property" <<<"$existing_properties"; then
        echo "$property: already exists."
    else
        wr vectorize create-metadata-index "$VECTORIZE_INDEX" --property-name="$property" --type=string
    fi
done

step "3/7 D1 database $D1_DATABASE"
d1_id=$(d1_database_id)
if [[ -n "$d1_id" ]]; then
    echo "Already exists."
else
    wr d1 create "$D1_DATABASE"
    d1_id=$(d1_database_id)
fi
[[ "$d1_id" =~ $UUID_PATTERN ]] || die "could not read the id of D1 database $D1_DATABASE (got '$d1_id')."
echo "database_id: $d1_id"

step "4/7 $CONFIG_FILE"
if [[ ! -f "$CONFIG_FILE" ]]; then
    source_file="$CONFIG_TEMPLATE"
elif grep -qF "$D1_ID_PLACEHOLDER" "$CONFIG_FILE"; then
    source_file="$CONFIG_FILE"
elif grep -qF "\"$d1_id\"" "$CONFIG_FILE"; then
    source_file=""
    echo "Already points at $D1_DATABASE."
else
    die "$CONFIG_FILE has a different database_id than D1 database $D1_DATABASE ($d1_id). Fix it by hand."
fi
if [[ -n "$source_file" ]]; then
    tmp_config=$(mktemp "${CONFIG_FILE}.tmp.XXXXXX")
    trap 'rm -f "$tmp_config"' EXIT
    sed "s/$D1_ID_PLACEHOLDER/$d1_id/" "$source_file" >"$tmp_config"
    mv "$tmp_config" "$CONFIG_FILE"
    trap - EXIT
    echo "Wrote $CONFIG_FILE from $source_file."
fi

step "5/7 D1 tables (migrations use IF NOT EXISTS, so re-running is safe)"
for migration in "${MIGRATION_FILES[@]}"; do
    wr d1 execute "$D1_DATABASE" --remote --yes --file="$migration"
done

step "6/7 R2 bucket $R2_BUCKET"
if wr r2 bucket info "$R2_BUCKET" >/dev/null 2>&1; then
    echo "Already exists."
else
    wr r2 bucket create "$R2_BUCKET"
fi

step "7/7 Worker secret $SECRET_NAME"
secret_is_set=false
if secret_json=$(wr secret list --format json); then
    if json_array_values name <<<"$secret_json" | grep -qxF "$SECRET_NAME"; then
        secret_is_set=true
    fi
else
    echo "Could not list secrets. On a first run the Worker does not exist yet, which is expected."
fi
if [[ "$secret_is_set" == true ]]; then
    echo "Already set. To rotate it: ${WRANGLER[*]} secret put $SECRET_NAME"
else
    echo "Paste a long random token (for example: openssl rand -hex 32)."
    echo "Keep a copy in your password manager; Cloudflare will not show it again."
    wr secret put "$SECRET_NAME"
fi

step "Bootstrap complete. Deploy with: ${WRANGLER[*]} deploy"
