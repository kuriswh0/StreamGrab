#!/bin/bash
# Double-click to launch StreamGrab (macOS). Close this window to quit.
cd "$(dirname "$0")"
exec python3 server.py "$@"
