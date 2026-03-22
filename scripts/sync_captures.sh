#!/bin/bash
# Sync captures from this machine to Dreamer's D:\AJData\captures
# Runs on Christy or R1 via cron
# Only syncs if Dreamer is reachable

DREAMER="192.168.1.44"
DREAMER_USER="Dream"
MACHINE=$(hostname)

# Map hostname to folder name
case "$MACHINE" in
    AJRobotics) FOLDER="Christy" ;;
    RPMain)     FOLDER="R1" ;;
    *)          FOLDER="$MACHINE" ;;
esac

LOCAL_CAPTURE_DIR="$HOME/captures"
REMOTE_DIR="/cygdrive/d/AJData/captures/$FOLDER"

# Create local capture dir if it doesn't exist
mkdir -p "$LOCAL_CAPTURE_DIR"

# Check if Dreamer is reachable
if ! ssh -o ConnectTimeout=5 -o BatchMode=yes "$DREAMER_USER@$DREAMER" "echo ok" > /dev/null 2>&1; then
    exit 0  # Dreamer offline, skip silently
fi

# Sync files to Dreamer
rsync -az --remove-source-files "$LOCAL_CAPTURE_DIR/" "$DREAMER_USER@$DREAMER:$REMOTE_DIR/" 2>/dev/null

# Clean up empty directories left behind
find "$LOCAL_CAPTURE_DIR" -type d -empty -delete 2>/dev/null
