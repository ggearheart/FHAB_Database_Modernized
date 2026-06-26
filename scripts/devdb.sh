#!/usr/bin/env bash
# Start a local PostgreSQL 17 + PostGIS dev cluster and create the fhab database.
# Idempotent: safe to re-run. Requires `brew install postgresql@17 postgis`.
set -euo pipefail

PG_PREFIX="$(brew --prefix postgresql@17)"
PGIS_PREFIX="$(brew --prefix postgis)"
export PATH="$PG_PREFIX/bin:$PATH"
export LC_ALL="${LC_ALL:-en_US.UTF-8}" LANG="${LANG:-en_US.UTF-8}"
PGDATA="${PGDATA:-$PG_PREFIX/../../var/postgresql@17}"
PORT="${PGPORT:-5432}"
DB="${FHAB_DB:-fhab}"

# The Homebrew postgis formula installs its extension files in its own keg; make them
# visible to the postgresql@17 server (idempotent copy).
cp -n "$PGIS_PREFIX"/lib/postgresql@17/*.dylib "$PG_PREFIX/lib/postgresql/" 2>/dev/null || true
cp -n "$PGIS_PREFIX"/share/postgresql@17/extension/* "$PG_PREFIX/share/postgresql/extension/" 2>/dev/null || true

if ! pg_isready -p "$PORT" >/dev/null 2>&1; then
  echo "Starting PostgreSQL on port $PORT…"
  pg_ctl -D "$PGDATA" -l /tmp/fhab_pg.log -o "-p $PORT" start
  sleep 2
fi

createdb -p "$PORT" "$DB" 2>/dev/null && echo "Created database '$DB'" || echo "Database '$DB' already exists"
psql -p "$PORT" -d "$DB" -c "CREATE EXTENSION IF NOT EXISTS postgis;" >/dev/null
echo "Ready. Connect with: FHAB_DATABASE_URL='dbname=$DB host=/tmp port=$PORT'"
