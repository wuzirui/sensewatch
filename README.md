# SenseWatch

macOS menu bar app that monitors SenseCore GPU clusters — jobs, containers, GPU availability, and connection health — with native notifications.

![menu bar](https://img.shields.io/badge/macOS-menu_bar-black) ![python](https://img.shields.io/badge/python-3.10+-blue)

## What it does

- **My Jobs** — your ACP training jobs filtered by IAM username, with live log preview
- **My CCI Containers** — start/stop controls, status monitoring
- **GPU Availability** — real-time idle GPU counts per cluster via `sco` CLI
- **Connection Health** — per-service reachability (AEC2, CCI, Monitor)
- **Notifications** — native macOS banners when jobs start, succeed, fail, or stop
- **Personality** — rotating flavor text, because staring at a menu bar should be fun

## Prerequisites

- macOS 12+
- Python 3.10+
- SenseCore API credentials (AccessKey ID + Secret)
- `sco` CLI (optional, for accurate GPU counts)

## Install

```bash
git clone git@github.com:wuzirui/sensewatch.git
cd sensewatch
pip install -e .
```

For development (includes pytest and pillow for icon generation):

```bash
pip install -e ".[dev]"
```

## Setup credentials

SenseWatch reads your SenseCore AccessKey from macOS Keychain. Add them once:

```bash
security add-generic-password -s sensecore_access_key_id -a $USER -w "YOUR_ACCESS_KEY_ID"
security add-generic-password -s sensecore_access_key_secret -a $USER -w "YOUR_ACCESS_KEY_SECRET"
```

To verify they're stored:

```bash
security find-generic-password -s sensecore_access_key_id -a $USER -w
```

Each macOS user has their own Keychain, so multiple people can use the same install with different credentials.

## Configure your environment

Copy the example config and fill in your subscription, workspaces, and clusters:

```bash
mkdir -p ~/.sensewatch
cp config.example.json ~/.sensewatch/config.json
```

Edit `~/.sensewatch/config.json`:

```json
{
  "subscription": "YOUR_SUBSCRIPTION_UUID",
  "workspace_zone": "cn-sh-01z",
  "cluster_zone": "cn-sh-01e",
  "workspaces": ["your-workspace-name"],
  "clusters": {
    "your-cluster": {"device": "N6lS", "vram_gb": 80, "nodes": 8}
  },
  "workspace_resource_ids": {
    "your-workspace-name": "WORKSPACE_RESOURCE_UUID"
  },
  "my_usernames": ["YOUR_IAM_USERNAME"]
}
```

Ask your admin for the subscription UUID, workspace names, and cluster details. Your IAM username is visible in the SenseCore Console under your job details (`ownership.user_name`).

## Setup `sco` CLI (recommended)

The `sco` CLI provides accurate GPU availability counts. Without it, SenseWatch falls back to estimating from node counts.

```bash
# Install sco
curl -sSfL https://sco.sensecore.cn/registry/sco/install.sh | sh
export PATH=~/.sco/bin:$PATH

# Install required components
sco components install acp
sco components install aec2

# Configure with your Keychain credentials
AK_ID=$(security find-generic-password -s sensecore_access_key_id -a $USER -w)
AK_SECRET=$(security find-generic-password -s sensecore_access_key_secret -a $USER -w)
sco config profiles create default 2>/dev/null || true
sco config set access_key_id "$AK_ID"
sco config set access_key_secret "$AK_SECRET"
sco config set zone cn-sh-01e
sco config set subscription 0197ee17-b6eb-7846-b2b4-a77c5f509b92
sco config set resource_group default

# Verify
sco aec2 clusters usage --name=computing-cluster-01e
```

## Run

```bash
sensewatch
```

Or:

```bash
python -m sensewatch
```

The eye-chip icon appears in your macOS menu bar. Click to see the full dashboard.

## Enable notifications

If you get a notification error on first run, create the required plist:

```bash
PYTHON_BIN=$(dirname $(which python))
/usr/libexec/PlistBuddy -c 'Add :CFBundleIdentifier string "com.sensewatch"' "$PYTHON_BIN/Info.plist" 2>/dev/null || true
```

## Launch at login (optional)

Create a Launch Agent to start SenseWatch automatically:

```bash
cat > ~/Library/LaunchAgents/com.sensewatch.plist << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.sensewatch</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/env</string>
        <string>sensewatch</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
</dict>
</plist>
PLIST

launchctl load ~/Library/LaunchAgents/com.sensewatch.plist
```

## Architecture

```
Main thread (AppKit run loop)
  └── 2s timer: checks dirty flag → rebuilds NSMenu

Background daemon threads (never touch UI)
  ├── poll_health    every 30s   HEAD pings to each service
  ├── poll_jobs      every 60s   ACP list_training_jobs per workspace
  ├── poll_cci       every 120s  CCI list_apps (paginated)
  ├── poll_gpu       every 300s  sco aec2 clusters usage per cluster
  └── poll_logs      every 60s   sco stream-logs preview for running jobs
```

All I/O runs in background threads. The main thread only rebuilds the menu. This prevents AppKit deadlocks.

## Configuration reference

User-specific settings live in `~/.sensewatch/config.json` (see `config.example.json`).
Polling intervals and other defaults live in `sensewatch/config.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| `POLL_INTERVAL_JOBS` | `60` | Seconds between ACP job polls |
| `POLL_INTERVAL_CCI` | `120` | Seconds between CCI container polls |
| `POLL_INTERVAL_GPU` | `300` | Seconds between GPU availability polls |
| `POLL_INTERVAL_HEALTH` | `30` | Seconds between health pings |
| `NOTIFICATION_COOLDOWN` | `300` | Don't re-notify same job within N seconds |
| `REQUEST_TIMEOUT` | `10` | HTTP request timeout in seconds |

## Tests

```bash
pytest tests/ -v
```

## Troubleshooting

**App doesn't appear in menu bar** — Make sure you're running from a real terminal (not a subprocess). The macOS window server needs the process to be a foreground app.

**"Credentials not found"** — Run the `security add-generic-password` commands above. The account name must match your macOS username (`$USER`).

**GPU availability shows estimates** — Install and configure `sco` CLI (see above). Without it, GPU counts are estimated from node counts.

**Notifications don't appear** — Run the `PlistBuddy` command in the "Enable notifications" section.

**All menu items greyed out** — Quit and restart. This can happen if the app was interrupted during a menu rebuild.
