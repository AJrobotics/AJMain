/**
 * RosMaster X3 LiDAR Radar Visualization
 * Connects via WebSocket, renders 2D point cloud on Canvas.
 * Only redraws when new scan data arrives to avoid flickering.
 */

const canvas = document.getElementById('lidar-canvas');
const ctx = canvas.getContext('2d');

// State
let scanPoints = [];
let needsRedraw = true;
let scanCount = 0;
let lastFpsTime = performance.now();
let scanFps = 0;
let wsLidar = null;
let wsStatus = null;
let isSimulated = false;
let isConnected = false;
let collisionSectors = [9999, 9999, 9999, 9999, 9999, 9999, 9999, 9999];
let collisionThresholds = { stop: 200, slow: 500, caution: 800 };
let ignoreAngle = 120;

// Range rings in meters
const RANGE_RINGS = [0.5, 1.0, 2.0, 4.0];
const MAX_RANGE_M = 5.0;
const POINT_COLOR = '#00ff88';
const POINT_COLOR_SIM = '#ffaa00';
const RING_COLOR = '#1a3a2a';
const RING_LABEL_COLOR = '#335533';
const AXIS_COLOR = '#222244';
const BG_COLOR = '#0a0a1a';

function resizeCanvas() {
    const rect = canvas.parentElement.getBoundingClientRect();
    canvas.width = rect.width * window.devicePixelRatio;
    canvas.height = rect.height * window.devicePixelRatio;
    canvas.style.width = rect.width + 'px';
    canvas.style.height = rect.height + 'px';
    needsRedraw = true;
}

function drawBackground(w, h, cx, cy, scale) {
    // Clear
    ctx.fillStyle = BG_COLOR;
    ctx.fillRect(0, 0, w, h);

    // Cross axes
    ctx.strokeStyle = AXIS_COLOR;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(cx, 0); ctx.lineTo(cx, h);
    ctx.moveTo(0, cy); ctx.lineTo(w, cy);
    ctx.stroke();

    // Range rings
    const dpr = window.devicePixelRatio;
    for (const r of RANGE_RINGS) {
        const px = r * scale;
        ctx.strokeStyle = RING_COLOR;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.arc(cx, cy, px, 0, Math.PI * 2);
        ctx.stroke();
        ctx.fillStyle = RING_LABEL_COLOR;
        ctx.font = `${11 * dpr}px monospace`;
        ctx.fillText(r + 'm', cx + px + 4, cy - 4);
    }

    // Forward indicator
    ctx.strokeStyle = '#333355';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(cx, cy - 40 * dpr);
    ctx.stroke();
    ctx.fillStyle = '#333355';
    ctx.font = `${10 * dpr}px monospace`;
    ctx.fillText('FWD', cx + 4, cy - 42 * dpr);

    // Draw rear ignore zone
    if (ignoreAngle > 0) {
        const halfIgnore = ignoreAngle / 2;
        const startDeg = 180 - halfIgnore - 90;  // convert to canvas coords (0=right, -90=up)
        const endDeg = 180 + halfIgnore - 90;
        ctx.fillStyle = 'rgba(80, 40, 40, 0.3)';
        ctx.beginPath();
        ctx.moveTo(cx, cy);
        ctx.arc(cx, cy, Math.min(cx, cy), startDeg * Math.PI / 180, endDeg * Math.PI / 180);
        ctx.closePath();
        ctx.fill();

        // Label
        ctx.fillStyle = '#664444';
        ctx.font = `${10 * dpr}px monospace`;
        ctx.fillText('IGNORE', cx - 20 * dpr, cy + Math.min(cx, cy) * 0.5);
    }

    // Robot dot
    ctx.fillStyle = '#00d4ff';
    ctx.beginPath();
    ctx.arc(cx, cy, 4 * dpr, 0, Math.PI * 2);
    ctx.fill();
}

