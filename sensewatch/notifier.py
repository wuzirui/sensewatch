"""macOS native notifications with personality subtitles and anti-spam."""

from __future__ import annotations

import time

from . import config, easter_eggs
from .state import JobState, JobTransition


def _send_notification(title: str, subtitle: str, body: str) -> None:
    """Send a macOS notification via PyObjC."""
    try:
        from Foundation import NSUserNotification, NSUserNotificationCenter
        notification = NSUserNotification.alloc().init()
        notification.setTitle_(title)
        notification.setSubtitle_(subtitle)
        notification.setInformativeText_(body)
        notification.setSoundName_("default")
        center = NSUserNotificationCenter.defaultUserNotificationCenter()
        center.deliverNotification_(notification)
    except Exception:
        pass  # Notification not critical


class Notifier:
    """Sends macOS notifications for job transitions with personality."""

    def __init__(self):
        self._last_notified: dict[str, float] = {}

    def on_job_transition(
        self, job_key: str, old_state: JobState | None, new_state: JobState
    ) -> None:
        now = time.time()
        last = self._last_notified.get(job_key, 0)
        if now - last < config.NOTIFICATION_COOLDOWN:
            return

        title, transition_type, should_notify = self._classify_transition(old_state, new_state)
        if not should_notify:
            return

        job_name = job_key.split("/", 1)[-1] if "/" in job_key else job_key
        subtitle = easter_eggs.notify_subtitle(transition_type)
        body = f"{job_name}: {old_state.value if old_state else '(new)'} \u2192 {new_state.value}"

        self._send(title, subtitle, body)
        self._last_notified[job_key] = now

    def on_connection_change(self, service: str, was_ok: bool, is_ok: bool) -> None:
        if was_ok and not is_ok:
            subtitle = easter_eggs.notify_subtitle("connection_lost")
            self._send("SenseWatch", subtitle, f"{service} is unreachable")
        elif not was_ok and is_ok:
            subtitle = easter_eggs.notify_subtitle("connection_restored")
            self._send("SenseWatch", subtitle, f"{service} is back online")

    def on_uptime_milestone(self, message: str) -> None:
        self._send("SenseWatch", message, "")

    def _classify_transition(
        self, old_state: JobState | None, new_state: JobState
    ) -> tuple[str, str, bool]:
        if new_state == JobState.RUNNING:
            return ("Job Running", "running", True)
        if new_state == JobState.SUCCEEDED:
            return ("Job Succeeded", "succeeded", True)
        if new_state == JobState.FAILED:
            return ("Job Failed", "failed", True)
        if new_state == JobState.STOPPED:
            return ("Job Stopped", "stopped", True)
        return ("", "", False)

    def _send(self, title: str, subtitle: str, body: str) -> None:
        _send_notification(title, subtitle, body)
