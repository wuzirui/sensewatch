# htpc-proxy — Isolated Chrome via SSH SOCKS5 Proxy

`htpc-proxy` launches an isolated Chrome browser that routes **all** traffic
through a remote SSH host. Your daily Chrome and other applications are
unaffected — only the proxy browser's traffic goes through the jump box.

Common use cases:
- Accessing internal web apps that are only reachable from the jump box
- Routing browser traffic through a different network egress (e.g. a home/office
  IP in another country)
- Using a Tailscale-connected machine as a browser-level VPN

## Prerequisites

- **Python 3.11+** (for `tomllib`)
- **SSH access** to a remote machine that can reach your target websites
- **Google Chrome** or Chromium (auto-detected on macOS, Linux, WSL)
- **`lsof`** (macOS/Linux — used to find the tunnel listener PID; pre-installed
  on macOS, `apt install lsof` on Linux if missing)

## Quick start

### 1. Configure the SSH jump host

Create `~/.config/dreamdojo/htpc-proxy.toml`:

```bash
mkdir -p ~/.config/dreamdojo
cp config.example.htpc-proxy.toml ~/.config/dreamdojo/htpc-proxy.toml
```

Edit it and set `host` to your SSH alias, Tailscale IP, or hostname:

```toml
[ssh]
host = "my-jump"   # ← change this
```

If you don't have an SSH alias yet, add one to `~/.ssh/config`:

```
Host my-jump
    HostName 100.x.y.z        # Tailscale IP or hostname
    User your-username
    IdentityFile ~/.ssh/id_ed25519
```

Verify SSH works:

```bash
ssh my-jump 'hostname && echo OK'
```

### 2. Install

```bash
pipx install "git+https://github.com/wuzirui/sensewatch.git@agent/acp-cli"
```

Or from a local checkout:

```bash
pip install -e .
```

### 3. Use

```bash
# One-shot: start tunnel, launch isolated Chrome, auto-cleanup on exit
htpc-proxy start

# Custom SOCKS5 port
htpc-proxy start --port 9999

# Tunnel only (no browser) — manually configure your own proxy client
htpc-proxy tunnel

# Check tunnel status
htpc-proxy status

# Stop the tunnel
htpc-proxy stop
```

## How it works

```
┌──────────────────┐     SSH -D <port>      ┌──────────────────┐
│   Your machine    │ ◄────────────────────► │   Jump host      │
│                   │   SOCKS5 tunnel        │  (Tailscale/SSH) │
│  ┌─────────────┐  │                        └──────────────────┘
│  │ Chrome       │──┼──► socks5://localhost:<port>
│  │ (isolated    │  │    → all traffic through jump host
│  │  profile)    │  │
│  └─────────────┘  │
│                   │
│  ┌─────────────┐  │
│  │ Daily        │──┼──► direct connection (unaffected)
│  │ Chrome       │  │
│  └─────────────┘  │
└──────────────────┘
```

- **SSH tunnel**: `ssh -D <port> -N` creates a SOCKS5 proxy on localhost. The
  `-o ControlPath=none` flag forces a dedicated connection (bypasses SSH
  multiplexing) so the tool owns the tunnel lifecycle.
- **Isolated Chrome**: `--proxy-server=socks5://localhost:<port>` routes all
  traffic through the proxy. `--user-data-dir=<isolated path>` keeps
  cookies, bookmarks, and extensions completely separate from your daily Chrome
  profile. Chrome's `socks5://` scheme resolves DNS through the proxy by
  default.
- **Lifecycle**: closing the Chrome window (or Ctrl+C in the terminal) tears
  down the tunnel automatically via `atexit` and signal handlers.

## Configuration reference

All keys in `~/.config/dreamdojo/htpc-proxy.toml` are optional:

| Key | Default | Description |
|-----|---------|-------------|
| `ssh.host` | `"my-jump"` | SSH host (alias, IP, or hostname) |
| `ssh.port` | `22` | SSH port |
| `proxy.socks_port` | `1080` | Local SOCKS5 port (auto fallback if occupied) |
| `chrome.path` | auto-detected | Path to Chrome binary |
| `chrome.user_data_dir` | `~/.cache/dreamdojo/htpc-proxy/chrome-profile` | Isolated profile directory |

## Troubleshooting

### ERR_PROXY_CONNECTION_FAILED

The Chrome proxy browser shows "Unable to connect to the proxy server."

- **Check the tunnel is alive**: `htpc-proxy status`
- **Restart the tunnel**: `htpc-proxy stop && htpc-proxy start`
- **Stale tunnel**: the SSH process may still be listening on the port but the
  underlying connection died. `htpc-proxy stop` kills by port listener, not
  just PID, so it reliably cleans up stale tunnels.

### ERR_TIMED_OUT on specific sites

The proxy is connected but a particular website doesn't load.

- **Test from the jump host directly**: `ssh <host> 'curl -sI --max-time 10 https://example.com'`
- **Test through the proxy**: `curl -sI --socks5-hostname localhost:<port> --max-time 10 https://example.com`
- If the site loads on the jump host but not through the proxy, restart the
  tunnel (`htpc-proxy stop && htpc-proxy start`).

### Chrome profile is locked

If a previous Chrome instance crashed, the profile lock file may be stale:

```bash
rm -f ~/.cache/dreamdojo/htpc-proxy/chrome-profile/SingletonLock
```

### "my-jump" SSH host not found

Edit `~/.config/dreamdojo/htpc-proxy.toml` and set `ssh.host` to your actual
SSH alias, or add the alias to `~/.ssh/config`.  A Tailscale IP (e.g.
`100.x.y.z`) also works directly.
