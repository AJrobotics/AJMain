#!/bin/bash
# Run this once to set up passwordless SSH to RosMaster
# Usage: ssh-copy-id jetson@192.168.1.99
# Password: yahboom (default)

ROSMASTER_IP="192.168.1.99"
ROSMASTER_USER="jetson"

echo "Copying SSH key to RosMaster at $ROSMASTER_IP..."
echo "When prompted, enter password: yahboom"
ssh-copy-id -i ~/.ssh/id_ed25519.pub ${ROSMASTER_USER}@${ROSMASTER_IP}

echo ""
echo "Testing passwordless SSH..."
ssh -o BatchMode=yes ${ROSMASTER_USER}@${ROSMASTER_IP} "echo 'SSH key auth works! Hostname: $(hostname)'"
