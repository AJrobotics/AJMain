#!/bin/bash
# AJ Robotics Agent - Christy
echo ""
echo "  ============================================"
echo "    AJ Robotics Agent - Christy"
echo "    http://127.0.0.1:5000"
echo "  ============================================"
echo ""

cd "$(dirname "$0")"
python3 -m agent.start_agent --machine Christy
