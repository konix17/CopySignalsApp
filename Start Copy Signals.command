#!/bin/zsh
# Double-click this file in Finder to start Copy Signals. Keep this window open while you use the app;
# close it (or press Ctrl+C) to stop the app.
cd "$(dirname "$0")"
(sleep 4 && open "http://localhost:8000") &
exec .venv/bin/uvicorn app.main:app --app-dir backend --port 8000
