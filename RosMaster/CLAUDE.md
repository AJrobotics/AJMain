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
| `/dev/gps` | U-Blox GPS | CDC-ACM (1546:01a7) | `0:2.1.2` |
| `/dev/ch340aux` | Second CH340 | CH340 (1a86:7523) | `0:2.1.4` |

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

## Multiprocessing Architecture
Three OS processes eliminate Python GIL contention between sensors:

```
┌─────────────────────────────────┐
│  Main Process (Tornado)         │
│  ├─ Web server (port 8080)      │
│  ├─ Collision avoidance         │◄── reads shared memory (38us)
│  ├─ SLAM engine (bg thread)     │
│  ├─ Explorer (bg thread)        │
│  ├─ Camera RGB (thread)         │
│  └─ STM32 motor control         │
└──────────┬──────────┬───────────┘
           │          │
    shared memory    shared memory
    lidar_shm[730]   depth_shm[326]
           │          │
┌──────────┴───┐ ┌────┴──────────┐
│ LiDAR Process│ │ Depth Process │
│ pyrplidar    │ │ OpenNI2       │
│ 10Hz, 100ms  │ │ 10 FPS        │
│ ±0.7ms jitter│ │               │
└──────────────┘ └───────────────┘
```

**Shared Memory Layout:**
- `lidar_shm` (`mp.Array('f', 730)`): [0]=count, [1]=timestamp, [2:722]=360 scan points (angle,dist), [722:730]=8 sector distances
- `depth_shm` (`mp.Array('f', 326)`): [0]=count, [1]=timestamp, [2:322]=160 depth line points (angle,dist), [322:325]=3 front sector distances (right-front,front,left-front), [325]=connected flag

**Queues (variable-size data):**
- `scan_queue` — LiDAR scan dicts for web UI broadcast
- `counts_queue` — raw point counts per revolution for oscilloscope
- `debug_queue` — revolution summaries for sensor debug tool
- `frame_queue` — depth heatmap JPEG for web UI broadcast

**Why multiprocessing:** Python GIL only allows one thread to run at a time. With threading, LiDAR dt_ms varied 80-750ms. With separate processes, dt_ms is stable at 100ms ±0.7ms regardless of CPU load from other sensors/SLAM/Tornado.

**Collision reads shared memory, not raw data:** LiDAR process pre-computes 8 sector min distances. Depth process pre-computes 3 front sector distances. Collision just reads floats and fuses — takes ~38 microseconds.

## Network Access
- **WiFi:** `192.168.1.99` (NETGEAR15, static IP) — primary access during robot operation
- **Direct Ethernet:** `10.0.0.1` ↔ `10.0.0.2` (Dreamer) — for development/debugging, <1ms latency
- Both interfaces active simultaneously; Ethernet optional

## Web UI (http://192.168.1.99:8080)
Accessible from any device on the network.

**WebSocket endpoints:**
| Endpoint | Rate | Content |
|----------|------|---------|
| `/ws/lidar` | ~1 Hz | LiDAR scan points + depth camera line overlay |
| `/ws/depth` | ~1 Hz | Depth heatmap JPEG |
| `/ws/cam/primary` | ~1 Hz | Astra RGB camera |
| `/ws/collision` | ~1 Hz | 8-sector collision distances |
| `/ws/slam` | ~0.5 Hz | SLAM map + pose |
| `/ws/status` | ~1 Hz | Battery, IMU, IP, scan counts |
| `/ws/debug` | ~10 Hz | Raw revolution summaries (enables LiDAR debug buffering — extra overhead) |
| `/ws/timing` | ~2 Hz | Timing metrics only (no overhead on sensor processes) |

