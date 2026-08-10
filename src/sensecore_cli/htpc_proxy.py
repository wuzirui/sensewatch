#!/usr/bin/env python3
"""Launch an isolated Chrome browser that routes all traffic through a remote
host via SSH SOCKS5 dynamic forwarding.

Usage:
    htpc-proxy start              # one-shot: tunnel + Chrome, cleanup on exit
    htpc-proxy start --port 9999  # custom SOCKS5 port
    htpc-proxy tunnel             # only start the tunnel (manual browser setup)
    htpc-proxy status             # show tunnel status
    htpc-proxy stop               # kill the tunnel

Design: SSH dynamic forwarding (-D) creates a local SOCKS5 proxy; Chrome runs
with an isolated user-data-dir so it coexists with your daily Chrome. When the
browser exits, the tunnel is torn down automatically.

Setup: see docs/htpc-proxy.md.  You MUST configure the remote SSH host before
first use — either via ~/.config/dreamdojo/htpc-proxy.toml or by setting up an
SSH Host alias in ~/.ssh/config.
"""

from __future__ import annotations

import argparse
import atexit
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import tomllib
from pathlib import Path

# ---------------------------------------------------------------------------
# Defaults — override via ~/.config/dreamdojo/htpc-proxy.toml
# ---------------------------------------------------------------------------

CONFIG_DIR = Path.home() / ".config" / "dreamdojo"
CONFIG_PATH = CONFIG_DIR / "htpc-proxy.toml"
CACHE_DIR = Path.home() / ".cache" / "dreamdojo" / "htpc-proxy"
PID_FILE = CACHE_DIR / "tunnel.pid"
PORT_FILE = CACHE_DIR / "tunnel.port"
CHROME_DATA_DIR = CACHE_DIR / "chrome-profile"

# Placeholder host — replace with your SSH alias, Tailscale IP, or hostname.
# Example SSH config (~/.ssh/config):
#   Host my-jump
#       HostName 100.x.y.z       # Tailscale IP of the remote machine
#       User your-username
#       IdentityFile ~/.ssh/id_ed25519
DEFAULT_SSH_HOST = "my-jump"

DEFAULT_SSH_PORT = 22
DEFAULT_SOCKS_PORT = 1080
DEFAULT_CHROME = "google-chrome-stable"

_tunnel_proc: subprocess.Popen | None = None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_config() -> dict:
    """Load TOML config, returning defaults for missing keys."""
    cfg: dict = {
        "ssh": {"host": DEFAULT_SSH_HOST, "port": DEFAULT_SSH_PORT},
        "proxy": {"socks_port": DEFAULT_SOCKS_PORT},
        "chrome": {"path": DEFAULT_CHROME, "user_data_dir": str(CHROME_DATA_DIR)},
    }
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            toml = tomllib.loads(f.read())
        for section in ("ssh", "proxy", "chrome"):
            if section in toml:
                cfg[section].update(toml[section])
    return cfg