function drawPoints(cx, cy, scale) {
    if (scanPoints.length === 0) return;

    const color = isSimulated ? POINT_COLOR_SIM : POINT_COLOR;
    const dpr = window.devicePixelRatio;
    const dotSize = 2 * dpr;

    ctx.fillStyle = color;
    ctx.beginPath();

    const halfIgnore = ignoreAngle / 2;
    for (const p of scanPoints) {
        const distM = p.dist / 1000.0;
        if (distM < 0.025 || distM > MAX_RANGE_M) continue;

        // Skip points in rear ignore zone
        let angleFromRear = Math.abs(((p.angle - 180) + 180) % 360 - 180);
        if (angleFromRear < halfIgnore) continue;

        // 0 deg = forward (up), clockwise
        const angleRad = (p.angle - 90) * Math.PI / 180.0;
        const px = cx + Math.cos(angleRad) * distM * scale;
        const py = cy + Math.sin(angleRad) * distM * scale;

        ctx.rect(px - dotSize / 2, py - dotSize / 2, dotSize, dotSize);
    }

    ctx.fill();
}

function drawCollisionZones(cx, cy, scale) {
    const dpr = window.devicePixelRatio;
    const sectorAngle = Math.PI * 2 / 8;
    const {stop, slow, caution} = collisionThresholds;

    for (let i = 0; i < 8; i++) {
        const dist = collisionSectors[i];
        let color;
        if (dist < stop) {
            color = 'rgba(255, 40, 40, 0.25)';
        } else if (dist < slow) {
            color = 'rgba(255, 160, 0, 0.20)';
        } else if (dist < caution) {
            color = 'rgba(255, 255, 0, 0.10)';
        } else {
            continue; // CLEAR — don't draw
        }

        // Sector i: starts at (i * 45° - 22.5°), converted with 0=front=up
        const startAngle = (i * 45 - 22.5 - 90) * Math.PI / 180;
        const endAngle = (i * 45 + 22.5 - 90) * Math.PI / 180;
        const radius = Math.min(dist / 1000.0, MAX_RANGE_M) * scale;

        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.moveTo(cx, cy);
        ctx.arc(cx, cy, radius, startAngle, endAngle);
        ctx.closePath();
        ctx.fill();

        // Draw sector boundary outline
        ctx.strokeStyle = color.replace(/[\d.]+\)$/, '0.5)');
        ctx.lineWidth = 1;
        ctx.stroke();
    }
}

function render() {
    if (!needsRedraw) {
        requestAnimationFrame(render);
        return;
    }
    needsRedraw = false;

    const w = canvas.width;
    const h = canvas.height;
    const cx = w / 2;
    const cy = h / 2;
    const scale = Math.min(cx, cy) / MAX_RANGE_M;

    drawBackground(w, h, cx, cy, scale);
    drawCollisionZones(cx, cy, scale);
    drawPoints(cx, cy, scale);

    // Update info text
    document.getElementById('lidar-fps').textContent = scanFps + ' scans/s';
    document.getElementById('lidar-points').textContent = scanPoints.length + ' points';
    document.getElementById('lidar-mode').textContent = isSimulated ? 'SIMULATED' : 'LIVE';

    requestAnimationFrame(render);
}

// --- WebSocket ---

function connectLidarWS() {
    const host = window.location.host;
    wsLidar = new WebSocket(`ws://${host}/ws/lidar`);

    wsLidar.onopen = () => {
        console.log('LiDAR WebSocket connected');
    };

    wsLidar.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === 'scan' && data.points.length > 0) {
            // Skip if point count drops significantly (likely partial scan)
            if (scanPoints.length > 50 && data.points.length < scanPoints.length * 0.5) {
                return;
            }
            scanPoints = data.points;
            isSimulated = data.simulated;
            isConnected = data.connected;
            needsRedraw = true;

            // Calculate scan rate
            scanCount++;
            const now = performance.now();
            if (now - lastFpsTime > 1000) {
                scanFps = Math.round(scanCount * 1000 / (now - lastFpsTime));
                scanCount = 0;
                lastFpsTime = now;
            }

            // Update status dot
            const dot = document.getElementById('dot-lidar');
            if (isConnected && !isSimulated) {
                dot.className = 'dot dot-ok';
            } else if (isConnected && isSimulated) {
                dot.className = 'dot dot-sim';
            } else {
                dot.className = 'dot dot-err';
            }
        }
    };

    wsLidar.onclose = () => {
        document.getElementById('dot-lidar').className = 'dot dot-err';
        setTimeout(connectLidarWS, 2000);
    };

    wsLidar.onerror = () => { wsLidar.close(); };
}

