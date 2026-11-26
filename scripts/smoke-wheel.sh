#!/bin/sh
# Smoke-test a built wheel the way production installs it: a clean virtualenv, a
# non-editable install, `migrate` on a temp data dir, then GET a page and a static file.
# Usage: scripts/smoke-wheel.sh path/to/lanscape_atlas-*.whl
set -eu

wheel=${1:?usage: smoke-wheel.sh WHEEL}
work=$(mktemp -d)
server=
cleanup() {
    [ -n "$server" ] && kill "$server" 2>/dev/null || true
    rm -rf "$work"
}
trap cleanup EXIT INT TERM

python3 -m venv "$work/venv"
"$work/venv/bin/pip" install --disable-pip-version-check --quiet "$wheel"

export LANSCAPE_DATA_DIR="$work/data"
port=${SMOKE_PORT:-18720}
atlas="$work/venv/bin/lanscape-atlas"

db=$("$atlas" migrate)
[ -f "$db" ] || { echo "smoke: migrate did not create $db" >&2; exit 1; }

# Run from an empty directory so nothing can import from the source tree.
cd "$work"
"$atlas" serve --host 127.0.0.1 --port "$port" >"$work/serve.log" 2>&1 &
server=$!

fetch() {
    # fetch URL OUTFILE: retry while the server starts.
    i=0
    while [ "$i" -lt 30 ]; do
        if curl --fail --silent --show-error --output "$2" "$1" 2>/dev/null; then
            return 0
        fi
        kill -0 "$server" 2>/dev/null || break
        i=$((i + 1))
        sleep 1
    done
    echo "smoke: GET $1 failed; server log follows" >&2
    cat "$work/serve.log" >&2
    return 1
}

fetch "http://127.0.0.1:$port/" "$work/index.html"
grep -qi "<html" "$work/index.html"
fetch "http://127.0.0.1:$port/static/app.css" "$work/app.css"
[ -s "$work/app.css" ]
echo "smoke: ok"
