#!/bin/sh
# One entry point for cron and for hand runs. Run it from the data dir (the one
# holding .env, posts/ and materials/); everything reads its settings from there.
#
#   run.sh sync    [--no-bcc|--dry-run ...]  fetch new posts, mail and post the summaries
#   run.sh serve   [--port N ...]            the MCP server over HTTP on 127.0.0.1 (long-running)
#   run.sh bot                               the Slack bot (long-running, needs serve)
#   run.sh reindex                           rebuild the semantic search index
set -e
REPO="$(cd "$(dirname "$0")" && pwd)"
PY="$REPO/.venv/bin/python"
# cron's PATH has no ~/.local/bin, where the Claude Code installer puts `claude`.
export PATH="$HOME/.local/bin:$PATH"
export PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

cmd="${1:-}"
[ $# -gt 0 ] && shift
case "$cmd" in
  sync)    exec "$PY" "$REPO/app/main.py" "$@" ;;
  serve)   exec "$PY" "$REPO/app/mcp_server.py" --data-dir "$PWD" --http --host 127.0.0.1 "$@" ;;
  bot)     exec "$PY" "$REPO/app/slack_bot.py" "$@" ;;
  reindex) exec "$PY" "$REPO/app/mcp_server.py" --data-dir "$PWD" --build-index "$@" ;;
  *) echo "usage: run.sh sync|serve|bot|reindex [args...]" >&2; exit 2 ;;
esac
