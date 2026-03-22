#!/bin/bash
# AJ Robotics Agent - CashCow
echo ""
echo "  ============================================"
echo "    AJ Robotics Agent - CashCow"
echo "    http://127.0.0.1:5000"
echo "  ============================================"
echo ""

cd "$(dirname "$0")"
python3 -m agent.start_agent --machine CashCow
