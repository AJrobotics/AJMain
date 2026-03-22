#!/bin/bash
# AJ Robotics Agent - R1 (Raspberry Pi)
echo ""
echo "  ============================================"
echo "    AJ Robotics Agent - R1"
echo "    http://127.0.0.1:5000"
echo "  ============================================"
echo ""

cd "$(dirname "$0")"

# Use venv if available
if [ -f "./venv/bin/python" ]; then
    ./venv/bin/python -m agent.start_agent --machine R1
else
    python3 -m agent.start_agent --machine R1
fi