def _ssh_host(cfg: dict) -> str:
    host = cfg["ssh"]["host"]
    port = cfg["ssh"]["port"]
    return host if port == 22 else f"{host}:{port}"


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _find_chrome(cfg: dict) -> str:
    """Resolve chrome binary path from config or auto-detect."""
    explicit = cfg["chrome"].get("path", "")
    if explicit and shutil.which(explicit):
        return explicit

    # macOS
    macos_paths = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]
    for p in macos_paths:
        if os.path.exists(p):
            return p

    # Linux
    linux_names = [
        "google-chrome-stable",
        "google-chrome",
        "chromium",
        "chromium-browser",
    ]
    for name in linux_names:
        found = shutil.which(name)
        if found:
            return found

    # Windows (WSL / Git Bash)
    win_paths = [
        "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    ]
    for p in win_paths:
        if os.path.exists(p):
            return p

    return "google-chrome-stable"  # fallback — let subprocess report the error


def _pick_port(preferred: int) -> int:
    """Return *preferred* if free, otherwise the first free port >= 1080."""
    if _port_is_free(preferred):
        return preferred
    for offset in range(20):
        candidate = 1080 + offset
        if _port_is_free(candidate):
            return candidate
    raise RuntimeError("No free port found in range 1080–1099")


def _port_is_free(port: int) -> bool:
    """True if nothing is listening on 127.0.0.1:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", port))
            return False  # connected → port is in use
        except (ConnectionRefusedError, socket.timeout, OSError):
            return True


def _port_listener_pid(port: int) -> int | None:
    """Return PID of the process listening on 127.0.0.1:port, or None."""
    try:
        out = subprocess.check_output(
            ["lsof", "-ti", f"TCP@127.0.0.1:{port}"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return int(out.splitlines()[0]) if out else None
    except (subprocess.CalledProcessError, ValueError):
        return None


def _save_pid_port(pid: int, port: int) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(pid))
    PORT_FILE.write_text(str(port))


def _read_pid_port() -> tuple[int | None, int | None]:
    pid = int(PID_FILE.read_text().strip()) if PID_FILE.exists() else None
    port = int(PORT_FILE.read_text().strip()) if PORT_FILE.exists() else None
    return pid, port


def _clear_pid_port() -> None:
    for f in (PID_FILE, PORT_FILE):
        if f.exists():
            f.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Tunnel
# ---------------------------------------------------------------------------


def _tunnel_is_alive() -> bool:
    """A tunnel is alive iff our port file says a port and something listens."""
    _, port = _read_pid_port()
    return port is not None and not _port_is_free(port)


def tunnel_start(cfg: dict, port: int | None = None) -> int:
    """Start SSH SOCKS5 tunnel. Returns the port number."""
    global _tunnel_proc

    socks_port = port or cfg["proxy"]["socks_port"]
    socks_port = _pick_port(socks_port)
    host = _ssh_host(cfg)

    # Force a dedicated connection — don't multiplex with an existing master.
    # If we re-use the master, our subprocess exits immediately after
    # registering the forward, making PID-based lifecycle management
    # impossible.
    cmd = [
        "ssh",
        "-D",
        str(socks_port),
        "-N",
        "-C",
        "-q",
        "-o",
        "ControlPath=none",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "TCPKeepAlive=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        host,
    ]

    print(f"→ SSH tunnel: localhost:{socks_port} → {host}")
    _tunnel_proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    atexit.register(_cleanup_tunnel)

    # Wait for the port to come alive (up to 5 s for slow auth)
    for _ in range(10):
        time.sleep(0.5)
        if _tunnel_proc.poll() is not None:
            stderr = (
                _tunnel_proc.stderr.read().decode(errors="replace")
                if _tunnel_proc.stderr
                else ""
            )
            raise RuntimeError(
                f"SSH tunnel failed to start (exit {_tunnel_proc.returncode}): {stderr}"
            )
        if not _port_is_free(socks_port):
            break
    else:
        _tunnel_proc.terminate()
        _tunnel_proc.wait()
        raise RuntimeError(
            f"SSH tunnel started but port {socks_port} never came alive"
        )

    _save_pid_port(_tunnel_proc.pid, socks_port)
    print(f"✓ Tunnel ready (pid={_tunnel_proc.pid}, port={socks_port})")
    return socks_port


def tunnel_status() -> bool:
    """Print tunnel status. Returns True if running."""
    _pid, port = _read_pid_port()

    if port is None:
        print("Tunnel: not running (no state file)")
        return False

    port_alive = not _port_is_free(port)
    if port_alive:
        listener = _port_listener_pid(port)
        print(f"Tunnel: running (port={port}, listener_pid={listener})")
        print(f"  Proxy: socks5://localhost:{port}")
    else:
        print(f"Tunnel: dead (port {port} not listening, cleaning up)")
        _clear_pid_port()
    return port_alive


def tunnel_stop() -> None:
    """Kill the tunnel by port, PID, and process handle — whatever sticks."""
    global _tunnel_proc
    pid, port = _read_pid_port()

    # 1. Kill by port listener
    if port is not None and not _port_is_free(port):
        listener = _port_listener_pid(port)
        if listener is not None:
            _kill_pid(listener)
            for _ in range(10):
                time.sleep(0.3)
                if _port_is_free(port):
                    break

    # 2. Kill tracked process
    if _tunnel_proc is not None:
        if _tunnel_proc.poll() is None:
            _tunnel_proc.terminate()
            try:
                _tunnel_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _tunnel_proc.kill()
                _tunnel_proc.wait()
        _tunnel_proc = None

    # 3. Kill by saved PID (last resort)
    if pid is not None:
        _kill_pid(pid)

    _clear_pid_port()
    print("✓ Tunnel stopped")


def _cleanup_tunnel() -> None:
    """atexit handler: ensure tunnel is torn down."""
    global _tunnel_proc
    # Kill by port first (most reliable)
    _, port = _read_pid_port()
    if port is not None and not _port_is_free(port):
        listener = _port_listener_pid(port)
        if listener is not None:
            _kill_pid(listener)
    # Then kill tracked process
    if _tunnel_proc is not None and _tunnel_proc.poll() is None:
        _tunnel_proc.terminate()
        try:
            _tunnel_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _tunnel_proc.kill()
        _tunnel_proc = None
    _clear_pid_port()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Chrome
# ---------------------------------------------------------------------------


def chrome_start(cfg: dict, socks_port: int) -> subprocess.Popen:
    """Launch isolated Chrome pointed at the SOCKS5 proxy."""
    chrome_bin = _find_chrome(cfg)
    user_data_dir = os.path.expanduser(
        cfg["chrome"].get("user_data_dir", str(CHROME_DATA_DIR))
    )

    cmd = [
        chrome_bin,
        f"--proxy-server=socks5://localhost:{socks_port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-default-apps",
    ]

    print(f"→ Chrome: {chrome_bin}")
    print(f"  Profile: {user_data_dir}")
    print(f"  Proxy:   socks5://localhost:{socks_port}")
    print("  (close the browser window to stop the tunnel)")

    # Detach Chrome so it runs independently of this script's stdin
    return subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_start(args: argparse.Namespace, cfg: dict) -> None:
    """One-shot: start tunnel, launch Chrome, wait, cleanup."""
    if _tunnel_is_alive():
        _, existing_port = _read_pid_port()
        print(f"Tunnel already running on port {existing_port}")
        print("Use 'htpc-proxy stop' first, or reuse with Chrome:")
        print(
            f"  google-chrome-stable --proxy-server=socks5://localhost:{existing_port}"
            f" --user-data-dir={CHROME_DATA_DIR}"
        )
        sys.exit(1)

    port_arg = args.port if hasattr(args, "port") and args.port else None
    socks_port = tunnel_start(cfg, port=port_arg)

    chrome = chrome_start(cfg, socks_port)

    print(
        "\n✓ Browser launched. Close the Chrome window to stop the tunnel,"
        " or Ctrl+C here."
    )
    try:
        chrome.wait()
    except KeyboardInterrupt:
        pass
    finally:
        print("\n→ Cleaning up...")
        if chrome.poll() is None:
            chrome.terminate()
            try:
                chrome.wait(timeout=5)
            except subprocess.TimeoutExpired:
                chrome.kill()
        tunnel_stop()


def cmd_tunnel(args: argparse.Namespace, cfg: dict) -> None:
    """Start tunnel only; stay in foreground until Ctrl+C."""
    if _tunnel_is_alive():
        _, existing_port = _read_pid_port()
        print(f"Tunnel already running on port {existing_port}")
        sys.exit(1)

    port_arg = args.port if hasattr(args, "port") and args.port else None
    tunnel_start(cfg, port=port_arg)

    print("\nTunnel running. Press Ctrl+C to stop.")
    try:
        while _tunnel_proc is not None and _tunnel_proc.poll() is None:
            time.sleep(1)
    except KeyboardInterrupt:
        print()
    finally:
        tunnel_stop()


def cmd_status(args: argparse.Namespace, cfg: dict) -> None:
    tunnel_status()


def cmd_stop(args: argparse.Namespace, cfg: dict) -> None:
    tunnel_stop()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Launch an isolated Chrome browser through SSH SOCKS5 proxy.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="Start tunnel + Chrome (one-shot)")
    p_start.add_argument(
        "--port", type=int, help="SOCKS5 port (default from config)"
    )

    p_tunnel = sub.add_parser("tunnel", help="Start tunnel only (foreground)")
    p_tunnel.add_argument(
        "--port", type=int, help="SOCKS5 port (default from config)"
    )

    sub.add_parser("status", help="Show tunnel status")
    sub.add_parser("stop", help="Stop the tunnel")

    args = parser.parse_args()
    cfg = load_config()

    commands = {
        "start": cmd_start,
        "tunnel": cmd_tunnel,
        "status": cmd_status,
        "stop": cmd_stop,
    }
    commands[args.command](args, cfg)


if __name__ == "__main__":
    main()
