# Dreamer Machine Notes

## Overview
- **Host**: 192.168.1.44 (ethernet), 192.168.1.30 (WiFi when connected)
- **OS**: Windows 11
- **Role**: Development PC, XBee heartbeat responder, GPU training
- **GPU**: RTX 4070 (used for YOLO + RL training)

## Services

| Service | Port | Script | Description |
|---------|------|--------|-------------|
| Agent | 5000 | `agent.start_agent --machine Dreamer` | API agent with heartbeat module |
| XBee Responder | 5001 | `Dreamer/xbee_service.py` | XBee COM18, replies "R4!" |
| YOLO Training | 5002 | `Dreamer/training_service.py` | Dataset management + YOLO training |
| RosMaster Dev | 8080 | `RosMaster/serve_local.py` | NN training UI + simulation |

## Starting Services
```
Dreamer\Start Agent.bat          # Port 5000
Dreamer\Start XBee Responder.bat # Port 5001
Dreamer\Start YOLO Training Service.bat  # Port 5002
```

## XBee
- Port: COM18
- Baud: 115200
- Reply message: "R4!"
- Auto-start with agent

## Config References
- `configs/hosts.json` — Machine registry (IP, role)
- `agent/agent_config.json` — Agent modules (heartbeat), XBee settings
