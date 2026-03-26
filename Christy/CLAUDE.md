# Christy — Main Hub

## Machine
- **OS:** Ubuntu, hostname `AJRobotics`, user `ajrobotics`
- **IP:** 192.168.1.94
- **Role:** Main Hub + Watchdog + XBee Coordinator + Data Hub
- **AJMain path:** `/home/ajrobotics/AJMain`
- **Venv:** `/home/ajrobotics/AJMain/venv`
- **Dashboard:** http://192.168.1.94:5000 (Flask, `app.py`)

## Services
| Service | Manager | Description |
|---------|---------|-------------|
| Flask dashboard | nohup | `python app.py` on port 5000 |
| XBee monitor | systemd `xbee-monitor.service` | Heartbeat broadcast, `/dev/ttyUSB0`, 10s interval |
| go2rtc | systemd | Video surveillance proxy, port 1984 |
| eufy event monitor | node | Push events from eufy cameras, port 63340 |

## XBee Monitor
- **Must use systemd** — do NOT start manually with nohup (causes port lock conflicts)
- Restart: `sudo systemctl restart xbee-monitor.service`
- Logs: `/home/ajrobotics/logs/xbee/`
- Responding devices: R1 (Raspberry Pi), ROSMASTER R3 (Jetson)

## Deployment
- **Full deploy:** `python -m deploy.deploy --to Christy --restart` (from Dreamer)
- **Robotics only:** `python -m deploy.deploy_christy --sync-only`
- **Restart Flask:** SSH and kill/restart `python app.py`
- Template changes don't require Flask restart (Jinja reloads automatically)
- `app.py` and `Christy/` module changes require Flask restart

## Dashboard Panels (machine_detail.html for Christy)
- **XBee Monitor Status** — process status, live event log, responding devices
- **CashCow Trading** — proxied via `/api/cashcow/trader-status` (avoids CORS)
- **Video Surveillance** — 8 RTSP cameras via go2rtc WebRTC + eufy event monitor
- **Recent Eufy Events** — table showing motion/person/push from all eufy cameras
- **Alexa Devices** — 6 Echo devices, online/offline status (TTS pending Home Assistant)
- **Remote Reboot** — CashCow, R1, Christy

## Alexa Integration (partial)
- Device list discovered from Amazon account, stored in `Christy/alexa_service.py`
- AlexaPy installed but auth doesn't work (Amazon blocks programmatic login)
- **Next step:** Set up Home Assistant in Docker for TTS/announcements
- Customer ID: `A10I92LG4OTH43`

## Eufy Cameras
- Event monitor runs on port 63340 (Node.js, `eufy-security-ws`)
- 10 devices registered, events from: Office, Dogfood, Hallway, Backyard, G1, G2
- Dashboard polls `/api/cameras/eufy/events` every 10 seconds
- Event-only camera (Backyard Solo S3) falls back to showing any device's latest event

## Known Issues
- `alexa-remote-control` cloned to `/home/ajrobotics/alexa-remote-control/` but not configured yet
