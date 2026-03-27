#!/bin/bash
# PolyBot Startup Script
# Handles: stale Tor, port conflicts, tmux, caffeinate
# Usage: ./start.sh

set -e

PROJ_DIR="$HOME/gengar_bot/gengar_polybot"
SESSION_NAME="polybot"

echo "🚀 PolyBot Startup"
echo "══════════════════════════════════"

# 1. Kill stale Tor processes
echo "🧹 Cleaning up stale Tor processes..."
if pgrep -x tor > /dev/null 2>&1; then
    sudo pkill tor 2>/dev/null || pkill -9 tor 2>/dev/null || echo "   ⚠️  Couldn't kill tor (may need sudo)"
    sleep 2
    echo "   ✓ Tor processes killed"
else
    echo "   ✓ No stale Tor processes"
fi

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
echo "🖥  Starting tmux session '$SESSION_NAME'..."
tmux new-session -d -s "$SESSION_NAME" -c "$PROJ_DIR" \
    "caffeinate -i python bot.py; echo '⚠️  Bot exited. Press enter to close.'; read"

echo ""
echo "══════════════════════════════════"
echo "✅ PolyBot running in tmux"
echo ""
echo "   Attach:  tmux attach -t $SESSION_NAME"
echo "   Detach:  Ctrl+B then D"
echo "   Stop:    tmux attach, then Ctrl+C"
echo "   Status:  tmux ls"
echo "══════════════════════════════════"
