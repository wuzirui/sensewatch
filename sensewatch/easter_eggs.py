"""Ambient personality system — rotating flavor text, notification subtitles, GPU commentary.

Inspired by Claude Code's thinking messages: small surprises woven into everyday use.
Every time you open the menu, it's a little different.
"""

from __future__ import annotations

import random
import time

# ── Flavor text pools ─────────────────────────────────────────────────────

FLAVOR_IDLE = [
    "Watching {gpu_total} GPUs so you don't have to",
    "Tensors are flowing. All is well.",
    "Your gradients are in good hands",
    "Still here. Still watching. Still caffeinated.",
    "{running_jobs} jobs running. 0 existential crises.",
    "Monitoring in progress... like your training",
    "The GPUs hum a quiet song",
    "All quiet on the compute front",
    "Running smoother than your learning rate schedule",
    "Another day, another gradient descent",
    "Somewhere, a weight matrix is being updated",
    "The backprop never stops",
    "Silently judging your hyperparameters",
    "Keeping the cluster under surveillance since {start_time}",
    "This is your friendly neighborhood GPU watcher",
    "Clusters monitored. Coffee consumed.",
    "Your models are training and so is my patience",
    "Every tensor tells a story",
    "64-bit floats in a 32-bit world",
    "I dream of electric GPUs",
    "The loss is dark and full of gradients",
    "In GPUs we trust",
    "May your loss be ever decreasing",
    "Optimizing the optimizer that optimizes",
    "No NaNs detected. Yet.",
    "The cluster abides",
    "Ctrl+C won't help you here",
    "Making sure your GPUs aren't lonely",
    "Epoch by epoch, we carry on",
    "The machines are thinking. Probably.",
    "Your attention mechanism is paying attention",
    "Batch norm: applied. Spirits: normalized.",
    "Somewhere between overfit and underfit",
    "The weight of the world (matrix)",
    "Training is just fancy curve fitting",
    "I have seen things you wouldn't believe. Like NCCL timeouts.",
    "The transformer is more than meets the eye",
    "Convolutions: the OG attention",
    "Remember when we thought 1 GPU was enough?",
    "It's GPUs all the way down",
    "Regularization is just trust issues with your data",
    "Your learning rate is your destiny",
    "From here to convergence",
    "The gradient is a lie. Sometimes.",
    "Broadcasting shapes like a radio station",
    "torch.no_grad() but with feelings",
    "Just a bunch of matrix multiplications in a trench coat",
    "Warm regards from the compute cluster",
    "The scheduler says hello",
    "VRAM: the real bottleneck in life",
]

FLAVOR_POLL_SUCCESS = [
    "Fresh data! Warm from the API.",
    "Just checked. Everything's still there.",
    "Pinged SenseCore. They pinged back.",
    "API responded. We're good.",
    "Data refreshed. Like a cold brew.",
    "All systems nominal.",
]

FLAVOR_DISCONNECTED = [
    "Can't reach SenseCore. Did someone trip over the cable?",
    "The void stares back (network unreachable)",
    "Houston, we have a connectivity problem",
    "I had data. Then the network left.",
    "Connection lost. I'm flying blind.",
    "The API is ghosting us",
    "Signal lost. Retrying with hope.",
    "Is it the VPN? It's always the VPN.",
]

# ── Notification subtitles ────────────────────────────────────────────────

NOTIFY_RUNNING = [
    "Let the tensors flow",
    "GPUs engaged. Training commences.",
    "Your neural network is cooking",
    "Weights are being optimized as we speak",
    "The backpropagation begins",
    "Launch sequence complete",
    "We have ignition",
    "All hands on deck",
]

NOTIFY_SUCCEEDED = [
    "Another one bites the dust (in a good way)",
    "Loss: converged. Researcher: pleased.",
    "The GPUs have done their duty",
    "Checkpoint saved. Glory achieved.",
    "Mission accomplished",
    "The weights have settled",
    "Time to check those metrics",
    "Convergence at last",
]

NOTIFY_FAILED = [
    "It happens to the best of us",
    "The learning rate sends its regrets",
    "Back to the drawing board?",
    "NaN happens.",
    "At least the GPUs are free now",
    "Time to check the logs",
    "The stack trace awaits",
    "Not all experiments succeed. This is fine.",
    "OOM, the silent killer",
]