**API endpoints:**
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/collision` | POST | Set ignore_angle, enable/disable |
| `/api/calibration` | POST/GET | Motor calibration tests |
| `/api/explorer` | POST/GET | Exploration control |
| `/api/depth_offset` | POST | Set depth camera angle offset |
| `/api/slam_method` | POST/GET | Switch mapping method |
| `/api/lidar_mode` | POST/GET | Switch scan mode (standard/express) |

**Timing bar** (on main dashboard + sensor debug):
- LiDAR age: ms since LiDAR process wrote to shared memory (should be <100ms)
- Depth age: ms since depth process wrote to shared memory (should be <150ms)
- Collision: microseconds to read shared memory and fuse sectors (should be <100us)
- Level: STOP/SLOW/CAUTION/CLEAR
- Sectors: 8 distances in mm

## Sensor Debug Tool (http://192.168.1.99:8080/static/sensor_debug.html)
Separate debug page for raw sensor analysis. Only adds overhead when open (enables raw debug buffering via `/ws/debug`).
- **LiDAR tab**: Revolution timeline (3 traces), angle coverage polar chart, LiDAR scan plot, revolution log table
- **Filter controls**: dt> and Valid> inputs to exclude bad revolutions from plots
- **Clickable rows**: pause + click a row to inspect that specific revolution
- **Future tabs**: Depth, IMU, XBee, GPS (placeholders)

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
  collision_avoidance.py     # 8-sector collision filter (reads shared memory)
  calibration.py             # Motor calibration runner
  slam_engine.py             # 2D SLAM (occupancy grid + ICP)
  explorer.py                # Autonomous frontier exploration
  status_display.py          # OLED status display
  boot_notify.py             # SMS notification on boot
  web_ui/
    server.py                # Tornado web server (main process)
    lidar_reader.py          # RPLidar S2 reader (separate process, shared memory)
    depth_reader.py          # Orbbec Astra depth (separate process, shared memory)
    camera_reader.py         # RGB camera reader (thread in main process)
    static/
      index.html             # Dashboard UI
      lidar.js               # All frontend JS
      sensor_debug.html      # Sensor debug tool (LiDAR + IMU tabs)
      mapping_debug.html     # Mapping debug tool (Live/Offline, recording playback)
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
- STOP < 300mm, SLOW < 500mm, CAUTION < 800mm
- **HARD SAFETY**: if ANY non-ignored sector < 300mm, ALL translational motion is blocked regardless of direction. Only rotation allowed.
- Rotation always allowed (even during emergency stop)
- Rear 140° ignore zone excludes LiDAR points behind robot (applied in LiDAR process)
- **Reads pre-computed sector distances from shared memory** — does NOT process raw sensor data
  - LiDAR process writes 8 sector distances to `lidar_shm[722:730]`
  - Depth process writes 3 front sector distances to `depth_shm[322:325]`
  - Collision fuses: `sector[i] = min(lidar_sector[i], depth_sector[i])` for front 3 sectors
  - Total collision update time: ~38 microseconds
- All motor commands (explorer, calibration, TCP) must go through `filter_motion()`
- Never add code that calls `set_car_motion()` directly without collision filtering

## LiDAR Configuration
- **RPLidar S2** (Model 113, FW 1.2, HW 18)
- Best motor PWM: **800** (stable at full battery 12.3V)
- Scan rate: ~10 Hz at PWM 800
- Points per revolution: ~1600 total, ~1240 valid (dist>0, quality>0)
- **Revolution detection: hardware `start_flag`** (encoder sync pulse, fires once per revolution at ~357°). Do NOT use angle-wrap detection — it's unreliable under CPU load.
- **Runs in a separate process** (`multiprocessing.Process`) to avoid GIL contention. Without this, Python GIL from other threads (depth camera, Tornado) causes dt_ms to vary 80-750ms. With separate process, dt_ms is stable at 100ms ±0.7ms.
- Data transfer: shared memory (`lidar_shm`) for scan + sectors, queues for variable-size data (counts, debug summaries)
- Pre-computes 8 sector min distances in LiDAR process (collision reads floats, not raw scan)
- Full reset sequence on startup: stop → motor off → disconnect → wait → reconnect (prevents sync byte errors)
- Depth camera overlay: horizontal line from middle row of depth frame (±30° FOV)
- Depth image is mirrored relative to LiDAR (angle negated in extraction)

## Depth Camera
- **Orbbec Astra** (original model with RGB + IR + depth)
- **Runs in a separate process** (`multiprocessing.Process`) — same pattern as LiDAR
- OpenNI2 path: `/home/jetson/yahboomcar_ros2_ws/software/library_ws/install/astra_camera/include/openni2/openni2_redist/arm64`
- Depth resolution: 640x480, range 400-5000mm
- Horizontal FOV: ~60° (±30° from center)
- RGB via OpenCV VideoCapture (video0), not OpenNI2 (stays in main process)
- Depth line: rows 20%-30% from top (wall-level), sampled every 4th pixel, median averaged → ~160 points → written to `depth_shm`
- Pre-computes 3 front sector distances (10th percentile, wall-level band rows 15-35%) → written to `depth_shm`
- Heatmap JPEG sent via queue (variable size, for web UI broadcast)
- `angle_offset` as `mp.Value('f')` — main process writes via API, depth process reads
- Depth image is mirrored relative to LiDAR (angle negated in extraction)

## SLAM
- Custom Python: occupancy grid (600x600, 50mm/cell) + ICP scan matching
- **Heading: 90% IMU + 10% ICP** (IMU is primary, ICP fine-tunes when quality ≥ 0.4)
  - IMU yaw is **negated** before use (`imu_corrected = -imu_yaw`) — IMU convention opposite to math convention
  - IMU offset calibrated on first scan to align with SLAM coordinate system
  - When ICP quality < 0.4: 100% IMU heading (no ICP rotation)
  - Previous attempts: 70/30 ICP/IMU blend caused ±30° oscillation; 100% IMU was stable but no fine-tuning
- **Translation: ICP only, capped at 50mm/scan** (allows 0.15m/s at 5Hz = 30mm + margin)
  - Previous 100mm cap caused ~200mm drift per rotation; 50mm cap is a balance between tracking real movement and preventing drift
  - 20mm cap was too tight — rejected most translations, robot couldn't track forward movement
  - Remaining drift (~80mm/rotation) is from LiDAR not at center of rotation (robot wobbles during turns)
- **ICP quality metric**: fraction of points within 100mm of target. Reject translation if quality < 0.2
- **Sensor fusion**: in ±30° forward overlap zone, LiDAR+depth cross-validation (L_OCC_FUSED=1.5, L_OCC_SINGLE=0.3)
- **Map persistence**: save/load to `/home/jetson/RosMaster/maps/` as compressed numpy `.npz`
- **Wall line extraction**: Hough Transform → angle snapping (0°/90°) → parallel line collapse → collinear merge
- **Simple loop closure**: detects revisited poses (300mm, 200 scan gap, quality > 0.4, cooldown 100 scans)
  - Corrects **position only** — does NOT change heading (IMU handles heading)
  - Previous bug: loop closure applied ICP rotation to pose, causing -123° heading jumps that contradicted IMU
- SLAM runs in background thread at 5 Hz, not Tornado event loop
- **Map display thresholds**: solid walls (log-odds > 2.0), free space (< -1.5), hint walls (0.8-2.0)
- **Future improvement**: landmark-based localization — detect wall features (corners, edges) as anchoring points to correct robot position, eliminating ICP drift

## Explorer
- Initial slow 360° scan at 0.2 rad/s before exploration (~31 sec)
- **Scan test**: 2x CW + 2x CCW full rotations from fixed position (for mapping debug)
  - Uses IMU-tracked cumulative rotation (not timer) — stops at exactly 360°
  - Button on dashboard: "Scan"
- Navigation turn speed: 0.3 rad/s (slow for sensor fusion accuracy)
- Forward speed: 0.08 m/s (adjustable via dashboard slider, range 0.02-0.15 m/s)
- Stuck spinning timeout: 60 steps (~12 sec), faster rotation (1.5x) for turns > 90°
- Frontier search: numpy vectorized (handles 10,000+ frontiers in <0.3s)
- Frontier clustering: grid-based binning O(n) instead of O(n²)
- Collision-aware: skips blocked targets, picks next frontier
- **File logging** to `/tmp/explorer.log` with detailed turn/forward/collision data
- **Video recording**: starts automatically with exploration/scan test
  - H.264 MP4 at 1 FPS, 320x120 (RGB + depth heatmap side-by-side)
  - JSON log with LiDAR scan (360°), pose, sectors per frame
  - Saved to `/home/jetson/RosMaster/maps/recordings/`
  - Auto-stops when exploration finishes

## Mapping Debug (http://192.168.1.99:8080/static/mapping_debug.html)
- **Live/Offline mode** dropdown
- **Live**: real-time occupancy grid + trajectory + wall lines + robot position
- **Offline**: load recording, step through frames, see LiDAR scan overlay on map
  - Yellow dots: LiDAR scan at selected moment (world coordinates)
  - Cyan trajectory: robot path up to that point
  - Orange FOV cone: depth camera field of view
  - Video player synced with pose log table
  - Mini LiDAR polar plot in side panel
- **Interactive map**: zoom (scroll), pan (drag), click cell to inspect log-odds
- **Save/Load/Reset** map buttons
- **Wall lines** view: extracted straight wall segments
- **Recording list** with playback

## NN Navigation Training
Training uses PPO (Proximal Policy Optimization) on Dreamer (RTX 4070) with Stable Baselines3.

**Observation:** 36 LiDAR bins (10° each, normalized [0,1])
**Action:** 3 continuous values → (vx, vy, vz) motor commands
**Goal:** Maximize room exploration coverage while avoiding collisions

**Realism parameters (sim-to-real transfer):**
| Parameter | Value | Reason |
|-----------|-------|--------|
| Robot radius | 150mm | Physical body size — LiDAR measures from center |
| LiDAR noise | ±30mm Gaussian | Real RPLidar S2 measurement noise |
| LiDAR dropout | 2% of rays | Random missing points (occlusion, surface absorption) |
| Velocity noise | ±10% forward, ±15% lateral | Mecanum wheel slip (lateral is worse) |
| Heading drift | ±0.3°/step | IMU noise accumulation |
| Rear ignore zone | 140° | Cables/devices block rear LiDAR (matches real robot) |
| STOP distance | 100mm | Hard safety: block all translation |
| SLOW distance | 200mm | Reduce speed to 30% |
| CAUTION distance | 300mm | Reduce speed to 70% |
| Max vx | -0.05 to 0.15 m/s | Forward/backward speed limits |
| Max vy | ±0.12 m/s | Lateral strafe speed (mecanum advantage) |
| Max vz | ±1.0 rad/s | Rotation speed |
| Step interval | 0.2s (5 Hz) | Matches real explorer update rate |

**Reward function:**
- +1.0 per newly explored cell
- -50.0 per collision (blocked by wall or collision filter)
- -0.1 per step (time penalty for efficiency)
- +5.0 bonus if coverage > 80% at episode end

**Episode termination:** 95% coverage (success), 200 collisions (stuck), or 1000 steps (timeout)

**Training pipeline (all from mapping_debug.html Simulation mode):**
1. Generate Maps — create diverse training rooms (map_generator.py)
2. Train NN — PPO training (train.py), 500K+ steps recommended
3. Evaluate — compare NN vs rule-based (evaluate.py)
4. Load in Sim — test ONNX model in browser simulation
5. Deploy to Jetson — scp nav_policy.pt to /home/jetson/RosMaster/models/

**Key files:**
- `training/robot_env.py` — Gymnasium environment with realistic physics
- `training/train.py` — PPO training script with ONNX/TorchScript export
- `training/evaluate.py` — NN vs rule-based comparison
- `training/map_generator.py` — Diverse room generator
- `jetson/nn_navigator.py` — Inference on Jetson (loads TorchScript model)
- `serve_local.py` — Local HTTP server for training UI on Dreamer (port 8080)

**IMU yaw vs heading:** With mecanum wheels, IMU yaw (body orientation) differs from movement direction during strafe. LiDAR is mounted on the body, so IMU yaw is the correct angle for scan-to-world transformation. SLAM mapping is unaffected by this distinction.

## XBee Communication
- **Module:** XBee PRO 900HP 10K (R3, MAC: 0013A20041BB8E1F)
- **Library:** `digi-xbee` (installed system-wide via `sudo pip3 install digi-xbee`)
- **Runs as thread** in server.py (not separate process) — low bandwidth, no GIL concern
- **Auto-reconnect:** retries every 5s if `/dev/xbee` disappears
- **All responses prefixed with `R3:`**
- **Dashboard panel:** XBee/GPS panel with ON/OFF toggle, RX/TX counts, broadcast input

**XBee Network:**
| Node ID | MAC | Location |
|---------|-----|----------|
| R3 | 0013A20041BB8E1F | RosMaster |
| Robot1 | 0013A20041BB8D5E | Christy |
| DeskTop | 0013A20041741E51 | Desktop |

**XBee Command Protocol:**
| Command | Response | Type |
|---------|----------|------|
| `All Good?` / `status` | `R3: Fix=1 Sats=5 34.505N 118.328W 827m Bat=11.2V` | Query |
| `gps` | `R3: No fix, 0 sats` | Query |
| `battery` | `R3: Bat=11.2V` | Query |
| `ping` | `R3: pong` | Query |
| `move 0.1 0 0.5` | `R3: Move(0.10,0.00,0.50)` | Control |
| `stop` / `!stop` | `R3: Stopped` | Control |
| `explore start/stop/status` | `R3: Explore: ...` | Control |

**Key files:** `jetson/xbee_comm.py`, `jetson/gps_reader.py`

## GPS
- **Module:** U-Blox 7 (UBX-G70xx, firmware 1.00, PROTVER 14.00)
- **Port:** `/dev/gps` (ttyACM0, CDC-ACM), 9600 baud
- **NMEA parsing:** Manual (GGA + RMC), no pynmea2 dependency
- **Runs as thread** in server.py, retries every 5s if unplugged
- **Antenna must face UP** (ceramic patch toward sky)
- **Cold start:** 2-5 minutes for first fix outdoors
- **No fix indoors** — GPS data always available but fix=false, never blocks

## NN Navigation Training
Training uses PPO (Proximal Policy Optimization) on Dreamer (RTX 4070) with Stable Baselines3.

**Multiple task-specific models:**
| Task | Obs Size | Extra Inputs | Reward Focus |
|------|----------|-------------|--------------|
| `explore` | 36 | LiDAR only | New cells, collision penalty |
| `floor_plan` | 39 | + frontier angle/dist, coverage | Frontier approach, coverage milestones |
| `wall_follow` | 39 | + side dist, target dist, front dist | Distance error, forward progress |

**Observation:** 36 LiDAR bins (10° each, normalized [0,1]) + task-specific extras
**Action:** 3 continuous values → (vx, vy, vz) motor commands
**Models saved per task:** `training/models/<task>/<task>_policy.onnx` + `.pt`

**Realism parameters (sim-to-real transfer):**
| Parameter | Value | Reason |
|-----------|-------|--------|
| Robot radius | 150mm | Physical body size |
| LiDAR noise | ±30mm Gaussian | RPLidar S2 measurement noise |
| LiDAR dropout | 2% of rays | Random missing points |
| Velocity noise | ±10% forward, ±15% lateral | Mecanum wheel slip |
| Heading drift | ±0.3°/step | IMU noise accumulation |
| Rear ignore zone | 140° | Cables/devices block rear LiDAR |
| STOP distance | 100mm | Hard safety |
| SLOW distance | 200mm | Reduce speed to 30% |
| CAUTION distance | 300mm | Reduce speed to 70% |

**Training pipeline (from mapping_debug.html Simulation mode):**
1. Select Task (Explore / Floor Plan / Wall Follow)
2. Generate Maps — create diverse training rooms
3. Train NN — PPO training, 500K+ steps recommended
4. Evaluate — compare NN vs rule-based
5. Load in Sim — test ONNX model in browser simulation
6. Deploy to Jetson — scp model to /home/jetson/RosMaster/models/

**Local development server:** `python serve_local.py` on Dreamer (port 8080), proxies API calls to Jetson

**Key files:**
- `training/robot_env.py` — Gymnasium environment with task-specific obs/reward
- `training/train.py` — PPO training script with `--task` parameter
- `training/evaluate.py` — NN vs rule-based comparison
- `training/map_generator.py` — Diverse room generator
- `serve_local.py` — Local HTTP server for training UI on Dreamer
- `jetson/nn_navigator.py` — Inference on Jetson (loads TorchScript model)

## Self-Driving (Route Record + Replay)

**Phase 1: Record Route** — physically push robot while sensors capture data.
- Dashboard panel: route name input, Record/Stop buttons, live stats
- Records at 5Hz: RGB keyframes (640x480), LiDAR 360° scans, GPS, IMU, collision sectors
- Routes saved to `/home/jetson/RosMaster/routes/<name>/`
- Route format: `waypoints.json` + `frames/` (keyframe JPEGs) + `features.npz` (ORB) + `segments/` (SLAM grids)
- Segment transitions every 15m (grid shift, preserves IMU heading)

**Phase 2: Behavior Cloning** — train NN from recorded routes.
- **Input:** RGB frame (80x60 grayscale) + 36 LiDAR bins
- **Output:** (vx, vy, vz) motor commands
- **Training labels:** optical flow (RGB) → translation, IMU yaw delta → rotation
- **Architecture:** CNN (3 conv layers) + LiDAR MLP (36→64) → combined head (192→128→64→3)
- Supports multiple routes: `--route P2,P3` or `--route ALL`
- Model: `training/models/route_nav/route_nav_policy.onnx` + `.pt`

**Phase 3: Route Replay** (implemented, needs testing)
- Waypoint following with multi-modal localization (SLAM + GPS + visual matching)
- Visual place recognition: ORB features + FLANN matcher
- GPS-SLAM fusion: weighted by environment (indoor vs outdoor)
- Dashboard: route selector dropdown + Follow button + progress bar

**Key files:**
- `jetson/route_recorder.py` — records sensor data during manual push
- `jetson/route_player.py` — autonomous waypoint following
- `jetson/visual_matcher.py` — ORB feature matching
- `jetson/gps_slam_fusion.py` — GPS + SLAM position fusion
- `training/behavior_cloning.py` — imitation learning from recordings

## Session Notes (March 26, 2026)

### What was accomplished
1. **XBee communication**: integrated into server.py as thread, auto-reconnect, handler registry, ON/OFF toggle on dashboard
2. **GPS integration**: u-blox 7 reader thread, NMEA parsing, status on dashboard, udev rules updated (GPS now at 0:2.1.2)
3. **NN training pipeline**: multi-task models (explore, floor_plan, complete_map, wall_follow), parallel training, per-task progress bars, reward info display
4. **Simulation mode**: built map from recordings, test NN in browser, per-task ONNX model loading
5. **Route recording**: dashboard panel, 5Hz sensor capture, keyframe extraction + ORB features
6. **Behavior cloning**: train CNN+MLP from recorded routes using optical flow + IMU, trained on P2+P3 (272 samples, loss 0.0003)
7. **Route replay**: waypoint follower with visual matching + GPS-SLAM fusion (implemented, not yet tested on robot)

### Remaining work
- Test behavior cloning model on real robot
- Record more routes (different rooms, outdoors with GPS)
- Add "Learn from Recording" button to training pipeline UI
- Test route replay with visual matching
- Deploy route_nav model to Jetson

## Known Issues
- **STM32 USB intermittent**: sometimes only 1 CH340 appears instead of 2. The STM32 driver board's CH340 USB path can change between boots depending on expansion board power-up timing. Server `init_bot()` tests battery voltage > 1V to verify connection.
- **Serial port conflict**: Only ONE process can open `/dev/rosmaster` at a time. The OLED `status_display.py` must NOT open it — only `server.py` should.
- **Low battery causes LiDAR instability**: voltage drops → RPLidar motor speed fluctuates → inconsistent scan data. Charge battery when voltage drops below 9V.
- **Expansion board beeps on power-up**: Press Key1 on the board to stop. The beep is STM32 firmware, not software-controlled unless USB serial works.
- **Yahboom autostart app**: removed from GNOME autostart (`~/.config/autostart/start_app.sh.desktop` deleted)
- **USB path-based udev rules**: work when devices are on stable hub ports, but expansion board's internal hub can reassign ports between boots. If battery reads 0V, the STM32 may be on a different ttyUSB — restart the web UI service after verifying which port has the STM32.
- **LiDAR sync byte errors on restart**: When `systemctl restart` kills the LiDAR process mid-scan, stale serial data causes `sync bytes are mismatched` on next connect. Fixed by full reset sequence in `_lidar_process()`: stop → motor off → disconnect → wait → reconnect. Service `Restart=always` retries if reset fails.
- **Server SIGTERM handling**: Ignores SIGTERM during first 10 seconds after startup (stale signal from systemctl restart). Uses `loop.add_callback_from_signal()` after grace period. Service uses `Restart=always` with `-u` (unbuffered) Python.
- **Astra RGB not via OpenNI2**: Color stream fails via OpenNI2; use OpenCV `VideoCapture(0)` for the Astra RGB camera instead.
- **GStreamer warnings**: Astra RGB camera shows GStreamer pipeline errors on first open but works with V4L2 backend fallback.
- **Video timeupdate overrides frame selection**: In mapping_debug.html offline mode, the HTML5 video `timeupdate` event fires even when paused (with currentTime=0), resetting `selectedRecEntry` back to frame 0. Fix: guard with `if (video.paused) return;` so video only overrides frame selection during playback.

## SMS Boot Notification
- Sends to 6616180571 (Verizon) via `dreamittogether@gmail.com`
- Gmail app password at `/home/jetson/.rosmaster_smtp`

## Session Notes (March 25, 2026)

### What was accomplished
1. **LiDAR revolution detection**: switched from angle-wrap to hardware `start_flag` — reliable, no CPU dependency
2. **Multiprocessing architecture**: LiDAR + depth camera in separate processes, shared memory for collision (~38μs reads)
3. **IMU heading fix**: `atan2(sin,cos)` for all angle math — eliminates ±180° wrapping bug that caused heading jumps
4. **Heading is 100% IMU**: ICP rotation disabled (proved unreliable). ICP translation re-enabled with 50mm cap for position tracking
5. **Sensor debug tool**: LiDAR tab (revolution timeline, angle coverage, scan plot) + IMU tab (heading sweep, compass)
6. **Mapping debug tool**: Live/Offline mode, recording playback with LiDAR overlay, landmark display, cell inspector
7. **Exploration recording**: H.264 video (RGB+depth) + JSON log with LiDAR scan, IMU yaw, landmarks per frame
8. **Wall-follow with mecanum strafe**: proportional distance control, initial wall approach, corner handling
9. **Configurable parameters from dashboard**: collision thresholds, ICP cap/quality, wall distance, speed
10. **Jump detection**: detects IMU glitches >28°/frame, pauses grid updates for 10 scans, shows on timing bar
11. **Scan test mode**: 2×CW + 2×CCW full rotations using IMU-tracked cumulative angle (not timer)

### Remaining issues to fix
- **ICP translation drift**: ~1mm/s even when stationary. Accumulates ~600mm over 10 minutes. Root cause: ICP finds small false translations in consecutive scans. Landmark-based correction was attempted but landmarks drift with the pose
- **Landmark-based localization**: algorithm detects corners and flat walls, but landmarks are computed from the drifting pose so they move with it. Need to anchor landmarks to first detection and correct pose against them, not the other way around. **Future approach**: detect wall features (corners, edges) as fixed anchoring points, re-localize against them each scan
- **Wall-follow hallway navigation**: robot maintains wall distance via sector measurements but at high speed (0.15 m/s) the strafe correction can't keep up in narrow hallways. Proportional control helps but needs further tuning
- **Floor plan output**: occupancy grid exists but wall lines are noisy. Need: morphological cleanup → Hough/RANSAC wall extraction → angle snapping → parallel line collapse → room segmentation (ROSE2) → vectorize to SVG/DXF
- **SLAM Toolbox integration**: ROS2 SLAM Toolbox is the recommended production solution (loop closure, correlative scan matching, lifelong mapping). Keep custom SLAM for real-time control, use SLAM Toolbox for final map output

### Key learnings
- **IMU is the only reliable heading source** on this hardware. ICP heading oscillates ±30° even with quality=0.99. The Rosmaster IMU (gyro+accel) provides stable heading but has occasional glitches at the ±180° boundary
- **`(x + π) % (2π) - π` has edge cases** — use `atan2(sin(x), cos(x))` for angle normalization
- **Python GIL kills sensor timing**: LiDAR dt varied 80-750ms with threading, stable at 100ms ±0.7ms with multiprocessing
- **Shared memory is fast**: collision reads 8 floats in ~38μs vs ~5ms via queues
- **Don't reset SLAM during exploration**: any code path that calls `slam.reset()` (including UI buttons, mapping method changes) destroys heading calibration
- **Mecanum wheels can't provide odometry**: wheel slip during lateral movement makes encoder-based position tracking unreliable
- **Battery voltage affects LiDAR**: low voltage → motor speed fluctuations → inconsistent scan data. Charge when <9V
