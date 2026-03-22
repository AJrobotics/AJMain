/**
 * Eufy Event Monitor — listens for push notifications (motion, doorbell, etc.)
 * from eufy-security-client and exposes events via a simple HTTP API.
 *
 * For battery cameras like Solo S3, continuous streaming drains the battery.
 * Instead, this monitors motion/person detection events and stores recent events.
 *
 * HTTP API (port 63340):
 *   GET /api/events          — recent push events (last 50)
 *   GET /api/events/latest   — most recent push event
 *   GET /api/events/history  — cloud event history (thumbnails + metadata)
 *   GET /api/devices         — all discovered devices
 *   GET /api/status          — bridge status
 *
 * Usage:
 *   node bridge.js
 */

const { EufySecurity } = require("eufy-security-client");
const http = require("http");
const path = require("path");
const fs = require("fs");

const EUFY_EMAIL = process.env.EUFY_EMAIL || "Dreamittogether@gmail.com";
const EUFY_PASSWORD = process.env.EUFY_PASSWORD || "Dcl406996!";
const API_PORT = parseInt(process.env.API_PORT || "63340", 10);
const PERSISTENT_DIR = path.join(__dirname, "persistent");
const MAX_EVENTS = 50;

if (!fs.existsSync(PERSISTENT_DIR)) {
    fs.mkdirSync(PERSISTENT_DIR, { recursive: true });
}

// State
let events = [];
let cloudEvents = [];   // Cloud event history (with thumbnails)
let devices = {};
let connected = false;
let lastError = null;
let eufyClientRef = null;  // Reference for cloud API calls

function addEvent(type, deviceSN, deviceName, data) {
    const event = {
        timestamp: new Date().toISOString(),
        type,
        deviceSN,
        deviceName,
        data,
    };
    events.unshift(event);
    if (events.length > MAX_EVENTS) events.pop();
    console.log(`[EVENT] ${type} from ${deviceName} (${deviceSN})`);
    return event;
}

// Fetch cloud event history (thumbnails + metadata)
async function fetchCloudEvents() {
    if (!eufyClientRef || !connected) return;
    try {
        const httpService = eufyClientRef.getApi();
        if (!httpService) return;

        // Try with date range (last 7 days)
        const now = new Date();
        const weekAgo = new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000);

        let result = [];
        try {
            result = await httpService.getVideoEvents(weekAgo, now);
        } catch (_) {
            // Fallback to getAllVideoEvents (no date range)
            result = await httpService.getAllVideoEvents();
        }

        if (result && Array.isArray(result) && result.length > 0) {
            cloudEvents = result.slice(0, 100).map(ev => ({
                event_id: ev.id || ev.event_id,
                device_sn: ev.device_sn,
                station_sn: ev.station_sn,
                event_time: ev.event_time ? new Date(ev.event_time * 1000).toISOString() : null,
                event_type: ev.event_type,
                thumbnail_url: ev.pic_url || ev.thumb_url || ev.thumbnail,
                file_path: ev.file_path,
                cipher_id: ev.cipher_id,
                viewed: ev.viewed,
                title: ev.title,
                content: ev.content,
            }));
            console.log(`[CLOUD] Fetched ${cloudEvents.length} cloud events`);
        } else {
            console.log("[CLOUD] No cloud events available (cloud storage may not be enabled)");
        }
    } catch (err) {
        console.error("[CLOUD] Error fetching cloud events:", err.message);
    }
}

// HTTP API server
const apiServer = http.createServer((req, res) => {
    res.setHeader("Content-Type", "application/json");
    res.setHeader("Access-Control-Allow-Origin", "*");

    const url = new URL(req.url, `http://${req.headers.host}`);

    if (url.pathname === "/api/events") {
        res.end(JSON.stringify({ events }));
    } else if (url.pathname === "/api/events/latest") {
        res.end(JSON.stringify({ event: events[0] || null }));
    } else if (url.pathname === "/api/events/history") {
        // Cloud event history — optionally filter by device_sn
        const sn = url.searchParams.get("sn");
        const filtered = sn
            ? cloudEvents.filter(e => e.device_sn === sn)
            : cloudEvents;
        res.end(JSON.stringify({ events: filtered, source: "cloud" }));
    } else if (url.pathname === "/api/devices") {
        res.end(JSON.stringify({ devices }));
    } else if (url.pathname === "/api/status") {
        res.end(JSON.stringify({
            connected,
            lastError,
            eventCount: events.length,
            cloudEventCount: cloudEvents.length,
            deviceCount: Object.keys(devices).length,
            uptime: process.uptime(),
        }));
    } else {
        res.statusCode = 404;
        res.end(JSON.stringify({ error: "not found" }));
    }
});

