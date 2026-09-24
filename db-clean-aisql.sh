#!/bin/sh

DIR="$(cd "$(dirname "$0")" && pwd)"
PY="/Users/aleksei/Work/scripts/parallels-sql-admins/.venv/bin/python3"

exec "$PY" "$DIR/db-clean-aisql.py" --commit "$@"