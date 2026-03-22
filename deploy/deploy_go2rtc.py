#!/usr/bin/env python3
"""
Deploy go2rtc to Christy for Eufy camera RTSP→WebRTC streaming.

Usage:
    python -m deploy.deploy_go2rtc                # Deploy go2rtc + config
    python -m deploy.deploy_go2rtc --restart       # Restart go2rtc service
    python -m deploy.deploy_go2rtc --status        # Check go2rtc status
    python -m deploy.deploy_go2rtc --generate-yaml # Generate go2rtc.yaml locally (preview)

Reads camera config from configs/cameras.json.
Installs go2rtc binary + systemd service on Christy (192.168.1.94).
"""

import argparse
import json
import os
import sys
from datetime import datetime

import paramiko

# ── Paths ──

LOCAL_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAMERAS_JSON = os.path.join(LOCAL_BASE, "configs", "cameras.json")
HOSTS_JSON = os.path.join(LOCAL_BASE, "configs", "hosts.json")

GO2RTC_VERSION = "1.9.8"
GO2RTC_BINARY_URL = (
    f"https://github.com/AlexxIT/go2rtc/releases/download/v{GO2RTC_VERSION}"
    "/go2rtc_linux_{arch}"
)

INSTALL_DIR = "/opt/go2rtc"
CONFIG_PATH = f"{INSTALL_DIR}/go2rtc.yaml"
BINARY_PATH = f"{INSTALL_DIR}/go2rtc"

SERVICE_TEMPLATE = """\
[Unit]
Description=go2rtc - RTSP to WebRTC bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={binary} -config {config}
WorkingDirectory={install_dir}
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
"""

# ── SSH helpers (reuse from deploy.py pattern) ──

_SSH_DIR = os.path.join(
    os.environ.get("USERPROFILE", os.path.expanduser("~")), ".ssh"
) if os.name == "nt" else os.path.join(os.path.expanduser("~"), ".ssh")

_SSH_KEY_PATHS = []
for _kn in ["id_ed25519", "id_rsa"]:
    _kp = os.path.join(_SSH_DIR, _kn)
    if os.path.exists(_kp):
        _SSH_KEY_PATHS.append(_kp)


def connect(host, user):
    """Create paramiko SSH client."""
    last_err = None
    for key_path in (_SSH_KEY_PATHS or [None]):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            kwargs = {
                "hostname": host,
                "username": user,
                "timeout": 5,
                "banner_timeout": 5,
                "auth_timeout": 5,
                "allow_agent": False,
                "look_for_keys": False,
            }
            if key_path:
                kwargs["key_filename"] = key_path
            client.connect(**kwargs)
            return client
        except paramiko.AuthenticationException as e:
            last_err = e
            try:
                client.close()
            except Exception:
                pass
    raise last_err or paramiko.AuthenticationException("No valid SSH key")


def ssh_run(client, command, quiet=False, timeout=30):
    """Run a command via SSH, return (stdout, rc)."""
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    rc = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    if not quiet:
        if out.strip():
            print(f"  {out.strip()}")
        if err.strip() and rc != 0:
            print(f"  STDERR: {err.strip()}", file=sys.stderr)
    return out.strip(), rc


# ── Config generation ──

def load_cameras_config():
    """Load cameras.json."""
    with open(CAMERAS_JSON, "r") as f:
        return json.load(f)


def load_christy_info():
    """Get Christy's SSH info from hosts.json."""
    with open(HOSTS_JSON, "r") as f:
        hosts = json.load(f)
    christy = hosts.get("computers", {}).get("Christy")
    if not christy:
        raise ValueError("Christy not found in hosts.json")
    return christy


def generate_go2rtc_yaml(config):
    """Generate go2rtc.yaml content from cameras.json."""
    cameras = config.get("cameras", {})

    lines = ["streams:"]
    for cam_id, cam in cameras.items():
        # P2P cameras use go2rtc_source (exec:ffmpeg), RTSP cameras use rtsp_url
        source = cam.get("go2rtc_source")
        if not source:
            source = cam.get("rtsp_url")
        if not source:
            creds = config.get("rtsp_credentials", {})
            template = config.get("rtsp_url_template", "rtsp://{user}:{pass}@{ip}:{port}/live0")
            source = template.format(
                user=creds.get("user", ""),
                **{"pass": creds.get("pass", "")},
                ip=cam["ip"],
                port=cam.get("rtsp_port", 554),
            )
        lines.append(f"  {cam_id}: {source}")

    # WebRTC / API config
    lines.append("")
    lines.append("webrtc:")
    lines.append("  candidates:")
    lines.append(f"    - {config['go2rtc']['host']}:8555")
    lines.append("")
    lines.append("api:")
    lines.append("  listen: \":1984\"")
    lines.append("  origin: \"*\"")

    return "\n".join(lines) + "\n"


# ── Deploy actions ──

def detect_arch(client):
    """Detect remote architecture (amd64 or arm64)."""
    out, _ = ssh_run(client, "uname -m", quiet=True)
    arch = out.strip()
    if arch in ("x86_64", "amd64"):
        return "amd64"
    elif arch in ("aarch64", "arm64"):
        return "arm64"
    else:
        print(f"  [WARN] Unknown arch: {arch}, defaulting to amd64")
        return "amd64"