async function main() {
    console.log("=== Eufy Event Monitor ===");
    console.log(`Email: ${EUFY_EMAIL}`);
    console.log(`API port: ${API_PORT}`);

    apiServer.listen(API_PORT, "0.0.0.0", () => {
        console.log(`[API] HTTP server listening on 0.0.0.0:${API_PORT}`);
    });

    const eufyClient = await EufySecurity.initialize({
        username: EUFY_EMAIL,
        password: EUFY_PASSWORD,
        persistentDir: PERSISTENT_DIR,
        p2pConnectionSetup: 0,
        pollingIntervalMinutes: 5,
        acceptInvitations: false,
    });

    eufyClient.on("connect", () => {
        connected = true;
        lastError = null;
        console.log("[EUFY] Connected to cloud");
    });

    eufyClient.on("close", () => {
        connected = false;
        console.log("[EUFY] Connection closed");
    });

    eufyClient.on("connection error", (error) => {
        lastError = error?.message || String(error);
        console.error("[EUFY] Connection error:", lastError);
    });

    eufyClient.on("tfa request", () => {
        lastError = "2FA required - re-auth needed";
        console.error("[EUFY] 2FA requested! Delete persistent/ and re-auth.");
    });

    // Device discovery
    eufyClient.on("device added", (device) => {
        const sn = device.getSerial();
        const name = device.getName();
        const type = device.getDeviceType();
        devices[sn] = { name, type, sn, addedAt: new Date().toISOString() };
        console.log(`[EUFY] Device: ${name} (SN: ${sn}, Type: ${type})`);
    });

    // Push notification events — motion, person, doorbell, etc.
    eufyClient.on("device motion detected", (device, state) => {
        addEvent("motion", device.getSerial(), device.getName(), { state });
    });

    eufyClient.on("device person detected", (device, state) => {
        addEvent("person", device.getSerial(), device.getName(), { state });
    });

    eufyClient.on("device pet detected", (device, state) => {
        addEvent("pet", device.getSerial(), device.getName(), { state });
    });

    eufyClient.on("device crying detected", (device, state) => {
        addEvent("crying", device.getSerial(), device.getName(), { state });
    });

    eufyClient.on("device sound detected", (device, state) => {
        addEvent("sound", device.getSerial(), device.getName(), { state });
    });

    eufyClient.on("device rings", (device, state) => {
        addEvent("doorbell", device.getSerial(), device.getName(), { state });
    });

    eufyClient.on("device property changed", (device, name, value) => {
        // Only log interesting property changes
        if (["motionDetected", "personDetected", "petDetected"].includes(name)) {
            addEvent("property", device.getSerial(), device.getName(), { property: name, value });
        }
    });

    eufyClient.on("push message", (message) => {
        // Raw push notifications — capture all media fields
        if (message.type) {
            const deviceSN = message.device_sn || message.station_sn || "unknown";
            const deviceName = devices[deviceSN]?.name || deviceSN;
            addEvent("push", deviceSN, deviceName, {
                type: message.type,
                title: message.title,
                content: message.content,
                pic_url: message.pic_url,
                file_path: message.file_path,
                doorbell_video_url: message.doorbell_video_url,
                event_time: message.event_time,
                person_name: message.person_name,
                sensor_open: message.sensor_open,
            });
        }
    });

    // Store reference for cloud API calls
    eufyClientRef = eufyClient;

    // Connect
    await eufyClient.connect();
    console.log("[EUFY] Waiting for events...");
    console.log("[EUFY] Monitor is running. Events will appear as they happen.");

    // Fetch cloud event history on startup and periodically (every 5 min)
    setTimeout(() => fetchCloudEvents(), 10000);
    setInterval(() => fetchCloudEvents(), 5 * 60 * 1000);
}

// Graceful shutdown
process.on("SIGINT", () => {
    console.log("\n[BRIDGE] Shutting down...");
    apiServer.close();
    process.exit(0);
});

process.on("SIGTERM", () => {
    console.log("\n[BRIDGE] Shutting down...");
    apiServer.close();
    process.exit(0);
});

main().catch((error) => {
    console.error("[FATAL]", error);
    process.exit(1);
});