function connectDepthWS() {
    const host = window.location.host;
    const ws = new WebSocket(`ws://${host}/ws/depth`);

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === 'depth' && data.image) {
            const img = document.getElementById('depth-image');
            img.src = 'data:image/jpeg;base64,' + data.image;
            img.style.display = 'block';
            img.style.transform = 'scaleX(-1)';
            document.getElementById('depth-placeholder').style.display = 'none';

            const s = data.stats;
            document.getElementById('depth-stats').textContent =
                `${s.width}x${s.height} | ${s.min}-${s.max}mm`;

            document.getElementById('dot-depth').className = 'dot dot-ok';

            // Floor detection
            if (data.floor_image) {
                const floorImg = document.getElementById('floor-image');
                floorImg.src = 'data:image/jpeg;base64,' + data.floor_image;
                floorImg.style.display = 'block';
                floorImg.style.transform = 'scaleX(-1)';
                document.getElementById('floor-placeholder').style.display = 'none';

                const fs = data.floor_stats || {};
                const obstTxt = fs.has_obstacle ? 'OBSTACLE DETECTED' : 'Clear';
                const distTxt = fs.min_dist > 0 ? ` ${fs.min_dist}mm` : '';
                const el = document.getElementById('floor-stats');
                el.textContent = obstTxt + distTxt;
                el.style.color = fs.has_obstacle ? '#ff4444' : '#00ff88';
            }
        }
    };

    ws.onclose = () => {
        document.getElementById('dot-depth').className = 'dot dot-err';
        setTimeout(connectDepthWS, 3000);
    };
    ws.onerror = () => { ws.close(); };
}

function connectCamWS(endpoint, imgId, placeholderId, dotId) {
    const host = window.location.host;
    const ws = new WebSocket(`ws://${host}${endpoint}`);

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.image) {
            const img = document.getElementById(imgId);
            img.src = 'data:image/jpeg;base64,' + data.image;
            img.style.display = 'block';
            document.getElementById(placeholderId).style.display = 'none';
            document.getElementById(dotId).className = 'dot dot-ok';
        }
    };

    ws.onclose = () => {
        document.getElementById(dotId).className = 'dot dot-err';
        setTimeout(() => connectCamWS(endpoint, imgId, placeholderId, dotId), 3000);
    };
    ws.onerror = () => { ws.close(); };
}

function connectCollisionWS() {
    const host = window.location.host;
    const ws = new WebSocket(`ws://${host}/ws/collision`);

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === 'collision') {
            collisionSectors = data.sectors;
            if (data.thresholds) collisionThresholds = data.thresholds;
            needsRedraw = true;

            // Update collision panel
            const levelEl = document.getElementById('collision-level');
            levelEl.textContent = data.level;
            levelEl.style.color = data.level === 'STOP' ? '#ff4444' :
                                  data.level === 'SLOW' ? '#ffaa00' :
                                  data.level === 'CAUTION' ? '#ffff44' : '#00ff88';

            document.getElementById('collision-min').textContent = data.min_dist + 'mm';
            document.getElementById('collision-enabled').textContent = data.enabled ? 'ON' : 'OFF';

            if (data.ignore_angle !== undefined) {
                ignoreAngle = data.ignore_angle;
                const input = document.getElementById('ignore-angle-input');
                if (document.activeElement !== input) {
                    input.value = ignoreAngle;
                }
            }
        }
    };

    ws.onclose = () => { setTimeout(connectCollisionWS, 3000); };
    ws.onerror = () => { ws.close(); };
}

function connectStatusWS() {
    const host = window.location.host;
    wsStatus = new WebSocket(`ws://${host}/ws/status`);

    wsStatus.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === 'status') {
            document.getElementById('battery-val').textContent = data.battery + 'V';
            document.getElementById('ip-val').textContent = data.ip;

            if (data.depth_connected) {
                document.getElementById('dot-depth').className = 'dot dot-ok';
            }

            if (data.imu) {
                document.getElementById('imu-roll').textContent = data.imu.angles.roll.toFixed(1) + '\u00B0';
                document.getElementById('imu-pitch').textContent = data.imu.angles.pitch.toFixed(1) + '\u00B0';
                document.getElementById('imu-yaw').textContent = data.imu.angles.yaw.toFixed(1) + '\u00B0';
                document.getElementById('imu-az').textContent = data.imu.accel.z.toFixed(2) + ' m/s\u00B2';
            }
        }
    };

    wsStatus.onclose = () => { setTimeout(connectStatusWS, 3000); };
    wsStatus.onerror = () => { wsStatus.close(); };
}

