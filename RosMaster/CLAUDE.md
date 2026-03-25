# RosMaster X3 Robot

## Hardware
- **Board:** Jetson Orin NX (JetPack R36.4.3, aarch64, Ubuntu)
- **Wheels:** Mecanum (4WD omnidirectional, car_type=2)
- **LiDAR:** RPLidar S2 (Slamtec, 1Mbaud, CP210x, `/dev/rplidar`)
- **Depth Camera:** Orbbec Astra (OpenNI2, depth 640x480 + RGB)
- **Driver Board:** STM32 with CH340 USB-serial (`/dev/rosmaster`)
- **XBee:** FT231X USB-serial (`/dev/xbee`, 115200 baud)
- **GPS:** U-Blox 7 (`/dev/gps`, CDC-ACM)
- **OLED:** SSD1306 128x32 via I2C
- **WiFi:** RTL8822CE, connected to NETGEAR15, static IP 192.168.1.99
- **SSH:** `jetson@192.168.1.99`, key auth, password `yahboom`
- **VNC:** port 5900, password `yahboom`, x11vnc with virtual display 1280x720

## Boot Sequence (important)
1. Turn on **Orin first** (wait ~30 sec for WiFi)
2. Turn on **expansion board** after Orin boots
3. Press **Key1** on expansion board to stop startup beep
- Hotspot auto-start is disabled (ROSMASTER WiFi profile autoconnect=no)
- NETGEAR15 has priority=10, static IP 192.168.1.99

## USB Device Mapping
Stable udev rules at `/etc/udev/rules.d/99-rosmaster.rules` based on physical USB hub path:

| Symlink | Device | Chip | USB Path |
|---------|--------|------|----------|
| `/dev/rplidar` | RPLidar S2 | CP210x (10c4:ea60) | `0:2.3` |
| `/dev/rosmaster` | STM32 driver board | CH340 (1a86:7523) | `0:2.1.1` |
| `/dev/xbee` | XBee module | FT231X (0403:6015) | `0:2.1.3` |
| `/dev/gps` | U-Blox GPS | CDC-ACM (1546:01a7) | `0:2.1.4` |
| `/dev/ch340aux` | Second CH340 | CH340 (1a86:7523) | `0:2.1.2` |

## Deployment
- **Deploy all code:** `python deploy.py`
- **Deploy + start TCP server:** `python deploy.py --start`
- **Deploy + start web UI:** `python deploy.py --webui`
- **Deploy OLED display:** `python deploy.py --oled`
- **Check status:** `python deploy.py --status`
- Files deploy from `jetson/` to `/home/jetson/RosMaster/` on Jetson

## Services on Jetson (systemd)
| Service | Description | Port |
|---------|-------------|------|
| `rosmaster-webui` | Web UI dashboard (Tornado) | 8080 |
| `rosmaster-status` | OLED status display | - |
| `rosmaster-notify` | Boot SMS notification | - |
| `x11vnc` | VNC server | 5900 |

## Web UI (http://192.168.1.99:8080)
Accessible from any device on the network.

**WebSocket endpoints:**
| Endpoint | Rate | Content |
|----------|------|---------|
| `/ws/lidar` | ~5 Hz | LiDAR scan points |
| `/ws/depth` | ~5 Hz | Depth heatmap + floor detection |
| `/ws/cam/primary` | ~5 Hz | Astra RGB camera |
| `/ws/collision` | ~5 Hz | 8-sector collision distances |
| `/ws/slam` | ~2 Hz | SLAM map + pose |
| `/ws/status` | ~1 Hz | Battery, IMU, IP |

**API endpoints:**
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/collision` | POST | Set ignore_angle, enable/disable |
| `/api/calibration` | POST/GET | Motor calibration tests |
| `/api/explorer` | POST/GET | Exploration control |

## Project Structure
```
config.py                    # IP, credentials, speed limits
deploy.py                    # Deploy script
tcp_client.py                # Windows TCP client for motor control
robot_control.py             # SSH-based remote control
remote.py                    # SSH helper utilities
jetson/
  robot_agent.py             # Rosmaster_Lib wrapper
  tcp_server.py              # TCP command server (port 5555)
  collision_avoidance.py     # 8-sector collision filter
  calibration.py             # Motor calibration runner
  slam_engine.py             # 2D SLAM (occupancy grid + ICP)
  explorer.py                # Autonomous frontier exploration
  status_display.py          # OLED status display
  boot_notify.py             # SMS notification on boot
  web_ui/
    server.py                # Tornado web server
    lidar_reader.py          # RPLidar S2 reader (pyrplidar)
    depth_reader.py          # Orbbec Astra depth + floor detection
    camera_reader.py         # RGB camera reader
    static/
      index.html             # Dashboard UI
      lidar.js               # All frontend JS
```

## Key Libraries on Jetson
- `pyrplidar` — RPLidar S2
- `primesense` — Orbbec Astra OpenNI2
- `digi-xbee` — XBee communication
- `Rosmaster_Lib` v3.3.9 — STM32 driver board
- `tornado` — Web server
- `numpy`, `scipy` — SLAM algorithms

## Motor Control
- TCP: port 5555, JSON commands (`move`, `stop`, `buzzer`, `led`, `battery`, `imu`, `servo`)
- Speed limits: vx/vy max 0.45 m/s, vz max 3.0 rad/s
- Collision avoidance intercepts all move commands
- Rear 120° ignore zone (configurable) for cables/devices

## Collision Avoidance
- 8 sectors (45° each), fuses LiDAR (360°) + depth camera (forward)
- STOP < 200mm, SLOW < 500mm, CAUTION < 800mm
- Rotation always allowed
- Rear ignore zone excludes LiDAR points behind robot

## SLAM
- Custom Python: occupancy grid (600x600, 50mm/cell) + ICP scan matching
- Pose tracking with IMU yaw fusion
- Frontier-based autonomous exploration
- Return-to-home via breadcrumb trail

## Known Issues
- STM32 USB connection is intermittent (sometimes only 1 CH340 appears)
- Low battery causes LiDAR instability
- Expansion board beeps on power-up until Key1 pressed
- Yahboom autostart app removed from GNOME autostart

## SMS Boot Notification
- Sends to 6616180571 (Verizon) via `dreamittogether@gmail.com`
- Gmail app password at `/home/jetson/.rosmaster_smtp`
