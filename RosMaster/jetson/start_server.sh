#!/bin/bash
# Start the RosMaster TCP server in the background
cd /home/jetson/RosMaster
pkill -f 'python3.*tcp_server' 2>/dev/null
sleep 0.5
nohup python3 tcp_server.py > /tmp/rosmaster_server.log 2>&1 &
echo "Started PID: $!"
