#!/bin/bash
# Quick git add, commit, and push
cd "$(dirname "$0")/.."
git add -A
git commit -m "Auto sync from $(hostname)" 2>/dev/null
git push origin main
