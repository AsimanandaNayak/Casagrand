#!/usr/bin/env bash
# Render expects the process to bind 0.0.0.0:$PORT — never use --reload here.
set -euo pipefail
exec uvicorn main:app --host 0.0.0.0 --port "${PORT:?PORT must be set by Render}"
