#!/bin/sh
# Thin wrapper kept for older Watcher prompts: the real loop is watch.py (cross-platform).
exec python3 "$(cd "$(dirname "$0")" && pwd)/watch.py" "$@"
