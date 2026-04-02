"""SenseWatch v1: PyObjC (menu bar icon) + pywebview (panel UI).

The NSStatusItem is created on the main thread BEFORE pywebview starts.
pywebview reuses the existing NSApplication singleton.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import objc
import webview
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSImage,
    NSStatusBar,
    NSVariableStatusItemLength,
)
from Foundation import NSObject

from . import config
from .api_client import SenseCoreAPIClient
from .auth import SenseCoreAuth
from .bridge import Bridge
from .icon import get_icon_path
from .log_viewer import LogViewer
from .notifier import Notifier
from .poller import Poller
from .state import StateStore

log = logging.getLogger("sensewatch")

PANEL_DIR = Path(__file__).parent / "panel"
PANEL_WIDTH = 360
PANEL_HEIGHT = 580

# prevent garbage collection
_pinned: list = []
_click_callback = None


class StatusBarDelegate(NSObject):
    def action_(self, sender):
        if _click_callback:
            _click_callback()


class SenseWatchApp:
    def __init__(self):
        self.start_time = time.time()
        self.auth = SenseCoreAuth()
        self.state = StateStore()
        self.client = SenseCoreAPIClient(self.auth)
        self.notifier = Notifier()
        self.poller = Poller(self.client, self.state, self.notifier, self)
        self.log_viewer = LogViewer(self.client)
        self.bridge = Bridge(self)

        self._window: webview.Window | None = None
        self._icon_path = get_icon_path()
        self._panel_visible = False

    def run(self):
        if not config.load_user_config():
            print(config.config_missing_message())
            return

        if not self.auth.load_credentials():
            print("SenseCore credentials not found in Keychain.")
            print("Run: security add-generic-password -s sensecore_access_key_id -a $USER -w 'YOUR_AK_ID'")
            return

        self._start_polling()

        # 1. Create NSApplication + set as menu bar app (MAIN THREAD, before webview)
        ns_app = NSApplication.sharedApplication()
        ns_app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

        # 2. Create the status bar icon (MAIN THREAD)
        self._create_status_item()

        # 3. Create pywebview window (hidden)
        self._window = webview.create_window(
            "SenseWatch",
            url=str(PANEL_DIR / "index.html"),
            js_api=self.bridge,
            width=PANEL_WIDTH,
            height=PANEL_HEIGHT,
            resizable=False,
            frameless=True,
            hidden=True,
            on_top=True,
        )

        # 4. Start pywebview (takes over main thread run loop)
        webview.start(debug=False)

    def _create_status_item(self):
        """Create NSStatusItem with icon. MUST be called on main thread."""
        global _click_callback
        _click_callback = self._toggle_panel

        delegate = StatusBarDelegate.alloc().init()

        status_bar = NSStatusBar.systemStatusBar()
        status_item = status_bar.statusItemWithLength_(NSVariableStatusItemLength)

        button = status_item.button()
        image = NSImage.alloc().initWithContentsOfFile_(self._icon_path)
        image.setTemplate_(True)
        image.setSize_((18, 18))
        button.setImage_(image)
        button.setTarget_(delegate)
        button.setAction_(objc.selector(delegate.action_, signature=b"v@:@"))

        _pinned.extend([status_item, delegate])

    def _toggle_panel(self):
        if not self._window:
            return
        if self._panel_visible:
            self._window.hide()
            self._panel_visible = False
        else:
            self._window.show()
            self._panel_visible = True

    def hide_panel(self):
        """Called from JS (Escape key) to hide the panel."""
        if self._window and self._panel_visible:
            self._window.hide()
            self._panel_visible = False

    def _start_polling(self):
        def _poll_loop(fn, interval, name):
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

    def mark_dirty(self):
        pass