def deploy_go2rtc(host, user):
    """Full deploy: download binary, upload config, create service."""
    config = load_cameras_config()
    yaml_content = generate_go2rtc_yaml(config)

    print(f"\n  Connecting to {user}@{host}...")
    client = connect(host, user)

    try:
        # Detect architecture
        arch = detect_arch(client)
        download_url = GO2RTC_BINARY_URL.format(arch=arch)
        print(f"  Architecture: {arch}")
        print(f"  go2rtc version: v{GO2RTC_VERSION}")

        # Create install directory
        ssh_run(client, f"sudo mkdir -p {INSTALL_DIR}", quiet=True)

        # Check if binary already exists
        out, rc = ssh_run(client, f"test -f {BINARY_PATH} && echo EXISTS", quiet=True)
        if "EXISTS" not in out:
            print(f"  Downloading go2rtc...")
            ssh_run(client, f"sudo wget -q -O {BINARY_PATH} {download_url}", timeout=60)
            ssh_run(client, f"sudo chmod +x {BINARY_PATH}", quiet=True)
            print(f"  [OK] Binary installed")
        else:
            print(f"  [OK] Binary already exists, skipping download")

        # Upload go2rtc.yaml via SFTP
        print(f"  Uploading go2rtc.yaml...")
        # Write to temp first, then sudo move
        sftp = client.open_sftp()
        with sftp.open("/tmp/go2rtc.yaml", "w") as f:
            f.write(yaml_content)
        sftp.close()
        ssh_run(client, f"sudo mv /tmp/go2rtc.yaml {CONFIG_PATH}", quiet=True)
        ssh_run(client, f"sudo chmod 600 {CONFIG_PATH}", quiet=True)
        print(f"  [OK] Config uploaded")

        # Create systemd service
        print(f"  Setting up systemd service...")
        service_content = SERVICE_TEMPLATE.format(
            binary=BINARY_PATH,
            config=CONFIG_PATH,
            install_dir=INSTALL_DIR,
        )
        sftp = client.open_sftp()
        with sftp.open("/tmp/go2rtc.service", "w") as f:
            f.write(service_content)
        sftp.close()
        ssh_run(client, "sudo mv /tmp/go2rtc.service /etc/systemd/system/go2rtc.service", quiet=True)
        ssh_run(client, "sudo systemctl daemon-reload", quiet=True)
        ssh_run(client, "sudo systemctl enable go2rtc.service", quiet=True)
        print(f"  [OK] Service created and enabled")

        # Start/restart
        print(f"  Starting go2rtc...")
        ssh_run(client, "sudo systemctl restart go2rtc.service", quiet=True)

        import time
        time.sleep(2)

        # Verify
        out, rc = ssh_run(client, "sudo systemctl is-active go2rtc.service", quiet=True)
        if "active" in out:
            print(f"  [OK] go2rtc is running")
            api_url = config["go2rtc"]["api_url"]
            print(f"\n  go2rtc Web UI: {api_url}")
            print(f"  go2rtc API:    {api_url}/api/streams")
        else:
            print(f"  [WARN] go2rtc may not be running: {out}")
            print(f"  Check logs: sudo journalctl -u go2rtc -f")

    finally:
        client.close()


def restart_go2rtc(host, user):
    """Restart go2rtc service."""
    client = connect(host, user)
    ssh_run(client, "sudo systemctl restart go2rtc.service", quiet=True)
    import time
    time.sleep(2)
    out, _ = ssh_run(client, "sudo systemctl is-active go2rtc.service", quiet=True)
    client.close()
    if "active" in out:
        print("  [OK] go2rtc restarted")
    else:
        print(f"  [WARN] Status: {out}")


def check_status(host, user):
    """Check go2rtc status."""
    client = connect(host, user)
    out, _ = ssh_run(client, "sudo systemctl status go2rtc.service --no-pager -l 2>&1 | head -15", quiet=True)
    print(out)
    client.close()


def update_config(host, user):
    """Update only the go2rtc.yaml config and restart."""
    config = load_cameras_config()
    yaml_content = generate_go2rtc_yaml(config)

    client = connect(host, user)
    sftp = client.open_sftp()
    with sftp.open("/tmp/go2rtc.yaml", "w") as f:
        f.write(yaml_content)
    sftp.close()
    ssh_run(client, f"sudo mv /tmp/go2rtc.yaml {CONFIG_PATH}", quiet=True)
    ssh_run(client, f"sudo chmod 600 {CONFIG_PATH}", quiet=True)
    ssh_run(client, "sudo systemctl restart go2rtc.service", quiet=True)

    import time
    time.sleep(2)
    out, _ = ssh_run(client, "sudo systemctl is-active go2rtc.service", quiet=True)
    client.close()

    if "active" in out:
        print("  [OK] Config updated and go2rtc restarted")
    else:
        print(f"  [WARN] Status after restart: {out}")


# ── Main ──

def main():
    parser = argparse.ArgumentParser(
        description="Deploy go2rtc to Christy for camera streaming",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--restart", action="store_true", help="Restart go2rtc service")
    parser.add_argument("--status", action="store_true", help="Check go2rtc service status")
    parser.add_argument("--update-config", action="store_true", help="Update config and restart")
    parser.add_argument("--generate-yaml", action="store_true", help="Print go2rtc.yaml to stdout (preview)")
    args = parser.parse_args()

    print()
    print("=" * 55)
    print("  AJ Robotics — go2rtc Deploy")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 55)

    if args.generate_yaml:
        config = load_cameras_config()
        print()
        print(generate_go2rtc_yaml(config))
        return

    # Get Christy connection info
    christy = load_christy_info()
    host = christy["host"]
    user = christy["username"]
    print(f"\n  Target: Christy ({user}@{host})")

    if args.status:
        check_status(host, user)
        return

    if args.restart:
        restart_go2rtc(host, user)
        return

    if args.update_config:
        update_config(host, user)
        return

    # Full deploy
    deploy_go2rtc(host, user)

    print()
    print("=" * 55)
    print("  go2rtc deploy complete!")
    print("=" * 55)
    print()


if __name__ == "__main__":
    main()
