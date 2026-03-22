#!/bin/bash
# Pull latest trained models from Dreamer to this machine
# Runs on Christy or R1

DREAMER="192.168.1.44"
DREAMER_USER="Dream"
REMOTE_DIR="/cygdrive/d/AJData/models"
LOCAL_DIR="$HOME/AJMain"

# Check if Dreamer is reachable
if ! ssh -o ConnectTimeout=5 -o BatchMode=yes "$DREAMER_USER@$DREAMER" "echo ok" > /dev/null 2>&1; then
    exit 0
fi

# Pull .pt files
rsync -az "$DREAMER_USER@$DREAMER:$REMOTE_DIR/*.pt" "$LOCAL_DIR/" 2>/dev/null