// --- Init ---

window.addEventListener('resize', resizeCanvas);
resizeCanvas();
connectLidarWS();
connectDepthWS();
connectCamWS('/ws/cam/primary', 'cam-primary-image', 'cam-primary-placeholder', 'dot-cam1');
connectCollisionWS();
connectSlamWS();
connectStatusWS();
requestAnimationFrame(render);

// --- SLAM + Explorer ---

function connectSlamWS() {
    const host = window.location.host;
    const ws = new WebSocket(`ws://${host}/ws/slam`);

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === 'slam') {
            if (data.image) {
                const img = document.getElementById('slam-map');
                img.src = 'data:image/jpeg;base64,' + data.image;
            }
            document.getElementById('slam-info').textContent =
                `Scans: ${data.scans} | Pose: (${data.pose.x}, ${data.pose.y}) ${data.pose.theta}\u00B0`;

            if (data.explorer) {
                const stEl = document.getElementById('explorer-state');
                stEl.textContent = data.explorer.state.toUpperCase();
                stEl.style.color = data.explorer.state === 'exploring' ? '#00ff88' :
                                   data.explorer.state === 'returning' ? '#ffaa00' :
                                   data.explorer.state === 'arrived' ? '#00d4ff' : '#e0e0e0';
                document.getElementById('explorer-frontiers').textContent = data.explorer.num_frontiers;
            }
        }
    };

    ws.onclose = () => { setTimeout(connectSlamWS, 3000); };
    ws.onerror = () => { ws.close(); };
}

function explorerCmd(action) {
    fetch('/api/explorer', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action: action}),
    }).then(r => r.json()).then(d => {
        if (d.error) {
            document.getElementById('explorer-state').textContent = d.error;
        }
    });
}

window.explorerCmd = explorerCmd;

// --- Calibration functions ---

function runCal(test) {
    const distance = parseInt(document.getElementById('cal-distance').value) || 500;
    fetch('/api/calibration', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action: 'run', test: test, distance: distance}),
    }).then(r => r.json()).then(d => {
        document.getElementById('cal-status').textContent = 'RUNNING: ' + test;
        pollCalStatus();
    });
}

function runCalAll() {
    const distance = parseInt(document.getElementById('cal-distance').value) || 500;
    fetch('/api/calibration', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action: 'run_all', distance: distance}),
    }).then(r => r.json()).then(d => {
        document.getElementById('cal-status').textContent = 'RUNNING ALL...';
        pollCalStatus();
    });
}

function abortCal() {
    fetch('/api/calibration', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action: 'abort'}),
    });
    document.getElementById('cal-status').textContent = 'ABORTED';
}

function pollCalStatus() {
    fetch('/api/calibration').then(r => r.json()).then(data => {
        const stEl = document.getElementById('cal-status');
        if (data.current_test) {
            stEl.textContent = 'RUNNING: ' + data.current_test;
        } else {
            stEl.textContent = data.state.toUpperCase();
        }

        // Show results
        const resEl = document.getElementById('cal-results');
        if (data.results && data.results.length > 0) {
            resEl.innerHTML = data.results.map(r => {
                const dir = r.direction || '?';
                const st = r.status || '?';
                const detail = r.imu_yaw_delta !== undefined ? ` yaw:${r.imu_yaw_delta}\u00B0` : '';
                const dur = r.actual_duration !== undefined ? ` ${r.actual_duration}s` : '';
                const color = st === 'done' ? '#00ff88' : st === 'blocked' ? '#ffaa00' : '#ff4444';
                return `<div style="color:${color}">${dir}: ${st}${dur}${detail}</div>`;
            }).join('');
        }

        if (data.state === 'running') {
            setTimeout(pollCalStatus, 500);
        }
    });
}

// Make calibration functions global for onclick
window.runCal = runCal;
window.runCalAll = runCalAll;
window.abortCal = abortCal;

// Ignore angle input handler
document.getElementById('ignore-angle-input').addEventListener('change', (e) => {
    const val = parseInt(e.target.value) || 0;
    fetch('/api/collision', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ignore_angle: val}),
    });
});
