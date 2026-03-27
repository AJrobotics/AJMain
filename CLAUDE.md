# AJMain Project Notes

## Deployment

- **Trader code only** (trader/ib_smart_trader/ files to CashCow): `python -m deploy.deploy_cashcow --sync-only`
- **Full deploy** (app.py, gui/templates, all code): `python -m deploy.deploy --to CashCow --restart`
- **Restart Flask app** on CashCow: `ssh dongchul@192.168.1.91 "cd ~/AJMain && kill $(pgrep -f 'agent.start_agent') && nohup ./venv/bin/python -m agent.start_agent --machine CashCow > /tmp/ajmain.log 2>&1 &"`
- CashCow Flask app runs via `agent.start_agent`, NOT systemd ajmain.service

## Trading Strategies

Three independent traders run on CashCow (192.168.1.91) via IB Gateway:

| Trader | client_id | Flag | Description |
|--------|-----------|------|-------------|
| Smart Trader | 1 | (default) | Swing trading, daily screening |
| Day Trader | 3 | `--day` | Intraday scalping, 1-5min bars |
| Politician Trader | 4 | `--politician` | Congressional trade follower + political events |

## Machine Folders

Each machine has its own folder with machine-specific code and CLAUDE.md:

| Machine | Folder | Role |
|---------|--------|------|
| Dreamer | `Dreamer/` | Dev PC — agent (5000), XBee service (5001), YOLO training (5002), RosMaster dev (8080) |
| CashCow | `CashCow/` | Trading PC — IB Smart Trader 24/7 |
| Christy | `Christy/` | Main hub — dashboard, watchdog, Alexa, XBee coordinator |
| RosMaster | `RosMaster/` | Robot — Jetson code, NN training, web UI |

Shared infrastructure (`agent/`, `agent_modules/`, `shared/`, `configs/`) stays in root.
