"""SenseWatch configuration: endpoints, workspaces, clusters, polling intervals."""

from __future__ import annotations

# ── Service endpoints ────────────────────────────────────────────────────────
AEC2_BASE = "https://aec2.cn-sh-01.sensecoreapi.cn"
CCI_BASE = "https://cci.cn-sh-01.sensecore.cn"
MONITOR_BASE = "https://monitor.sensecoreapi.cn"

# ── Subscription / org ───────────────────────────────────────────────────────
SUBSCRIPTION = "0197ee17-b6eb-7846-b2b4-a77c5f509b92"
RESOURCE_GROUP = "default"
WORKSPACE_ZONE = "cn-sh-01z"
CLUSTER_ZONE = "cn-sh-01e"

# ── Workspaces to monitor ────────────────────────────────────────────────────
WORKSPACES = ["share-space-01e", "project-one"]

# ── Known clusters ───────────────────────────────────────────────────────────
CLUSTERS: dict[str, dict] = {
    "computing-cluster-01e": {"device": "N6lS", "vram_gb": 80, "nodes": 39},
    "computing-cluster-01e-02": {"device": "N6lS", "vram_gb": 80, "nodes": 3},
    "computing-cluster-01e-hbxx": {"device": "n11ls", "vram_gb": 141, "nodes": 10},
    "debug-cluster-01e": {"device": "N6lS", "vram_gb": 80, "nodes": 2},
}

# ── Workspace resource IDs (for Monitor API) ─────────────────────────────────
WORKSPACE_RESOURCE_IDS: dict[str, str] = {
    "share-space-01e": "01995848-9da4-7b9a-917c-db5bdea185e5",
    "project-one": "019cfffd-a2cb-76b4-a50d-291e0b754b65",
}

# ── Polling intervals (seconds) ──────────────────────────────────────────────
POLL_INTERVAL_JOBS = 60
POLL_INTERVAL_CCI = 120
POLL_INTERVAL_GPU = 300
POLL_INTERVAL_HEALTH = 30

# ── Notification ──────────────────────────────────────────────────────────────
NOTIFICATION_COOLDOWN = 300  # seconds — don't re-notify same job within 5 min

# ── Keychain ──────────────────────────────────────────────────────────────────
KEYCHAIN_AKID_SERVICE = "sensecore_access_key_id"
KEYCHAIN_SECRET_SERVICE = "sensecore_access_key_secret"

# ── Job states ────────────────────────────────────────────────────────────────
ACTIVE_JOB_STATES = {"RUNNING", "CREATING", "STARTING", "INIT", "PENDING"}
TERMINAL_JOB_STATES = {"SUCCEEDED", "FAILED", "STOPPED", "DELETED"}

# ── User identity (for filtering "my jobs/containers") ────────────────────────
# IAM usernames — you may have different usernames across ACP and CCI.
# Set to None to show all.
MY_USERNAMES: set[str] | None = {"L202500193"}

# ── HTTP ──────────────────────────────────────────────────────────────────────
REQUEST_TIMEOUT = 10  # seconds
