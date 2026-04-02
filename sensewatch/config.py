"""SenseWatch configuration: endpoints, workspaces, clusters, polling intervals.

User-specific values (subscription, workspaces, clusters, identity) are loaded
from ~/.sensewatch/config.json. See config.example.json for the template.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("sensewatch")

# ── User config file ──────────────────────────────────────────────────────────
CONFIG_DIR = Path.home() / ".sensewatch"
CONFIG_FILE = CONFIG_DIR / "config.json"

# ── Service endpoints (public, not user-specific) ─────────────────────────────
AEC2_BASE = "https://aec2.cn-sh-01.sensecoreapi.cn"
CCI_BASE = "https://cci.cn-sh-01.sensecore.cn"
MONITOR_BASE = "https://monitor.sensecoreapi.cn"

# ── Polling intervals (seconds) ──────────────────────────────────────────────
POLL_INTERVAL_JOBS = 60
POLL_INTERVAL_CCI = 120
POLL_INTERVAL_GPU = 300
POLL_INTERVAL_HEALTH = 30

# ── Notification ──────────────────────────────────────────────────────────────
NOTIFICATION_COOLDOWN = 300

# ── Keychain ──────────────────────────────────────────────────────────────────
KEYCHAIN_AKID_SERVICE = "sensecore_access_key_id"
KEYCHAIN_SECRET_SERVICE = "sensecore_access_key_secret"

# ── Job states ────────────────────────────────────────────────────────────────
ACTIVE_JOB_STATES = {"RUNNING", "CREATING", "STARTING", "INIT", "PENDING"}
TERMINAL_JOB_STATES = {"SUCCEEDED", "FAILED", "STOPPED", "DELETED"}

# ── HTTP ──────────────────────────────────────────────────────────────────────
REQUEST_TIMEOUT = 10

# ── User-specific (loaded from config.json) ───────────────────────────────────
SUBSCRIPTION: str = ""
RESOURCE_GROUP: str = "default"
WORKSPACE_ZONE: str = ""
CLUSTER_ZONE: str = ""
WORKSPACES: list[str] = []
CLUSTERS: dict[str, dict] = {}
WORKSPACE_RESOURCE_IDS: dict[str, str] = {}
MY_USERNAMES: set[str] | None = None


def load_user_config() -> bool:
    """Load user-specific config from ~/.sensewatch/config.json.

    Returns True if loaded successfully, False if file missing/invalid.
    """
    global SUBSCRIPTION, RESOURCE_GROUP, WORKSPACE_ZONE, CLUSTER_ZONE
    global WORKSPACES, CLUSTERS, WORKSPACE_RESOURCE_IDS, MY_USERNAMES

    if not CONFIG_FILE.exists():
        return False

    try:
        data = json.loads(CONFIG_FILE.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.error("Failed to load %s: %s", CONFIG_FILE, e)
        return False

    SUBSCRIPTION = data.get("subscription", SUBSCRIPTION)
    RESOURCE_GROUP = data.get("resource_group", RESOURCE_GROUP)
    WORKSPACE_ZONE = data.get("workspace_zone", WORKSPACE_ZONE)
    CLUSTER_ZONE = data.get("cluster_zone", CLUSTER_ZONE)
    WORKSPACES = data.get("workspaces", WORKSPACES)
    CLUSTERS = data.get("clusters", CLUSTERS)
    WORKSPACE_RESOURCE_IDS = data.get("workspace_resource_ids", WORKSPACE_RESOURCE_IDS)

    usernames = data.get("my_usernames")
    if usernames:
        MY_USERNAMES = set(usernames)

    return True


def config_missing_message() -> str:
    """Return setup instructions when config.json is missing."""
    return (
        f"Config file not found: {CONFIG_FILE}\n\n"
        "Create it from the template:\n\n"
        f"  mkdir -p {CONFIG_DIR}\n"
        f"  cp config.example.json {CONFIG_FILE}\n\n"
        "Then edit with your subscription, workspaces, and IAM username."
    )
