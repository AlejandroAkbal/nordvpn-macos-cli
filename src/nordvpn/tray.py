"""Native macOS menu bar app (PyObjC). Shows lock/unlock icon and Connect/Disconnect/Quit."""

from __future__ import annotations

import os
import subprocess
import sys
import threading

from Foundation import NSObject, NSTimer
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSMenuItem,
    NSMenu,
    NSStatusBar,
    NSVariableStatusItemLength,
)
from PyObjCTools import AppHelper

from . import openvpn
from . import utils


class NordVPNStatusApp(NSObject):
    def applicationDidFinishLaunching_(self, notification: object) -> None:
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory
        )

        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength
        )
        self.button = self.status_item.button()
        self.button.setTitle_("⏳")

        self.menu = NSMenu.alloc().init()

        self.status_info = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Checking...", None, ""
        )
        self.menu.addItem_(self.status_info)
        self.menu.addItem_(NSMenuItem.separatorItem())

        self.connect_us = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Connect (US)", "connectUS:", ""
        )
        self.menu.addItem_(self.connect_us)

        self.disconnect_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Disconnect", "disconnectVPN:", ""
        )
        self.menu.addItem_(self.disconnect_item)

        self.menu.addItem_(NSMenuItem.separatorItem())

        self.quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit Tray", "terminate:", "q"
        )
        self.quit_item.setTarget_(NSApplication.sharedApplication())
        self.menu.addItem_(self.quit_item)

        self.status_item.setMenu_(self.menu)

        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            5.0, self, "checkStatus:", None, True
        )
        self.checkStatus_(None)

    def checkStatus_(self, sender: object) -> None:
        threading.Thread(target=self._bg_check).start()

    def _bg_check(self) -> None:
        connected = openvpn.is_connected()
        AppHelper.callAfter(self._update_ui, connected)

    def _update_ui(self, connected: bool) -> None:
        if connected:
            self.button.setTitle_("🔒")
            self.status_info.setTitle_("VPN: Connected")
            self.connect_us.setHidden_(True)
            self.disconnect_item.setHidden_(False)
        else:
            self.button.setTitle_("🔓")
            self.status_info.setTitle_("VPN: Disconnected")
            self.connect_us.setHidden_(False)
            self.disconnect_item.setHidden_(True)

    def connectUS_(self, sender: object) -> None:
        self.button.setTitle_("⌛")
        self._run_admin("nordvpn connect US --daemon")

    def disconnectVPN_(self, sender: object) -> None:
        self.button.setTitle_("⌛")
        self._run_admin("nordvpn disconnect")

    def _run_admin(self, cmd_str: str) -> None:
        try:
            nord_bin = utils.resolve_binary("nordvpn")
        except RuntimeError:
            candidate = os.path.join(os.path.dirname(sys.executable), "nordvpn")
            nord_bin = candidate if os.path.exists(candidate) else None
        if nord_bin:
            cmd_str = cmd_str.replace("nordvpn", f"'{nord_bin}'", 1)

        script = f'do shell script "{cmd_str}" with administrator privileges'

        def _bg() -> None:
            subprocess.run(["/usr/bin/osascript", "-e", script])
            AppHelper.callAfter(self.checkStatus_, None)

        threading.Thread(target=_bg).start()


def run() -> None:
    app = NSApplication.sharedApplication()
    delegate = NordVPNStatusApp.alloc().init()
    app.setDelegate_(delegate)
    AppHelper.runEventLoop()


if __name__ == "__main__":
    run()
