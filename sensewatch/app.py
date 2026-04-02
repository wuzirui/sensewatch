"""SenseWatch: main rumps.App subclass — menu bar app with timer orchestration.

CRITICAL THREADING RULE:
- All API calls / subprocess calls run in background daemon threads.
- All NSMenu / rumps.Menu manipulation runs ONLY on the main thread.
- Pollers set self._menu_dirty = True; a 2-second main-thread timer rebuilds.
- NEVER call self.menu.clear() or self.menu.add() from a background thread.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

import rumps

from . import config, easter_eggs
from .api_client import SenseCoreAPIClient
from .auth import SenseCoreAuth
from .icon import get_icon_path
from .log_viewer import LogViewer
from .menu_builder import build_menu
from .notifier import Notifier
from .poller import Poller
from .state import JobSnapshot, StateStore

log = logging.getLogger("sensewatch")


class SenseWatchApp(rumps.App):
    def __init__(self):
        icon_path = get_icon_path()
        super().__init__(
            name="SenseWatch",
            icon=icon_path,
            template=True,
            quit_button=None,
        )

        self.start_time = time.time()
        self.auth = SenseCoreAuth()
        self.state = StateStore()
        self.client = SenseCoreAPIClient(self.auth)
        self.notifier = Notifier()
        self.poller = Poller(self.client, self.state, self.notifier, self)
        self.log_viewer = LogViewer(self.client)

        self._timers: list[rumps.Timer] = []
        self._menu_dirty = True  # Flag: pollers set this, main thread reads it

    # ── Lifecycle ─────────────────────────────────────────────────────────

    @rumps.events.before_start
    def _setup(self):
        if not self.auth.load_credentials():
            self._show_setup_dialog()
            return

        self._build_menu_now()  # Initial "Loading..." menu
        self._start_polling()

    def _show_setup_dialog(self):
        msg = (
            "SenseCore credentials not found in Keychain.\n\n"
            "Run these commands in Terminal:\n\n"
            "  security add-generic-password \\\n"
            "    -s sensecore_access_key_id -a $USER \\\n"
            "    -w 'YOUR_ACCESS_KEY_ID'\n\n"
            "  security add-generic-password \\\n"
            "    -s sensecore_access_key_secret -a $USER \\\n"
            "    -w 'YOUR_ACCESS_KEY_SECRET'\n\n"
            "Then restart SenseWatch."
        )
        response = rumps.alert(
            title="SenseWatch Setup",
            message=msg,
            ok="Retry",
            cancel="Quit",
        )
        if response == 1:
            if self.auth.load_credentials():
                self._start_polling()
            else:
                rumps.alert("Still no credentials found.", ok="Quit")
                rumps.quit_application()
        else:
            rumps.quit_application()

    def _start_polling(self):
        """Start polling. Background threads do I/O; main-thread timer rebuilds menu."""

        # ── Main-thread timer: checks dirty flag and rebuilds menu ──
        # This is the ONLY place that touches self.menu
        t_menu = rumps.Timer(self._maybe_rebuild_menu, 2)
        t_menu.start()
        self._timers.append(t_menu)

        # ── Background polling threads (never touch menu) ──
        def _poll_loop(fn, interval, name):
            """Run fn() every `interval` seconds in a background thread."""
            while True:
                try:
                    fn()
                except Exception:
                    log.exception("Poll error in %s", name)
                time.sleep(interval)

        for fn, interval, name in [
            (self.poller.poll_health, config.POLL_INTERVAL_HEALTH, "health"),
            (self.poller.poll_jobs, config.POLL_INTERVAL_JOBS, "jobs"),
            (self.poller.poll_cci, config.POLL_INTERVAL_CCI, "cci"),
            (self.poller.poll_gpu, config.POLL_INTERVAL_GPU, "gpu"),
            (self.poller.poll_log_previews, config.POLL_INTERVAL_JOBS, "logs"),
        ]:
            t = threading.Thread(
                target=_poll_loop, args=(fn, interval, name), daemon=True
            )
            t.start()

    # ── Menu (MAIN THREAD ONLY) ───────────────────────────────────────────

    def mark_dirty(self):
        """Called by pollers (from any thread) to request a menu rebuild."""
        self._menu_dirty = True

    def _maybe_rebuild_menu(self, _timer=None):
        """Called every 2s on the main thread. Rebuilds menu and shows queued alerts."""
        # Show any pending alert (must be on main thread for NSWindow)
        if self._pending_alert is not None:
            title, message = self._pending_alert
            self._pending_alert = None
            rumps.alert(title=title, message=message, ok="Close")

        if not self._menu_dirty:
            return
        self._menu_dirty = False
        self._build_menu_now()

    def _build_menu_now(self):
        """Rebuild the menu. MUST be called on the main thread only."""
        my_jobs = self.state.my_jobs()
        running_count = sum(
            1 for j in my_jobs
            if j.state.value in config.ACTIVE_JOB_STATES
        )
        total_gpus = sum(
            s.total_gpus for s in self.state.gpu_availability.values()
        )
        start_str = datetime.fromtimestamp(self.start_time).strftime("%H:%M")
        connected = self.state.health.aec2_ok

        flavor = easter_eggs.flavor_text(
            running_jobs=running_count,
            gpu_total=total_gpus,
            start_time=start_str,
            connected=connected,
        )

        items = build_menu(self.state, flavor_text=flavor, app_ref=self)

        self.menu.clear()
        for item in items:
            self.menu.add(item)

        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("Quit SenseWatch", callback=self._quit))

    def _on_refresh(self, _):
        """Manual refresh — kick all pollers in background."""
        def do_refresh():
            threads = [
                threading.Thread(target=self.poller.poll_health, daemon=True),
                threading.Thread(target=self.poller.poll_jobs, daemon=True),
                threading.Thread(target=self.poller.poll_cci, daemon=True),
                threading.Thread(target=self.poller.poll_gpu, daemon=True),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        threading.Thread(target=do_refresh, daemon=True).start()

    def show_job_logs(self, job: JobSnapshot):
        """Fetch logs in background, queue alert for main thread."""
        def fetch_logs():
            if job.is_terminal:
                log_text = self.log_viewer.fetch_offline_logs(job)
            else:
                log_text = self.log_viewer.fetch_live_logs(job)

            display_text = log_text
            if len(display_text) > 5000:
                display_text = "...(truncated)\n" + display_text[-5000:]

            try:
                import subprocess
                subprocess.run(["pbcopy"], input=log_text.encode(), check=True)
                clipboard_msg = "(Full log copied to clipboard)\n\n"
            except Exception:
                clipboard_msg = ""

            # Queue the alert for the main thread via _pending_alert
            self._pending_alert = (
                f"Logs: {job.display_name or job.name}",
                f"{clipboard_msg}{display_text}",
            )

        threading.Thread(target=fetch_logs, daemon=True).start()

    _pending_alert: tuple[str, str] | None = None

    def cci_start(self, app_snap):
        """Start a CCI container in background."""
        def do_start():
            name = app_snap.display_name or app_snap.name
            try:
                self.client.start_cci_app(app_snap.workspace, app_snap.name)
                self.notifier._send("CCI Start", f"Starting {name}...", "")
                self.poller.poll_cci()
            except Exception as e:
                self.notifier._send("CCI Start Failed", name, str(e))

        threading.Thread(target=do_start, daemon=True).start()

    def cci_stop(self, app_snap):
        """Stop a CCI container in background."""
        def do_stop():
            name = app_snap.display_name or app_snap.name
            try:
                self.client.stop_cci_app(app_snap.workspace, app_snap.name)
                self.notifier._send("CCI Stop", f"Stopping {name}...", "")
                self.poller.poll_cci()
            except Exception as e:
                self.notifier._send("CCI Stop Failed", name, str(e))

        threading.Thread(target=do_stop, daemon=True).start()

    def _quit(self, _):
        rumps.quit_application()
