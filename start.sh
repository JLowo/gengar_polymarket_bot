#!/bin/bash
# PolyBot Startup Script
# Handles: stale Tor, port conflicts, tmux, caffeinate
# Usage: ./start.sh   (works from any folder; paths come from this script's location)

set -e

# The repo is the folder this script lives in. proxy.py keeps Tor's state one
# level above the repo (<repo>/../.tor), so the Tor cache is cleared from there.
PROJ_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOR_DIR="$(dirname "$PROJ_DIR")/.tor"
SESSION_NAME="polybot"

echo "🚀 PolyBot Startup"
echo "══════════════════════════════════"

# 0. The bot runs with the repo's own Python environment (.venv)
if [ ! -x "$PROJ_DIR/.venv/bin/python" ]; then
    echo "❌ No Python environment at $PROJ_DIR/.venv"
    echo "   Create it from the repo folder with:"
    echo "     uv venv --python 3.12 .venv"
    echo "     uv pip install -r requirements.txt"
    exit 1
fi

# 1. Kill stale Tor processes and clear cached data
#    proxy.py starts Tor as you, so no sudo is needed. -x matches only processes
#    named exactly "tor" (a bare "pkill tor" also hits system daemons like storagekitd).
echo "🧹 Cleaning up stale Tor processes..."
if pgrep -x tor > /dev/null 2>&1; then
    pkill -x tor 2>/dev/null || echo "   ⚠️  Couldn't kill tor (it may belong to another user)"
    sleep 2
    echo "   ✓ Tor processes killed"
else
    echo "   ✓ No stale Tor processes"
fi
rm -rf "$TOR_DIR/data"
echo "   ✓ Tor data cache cleared"

# 2. Free port 9050 if occupied
if lsof -i :9050 > /dev/null 2>&1; then
    echo "🔌 Port 9050 in use, killing..."
    lsof -ti :9050 | xargs kill -9 2>/dev/null || true
    sleep 2
    echo "   ✓ Port 9050 freed"
else
    echo "   ✓ Port 9050 clear"
fi

# 3. Kill existing tmux session if running
if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    echo "🔄 Killing existing tmux session '$SESSION_NAME'..."
    tmux kill-session -t "$SESSION_NAME"
    echo "   ✓ Old session killed"
else
    echo "   ✓ No existing tmux session"
fi

# 4. Launch tmux with caffeinate + bot
#    tmux starts in $PROJ_DIR, so the relative .venv path avoids quoting trouble
#    when the folder name contains spaces.
#    `trap true INT` keeps the wrapper shell alive when you press Ctrl+C (the bot
#    still gets it and shuts down cleanly), so the window stays open with the
#    final summary instead of vanishing. Children get the default Ctrl+C handling.
echo "🖥  Starting tmux session '$SESSION_NAME'..."
tmux new-session -d -s "$SESSION_NAME" -c "$PROJ_DIR" \
    "trap true INT; caffeinate -i .venv/bin/python bot.py; echo '⚠️  Bot exited. Press enter to close.'; read"

echo ""
echo "══════════════════════════════════"
echo "✅ PolyBot running in tmux"
echo ""
echo "   Attach:  tmux attach -t $SESSION_NAME"
echo "   Detach:  Ctrl+B then D"
echo "   Stop:    tmux attach, then Ctrl+C"
echo "   Status:  tmux ls"
echo "══════════════════════════════════"
