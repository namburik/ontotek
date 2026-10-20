#!/bin/ksh
# Kills whatever is listening on the OntoTek UI port, then relaunches
# app/ontotek_ui.py in the background.
#
# Run: ./run.ksh
set -e

cd "$(dirname "$0")"

# server.py's build_ontology() hits OpenMetadata (and Cube serves the actual
# queries) at UI startup, so those containers need to already be up.
echo "Starting docker services (OpenMetadata, Cube, Postgres)..."
docker compose up -d

PORT=8765
LOG=/tmp/ontotek_ui.log

# .venv-tools only has the OpenMetadata/Cube loader tooling deps (requests,
# psycopg2-binary, python-dotenv — see loader/requirements.txt), never
# app/'s own runtime deps (langchain, mcp, ...). If it's active in the
# caller's shell, strip it so `python3` below resolves to an interpreter
# that actually has requirements.txt installed, instead of failing deep
# inside ontotek_agent.py's import with only a log file to explain why.
case "${VIRTUAL_ENV:-}" in
    */.venv-tools)
        echo "Note: .venv-tools was active in this shell — bypassing it for app/'s own deps."
        PATH=$(printf '%s' "$PATH" | tr ':' '\n' | grep -v "^${VIRTUAL_ENV}/bin\$" | tr '\n' ':')
        PATH=${PATH%:}
        unset VIRTUAL_ENV
        ;;
esac

if ! python3 -c "import langchain_core, mcp, jwt, dotenv" 2>/dev/null; then
    echo "ERROR: $(command -v python3) is missing app/'s deps (langchain_core/mcp/PyJWT/python-dotenv)." >&2
    echo "Run from the project root (not inside .venv-tools): pip install -r requirements.txt" >&2
    exit 1
fi

PIDS=$(lsof -ti "tcp:${PORT}" 2>/dev/null || true)
if [ -n "$PIDS" ]; then
    echo "Killing existing process(es) on port ${PORT}: ${PIDS}"
    kill $PIDS
    sleep 1
fi

nohup python3 app/ontotek_ui.py > "$LOG" 2>&1 &
disown

echo "OntoTek UI restarted (pid $!) — http://localhost:${PORT}"
echo "Log: ${LOG}"