NOTIFY_STOPPED = [
    "Manually stopped. I hope you meant to do that.",
    "Someone pulled the plug",
    "Training interrupted. Checkpoints may be available.",
    "Halted by command",
]

NOTIFY_CONNECTION_LOST = [
    "We've lost contact with SenseCore",
    "The cluster has gone dark",
    "Network down. Standing by.",
]

NOTIFY_CONNECTION_RESTORED = [
    "We're back online!",
    "Connection restored. Did you miss me?",
    "SenseCore is reachable again",
    "The lights are back on",
]

# ── GPU availability commentary ───────────────────────────────────────────

GPU_PLENTY = [  # >50% idle
    "Plenty of room. Go wild.",
    "The cluster is practically begging for work",
    "Wide open. Submit something!",
    "GPUs gathering dust. Put them to work.",
    "Idle GPUs are sad GPUs",
]

GPU_MODERATE = [  # 20-50% idle
    "Getting busy. Grab yours while you can.",
    "Some room left. Act fast.",
    "Moderately occupied. Nothing urgent.",
    "There's space if you squint",
]

GPU_SCARCE = [  # <20% idle, non-zero
    "Getting cozy in here",
    "Standing room only",
    "Musical chairs, GPU edition",
    "Slim pickings. Good luck.",
    "The early bird gets the GPU",
]

GPU_FULL = [  # 0 idle
    "Fully booked. Try again later.",
    "Everyone brought their A100s to the party",
    "Patience, young researcher",
    "Not a single GPU in sight",
    "Come back tomorrow. Or in 5 minutes.",
]

# ── Uptime milestones ─────────────────────────────────────────────────────

UPTIME_MILESTONES = {
    3600: "SenseWatch has been running for 1 hour. We're getting along great.",
    86400: "24 hours together. That's commitment.",
    604800: "One week straight. You and I, we're inseparable.",
    2592000: "A whole month. We should celebrate.",
}

# ── Personality engine ────────────────────────────────────────────────────

_recent_picks: dict[int, list[int]] = {}  # pool id -> list of recent indices
_HISTORY_SIZE = 5


def pick(pool: list[str]) -> str:
    """Pick a random string from the pool, avoiding the last 5 picks."""
    pool_id = id(pool)
    history = _recent_picks.setdefault(pool_id, [])

    available = [i for i in range(len(pool)) if i not in history]
    if not available:
        # All exhausted, reset history
        history.clear()
        available = list(range(len(pool)))

    idx = random.choice(available)
    history.append(idx)
    if len(history) > _HISTORY_SIZE:
        history.pop(0)

    return pool[idx]


def flavor_text(
    running_jobs: int = 0,
    gpu_total: int = 0,
    start_time: str = "",
    connected: bool = True,
) -> str:
    """Get a rotating flavor text string for the menu footer."""
    if not connected:
        return pick(FLAVOR_DISCONNECTED)

    text = pick(FLAVOR_IDLE)
    return text.format(
        running_jobs=running_jobs,
        gpu_total=gpu_total,
        start_time=start_time,
    )


def notify_subtitle(transition_type: str) -> str:
    """Get a personality subtitle for a notification."""
    pools = {
        "running": NOTIFY_RUNNING,
        "succeeded": NOTIFY_SUCCEEDED,
        "failed": NOTIFY_FAILED,
        "stopped": NOTIFY_STOPPED,
        "connection_lost": NOTIFY_CONNECTION_LOST,
        "connection_restored": NOTIFY_CONNECTION_RESTORED,
    }
    pool = pools.get(transition_type, FLAVOR_IDLE)
    return pick(pool)


def gpu_commentary(idle_gpus: int, total_gpus: int) -> str:
    """Get contextual commentary for GPU availability."""
    if total_gpus == 0:
        return ""
    ratio = idle_gpus / total_gpus
    if idle_gpus == 0:
        return pick(GPU_FULL)
    elif ratio < 0.2:
        return pick(GPU_SCARCE)
    elif ratio < 0.5:
        return pick(GPU_MODERATE)
    else:
        return pick(GPU_PLENTY)


_uptime_notified: set[int] = set()


def uptime_check(start_time: float) -> str | None:
    """Check if we've crossed an uptime milestone. Returns message or None."""
    elapsed = time.time() - start_time
    for threshold, message in sorted(UPTIME_MILESTONES.items()):
        if elapsed >= threshold and threshold not in _uptime_notified:
            _uptime_notified.add(threshold)
            return message
    return None
