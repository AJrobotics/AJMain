# RosMaster X3 Robot

## Hardware
- **Board:** Jetson Orin NX (JetPack R36.4.3, aarch64, Ubuntu)
- **Wheels:** Mecanum (4WD omnidirectional, car_type=2)
- **LiDAR:** RPLidar S2 (Slamtec, 1Mbaud, CP210x, `/dev/rplidar`)
- **Depth Camera:** Orbbec Astra (OpenNI2, depth 640x480 + RGB, HFOV ~60°)
- **Driver Board:** STM32 v3.5 with CH340 USB-serial (`/dev/rosmaster`)
- **XBee:** FT231X USB-serial (`/dev/xbee`, 115200 baud)
- **GPS:** U-Blox 7 (`/dev/gps`, CDC-ACM)
- **OLED:** SSD1306 128x32 via I2C (bus 0, addr 0x50/0x57)
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
| `/ws/lidar` | ~5 Hz | LiDAR scan points + depth camera line overlay |
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
| `/api/depth_offset` | POST | Set depth camera angle offset |

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
    depth_reader.py          # Orbbec Astra depth + floor detection + depth line
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

## Collision Avoidance (HIGHEST PRIORITY)
**Collision avoidance has the highest priority in the system. No code path may bypass it.**
- 8 sectors (45° each), fuses LiDAR (360°) + depth camera (forward)
- STOP < 200mm, SLOW < 500mm, CAUTION < 800mm
- **HARD SAFETY**: if ANY non-ignored sector < 200mm, ALL translational motion is blocked regardless of direction. Only rotation allowed.
- Rotation always allowed (even during emergency stop)
- Rear ignore zone excludes LiDAR points behind robot
- All motor commands (explorer, calibration, TCP) must go through `filter_motion()`
- Never add code that calls `set_car_motion()` directly without collision filtering

## LiDAR Configuration
- **RPLidar S2** (Model 113, FW 1.2, HW 18)
- Best motor PWM: **600** (spread=273, most stable)
- Scan rate: ~4.5 Hz at PWM 600
- Points per revolution: 1155-1428 (avg ~1256)
- Revolution detection: angle wraps backward by >300°
- Depth camera overlay: horizontal line from middle row of depth frame (±30° FOV)
- Depth image is mirrored relative to LiDAR (angle negated in extraction)

## Depth Camera
- **Orbbec Astra** (original model with RGB + IR + depth)
- OpenNI2 path: `/home/jetson/yahboomcar_ros2_ws/software/library_ws/install/astra_camera/include/openni2/openni2_redist/arm64`
- Depth resolution: 640x480, range 400-5000mm
- Horizontal FOV: ~60° (±30° from center)
- RGB via OpenCV VideoCapture (video0), not OpenNI2
- Depth line overlay: rows 20%-30% from top (wall-level, avoids floor), sampled every 4th pixel, median averaged → ~130 points
- Depth image is mirrored relative to LiDAR (angle negated in extraction)
- Angle offset calibration available via `/api/depth_offset`

## SLAM
- Custom Python: occupancy grid (600x600, 50mm/cell) + ICP scan matching
- Pose tracking with IMU yaw fusion
- **Sensor fusion**: in ±30° forward overlap zone, LiDAR points confirmed by depth camera get higher confidence (L_OCC_FUSED=1.5), contradicted points get lower confidence (L_OCC_SINGLE=0.3)
- Frontier-based autonomous exploration
- Return-to-home via breadcrumb trail
- SLAM runs in background thread, not Tornado event loop
- **Map display thresholds**: solid walls (log-odds > 0.9), free space (< -1.5), hint walls (0.8-2.0)
- **Walls-only canvas**: shows only confirmed wall cells (log-odds > 0.9) as white on black

## Explorer
- Initial slow 360° scan at 0.2 rad/s before exploration (~31 sec)
- Navigation turn speed: 0.3 rad/s (slow for sensor fusion accuracy)
- Forward speed: 0.08 m/s (slow for safety)
- Frontier search: numpy vectorized (handles 10,000+ frontiers in <0.3s)
- Frontier clustering: grid-based binning O(n) instead of O(n²)
- Collision-aware: skips blocked targets, picks next frontier
- Logs frontier count, cluster count, navigation targets, and collision blocks

## Known Issues
- **STM32 USB intermittent**: sometimes only 1 CH340 appears instead of 2. The STM32 driver board's CH340 USB path can change between boots depending on expansion board power-up timing. Server `init_bot()` tests battery voltage > 1V to verify connection.
- **Serial port conflict**: Only ONE process can open `/dev/rosmaster` at a time. The OLED `status_display.py` must NOT open it — only `server.py` should.
- **Low battery causes LiDAR instability**: voltage drops → RPLidar motor speed fluctuates → inconsistent scan data. Charge battery when voltage drops below 9V.
- **Expansion board beeps on power-up**: Press Key1 on the board to stop. The beep is STM32 firmware, not software-controlled unless USB serial works.
- **Yahboom autostart app**: removed from GNOME autostart (`~/.config/autostart/start_app.sh.desktop` deleted)
- **USB path-based udev rules**: work when devices are on stable hub ports, but expansion board's internal hub can reassign ports between boots. If battery reads 0V, the STM32 may be on a different ttyUSB — restart the web UI service after verifying which port has the STM32.
- **Server SIGTERM handling**: Ignores SIGTERM during first 10 seconds after startup (stale signal from systemctl restart). Uses `loop.add_callback_from_signal()` after grace period. Service uses `Restart=always` with `-u` (unbuffered) Python.
- **Astra RGB not via OpenNI2**: Color stream fails via OpenNI2; use OpenCV `VideoCapture(0)` for the Astra RGB camera instead.
- **GStreamer warnings**: Astra RGB camera shows GStreamer pipeline errors on first open but works with V4L2 backend fallback.

## SMS Boot Notification
- Sends to 6616180571 (Verizon) via `dreamittogether@gmail.com`
- Gmail app password at `/home/jetson/.rosmaster_smtp`
