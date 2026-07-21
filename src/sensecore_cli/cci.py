#!/usr/bin/env python3
"""Agent-friendly CCI CLI — list, status, start, stop, restart, doctor for CCI apps.

Usage:
    cci list                              # my apps across linked workspaces
    cci list --state RUNNING --json
    cci status zirui-cpu                  # verbose one-app view
    cci restart zirui-cpu                 # smart: start if SUSPENDED, else stop+wait+start
    cci start zirui-cpu
    cci stop zirui-cpu
    cci doctor zirui-cpu                  # restart if down; verify DNAT lookup; poll ssh
    cci doctor --all                      # check every app in [dnat.port_template]

DNAT bindings are read-only here. The sco DNAT write APIs are unreliable, so the
doctor no longer creates, deletes, or GCs DNAT rules. If the lookup finds no
rule pointing at the app's uid, doctor prints a rebind alert so the user can
recreate the rule in the SenseCore console.

Reuses: packaged hmac_request.py (HMAC),
        ~/.cache/dreamdojo/acp/workspaces.json (workspace catalog),
        ~/.config/dreamdojo/acp.toml [identity] / [dnat] blocks.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


HMAC_HELPER = Path(__file__).resolve().with_name("hmac_request.py")
CCI_BASE = "https://cci.cn-sh-01.sensecore.cn"
SUBSCRIPTION = "0197ee17-b6eb-7846-b2b4-a77c5f509b92"
RG = "default"
WS_ZONE = "cn-sh-01z"

ACP_CACHE_DIR = Path.home() / ".cache" / "dreamdojo" / "acp"
ACP_CONFIG_PATH = Path.home() / ".config" / "dreamdojo" / "acp.toml"

KNOWN_WORKSPACES = ["share-space-01e", "p1-video-world-model-for-robot-learning"]
ACTIVE_STATES = {"RUNNING", "STARTING", "PROGRESSING", "INIT", "PENDING", "CREATING"}
INACTIVE_STATES = {"SUSPENDED", "STOPPED", "FAILED"}

SCO_BIN = os.environ.get("SCO_BIN", str(Path.home() / ".sco" / "bin" / "sco"))
SSH_USER = "root"
SSH_INTERNAL_PORT = "22"


# =============================================================================
# Identity (read from acp.toml; --user/--all-users override at CLI)
# =============================================================================


def load_identity() -> dict[str, str]:
    if not ACP_CONFIG_PATH.exists():
        return {"user_name": "", "user_id": ""}
    with open(ACP_CONFIG_PATH, "rb") as fh:
        cfg = tomllib.load(fh)
    ident = cfg.get("identity", {}) or {}
    return {"user_name": ident.get("user_name", ""), "user_id": ident.get("user_id", "")}


def load_dnat_config() -> dict[str, Any]:
    """Read [dnat] block from acp.toml. Returns {} if missing."""
    if not ACP_CONFIG_PATH.exists():
        return {}
    with open(ACP_CONFIG_PATH, "rb") as fh:
        cfg = tomllib.load(fh)
    return cfg.get("dnat", {}) or {}


# =============================================================================
# HMAC client
# =============================================================================


class HMACError(Exception):
    pass


def hmac_call(method: str, path: str, body: dict[str, Any] | None = None, timeout: int = 30) -> dict[str, Any]:
    cmd = [
        sys.executable, str(HMAC_HELPER),
        "--service-base", CCI_BASE,
        "--path", path,
        "--method", method,
        "--timeout", str(timeout),
    ]
    if body is not None:
        cmd += ["--data", json.dumps(body)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 10)
    if r.returncode != 0:
        raise HMACError(f"{method} {path} failed (rc={r.returncode}): {r.stderr.strip() or r.stdout.strip()}")
    lines = r.stdout.splitlines()
    if lines and lines[0].startswith("STATUS "):
        lines = lines[1:]
    blob = "\n".join(lines).strip()
    return json.loads(blob) if blob else {}


# =============================================================================
# Workspace discovery
# =============================================================================


def linked_workspaces() -> list[str]:
    """Read workspace names from the acp.py cache; fall back to KNOWN_WORKSPACES."""
    cache = ACP_CACHE_DIR / "workspaces.json"
    if cache.exists():
        try:
            with open(cache) as fh:
                entries = json.load(fh)
            names = [e["name"] for e in entries if isinstance(e, dict) and e.get("name")]
            if names:
                return names
        except Exception:
            pass
    return list(KNOWN_WORKSPACES)


# =============================================================================
# App listing
# =============================================================================


def workspace_apps_path(ws: str, page_token: str = "") -> str:
    base = f"/compute/cci/data/v2/subscriptions/{SUBSCRIPTION}/resourceGroups/{RG}/zones/{WS_ZONE}/workspaces/{ws}/apps?page_size=200"
    if page_token:
        base += f"&page_token={page_token}"
    return base


def app_path(ws: str, name: str, action: str = "") -> str:
    base = f"/compute/cci/data/v2/subscriptions/{SUBSCRIPTION}/resourceGroups/{RG}/zones/{WS_ZONE}/workspaces/{ws}/apps/{name}"
    if action:
        base += f":{action}"
    return base


def list_workspace_apps(ws: str) -> list[dict[str, Any]]:
    apps: list[dict[str, Any]] = []
    token = ""
    while True:
        data = hmac_call("GET", workspace_apps_path(ws, token))
        for a in data.get("apps", []):
            a["_workspace"] = ws
            apps.append(a)
        token = data.get("next_page_token", "")
        if not token:
            break
    return apps


def list_all_apps(workspaces: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(5, len(workspaces) or 1)) as pool:
        for chunk in pool.map(_safe_list, workspaces):
            out.extend(chunk)
    return out


def _safe_list(ws: str) -> list[dict[str, Any]]:
    try:
        return list_workspace_apps(ws)
    except Exception as e:
        print(f"[cci] warning: list {ws} failed: {e}", file=sys.stderr)
        return []


# =============================================================================
# Filters
# =============================================================================


def filter_apps(
    apps: list[dict[str, Any]],
    user_name: str = "",
    user_id: str = "",
    states: list[str] | None = None,
) -> list[dict[str, Any]]:
    out = apps
    if user_name or user_id:
        def own_match(a: dict[str, Any]) -> bool:
            o = a.get("ownership", {}) or {}
            if user_name and o.get("user_name") == user_name:
                return True
            if user_id and o.get("user_id") == user_id:
                return True
            return False
        out = [a for a in out if own_match(a)]
    if states:
        sset = {s.upper() for s in states}
        out = [a for a in out if (a.get("state") or "").upper() in sset]
    return out


def find_one(apps: list[dict[str, Any]], key: str) -> dict[str, Any]:
    """Match by exact name first, then by display_name. Multiple display matches → exit 2."""
    name_hits = [a for a in apps if a.get("name") == key]
    if len(name_hits) == 1:
        return name_hits[0]
    disp_hits = [a for a in apps if a.get("display_name") == key]
    if len(disp_hits) == 1:
        return disp_hits[0]
    if not name_hits and not disp_hits:
        print(f"[cci] no app matches {key!r} (after filters)", file=sys.stderr)
        sys.exit(2)
    cands = name_hits or disp_hits
    print(f"[cci] {len(cands)} apps match {key!r} — disambiguate by name:", file=sys.stderr)
    for a in cands:
        print(f"  name={a['name']}  display={a['display_name']}  ws={a['_workspace']}  state={a['state']}", file=sys.stderr)
    sys.exit(2)


# =============================================================================
# Formatting
# =============================================================================


def fmt_age(iso_ts: str) -> str:
    try:
        ts = iso_ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
    except Exception:
        return "?"
    delta = (datetime.now(timezone.utc) - dt).total_seconds()
    if delta < 60:
        return f"{int(delta)}s"
    if delta < 3600:
        return f"{int(delta // 60)}m"
    if delta < 86400:
        return f"{int(delta // 3600)}h"
    return f"{int(delta // 86400)}d"


def print_table(apps: list[dict[str, Any]]) -> None:
    if not apps:
        print("(no apps; use --all-users to see other owners, or --workspace WS to broaden)")
        return
    rows = []
    for a in apps:
        rows.append((
            a.get("name", ""),
            (a.get("display_name") or "")[:28],
            a.get("state", ""),
            (a.get("resource_pool", {}) or {}).get("name", ""),
            (a.get("template", {}) or {}).get("resource_spec", {}).get("name", ""),
            f"{a.get('ready_replicas', 0)}/{a.get('replicas', 0)}",
            fmt_age(a.get("update_time") or a.get("create_time") or ""),
        ))
    widths = [max(len(str(r[i])) for r in rows + [("NAME", "DISPLAY", "STATE", "CLUSTER", "SPEC", "RDY/REP", "AGE")]) for i in range(7)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format("NAME", "DISPLAY", "STATE", "CLUSTER", "SPEC", "RDY/REP", "AGE"))
    for r in rows:
        print(fmt.format(*[str(x) for x in r]))


def print_status(a: dict[str, Any]) -> None:
    rp = a.get("resource_pool", {}) or {}
    spec = (a.get("template", {}) or {}).get("resource_spec", {})
    own = a.get("ownership", {}) or {}
    print(f"{a['name']}")
    print(f"  display     {a.get('display_name','')}")
    print(f"  workspace   {a['_workspace']}")
    print(f"  state       {a.get('state','')} (replicas {a.get('ready_replicas',0)}/{a.get('replicas',0)})")
    print(f"  owner       {own.get('user_name','?')} ({own.get('user_id','?')[:8]})")
    print(f"  cluster     {rp.get('name','?')} (zone {rp.get('available_zone','?')})")
    print(f"  spec        {spec.get('name','?')}  {spec.get('description','')}")
    print(f"  created     {a.get('create_time','?')}  ({fmt_age(a.get('create_time',''))} ago)")
    print(f"  updated     {a.get('update_time','?')}  ({fmt_age(a.get('update_time',''))} ago)")
    containers = (a.get("template", {}) or {}).get("containers", [])
    if containers:
        c = containers[0]
        rr = c.get("resource_request", {}) or {}
        print(f"  container   image={c.get('image_path','?')}")
        print(f"              cpu={rr.get('cpu','?')}  mem={rr.get('memory','?')}  gpu={rr.get('nvidia.com/gpu') or rr.get('nvidia.com/mig-3g.40gb') or '0'}")


# =============================================================================
# Lifecycle ops
# =============================================================================


def get_app(ws: str, name: str) -> dict[str, Any]:
    out = hmac_call("GET", app_path(ws, name))
    out["_workspace"] = ws
    return out


def do_start(a: dict[str, Any]) -> None:
    print(f"[cci] starting {a['name']} ({a.get('display_name','')}) in {a['_workspace']} ...", file=sys.stderr)
    hmac_call("POST", app_path(a["_workspace"], a["name"], "start"))
    wait_until(a, target=ACTIVE_STATES, timeout=60, label="start")


def do_stop(a: dict[str, Any]) -> None:
    print(f"[cci] stopping {a['name']} ({a.get('display_name','')}) in {a['_workspace']} ...", file=sys.stderr)
    hmac_call("POST", app_path(a["_workspace"], a["name"], "stop"))
    wait_until(a, target={"SUSPENDED", "STOPPED"}, timeout=60, label="stop")


def wait_until(a: dict[str, Any], target: set[str], timeout: int, label: str) -> dict[str, Any]:
    start = time.time()
    last = a
    while time.time() - start < timeout:
        time.sleep(2)
        try:
            last = get_app(a["_workspace"], a["name"])
        except Exception:
            continue
        s = (last.get("state") or "").upper()
        if s in target:
            print(f"[cci] {label} ok — state={s} ready={last.get('ready_replicas',0)}/{last.get('replicas',0)}", file=sys.stderr)
            return last
    s = (last.get("state") or "").upper()
    print(f"[cci] {label} not in {target} after {timeout}s — current state={s}", file=sys.stderr)
    return last


# =============================================================================
# Subcommands
# =============================================================================


def add_common_filters(p: argparse.ArgumentParser) -> None:
    p.add_argument("--user", help="Filter by user_name (e.g. L202500193). Default: identity from acp.toml.")
    p.add_argument("--all-users", action="store_true", help="Disable user filter.")
    p.add_argument("--workspace", help="Restrict to one workspace. Default: all linked workspaces.")


def cmd_list(args: argparse.Namespace) -> int:
    workspaces = [args.workspace] if args.workspace else linked_workspaces()
    apps = list_all_apps(workspaces)
    user_name, user_id = resolve_user(args)
    states = [s.strip() for s in args.state.split(",")] if args.state else None
    apps = filter_apps(apps, user_name=user_name, user_id=user_id, states=states)
    apps.sort(key=lambda a: a.get("update_time") or "", reverse=True)
    if args.json:
        print(json.dumps(apps, ensure_ascii=False, indent=2))
    else:
        print_table(apps)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    a = locate(args)
    print_status(a)
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    a = locate(args)
    state = (a.get("state") or "").upper()
    if state in ACTIVE_STATES:
        print(f"[cci] {a['name']} already in state={state}; nothing to do", file=sys.stderr)
        return 0
    do_start(a)
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    a = locate(args)
    state = (a.get("state") or "").upper()
    if state in {"SUSPENDED", "STOPPED"}:
        print(f"[cci] {a['name']} already in state={state}; nothing to do", file=sys.stderr)
        return 0
    do_stop(a)
    return 0


def cmd_restart(args: argparse.Namespace) -> int:
    a = locate(args)
    state = (a.get("state") or "").upper()
    print(f"[cci] {a['name']} current state={state}", file=sys.stderr)
    if state in {"SUSPENDED", "STOPPED", "FAILED"}:
        do_start(a)
    elif state == "RUNNING":
        do_stop(a)
        # Re-fetch after stop to get the post-stop state
        a = get_app(a["_workspace"], a["name"])
        do_start(a)
    elif state in {"STARTING", "PROGRESSING", "INIT", "PENDING", "CREATING"}:
        if not args.force:
            print(f"[cci] state={state} is transitional — refusing to restart. Pass --force to override.", file=sys.stderr)
            return 1
        do_stop(a)
        a = get_app(a["_workspace"], a["name"])
        do_start(a)
    else:
        print(f"[cci] unknown state {state} — refusing to restart. Pass --force to override.", file=sys.stderr)
        return 1
    return 0


def resolve_user(args: argparse.Namespace) -> tuple[str, str]:
    if args.all_users:
        return "", ""
    if args.user:
        return args.user, ""
    ident = load_identity()
    return ident["user_name"], ident["user_id"]


def locate(args: argparse.Namespace) -> dict[str, Any]:
    """Find one app by `key` honoring --workspace/--user/--all-users filters."""
    workspaces = [args.workspace] if args.workspace else linked_workspaces()
    apps = list_all_apps(workspaces)
    user_name, user_id = resolve_user(args)
    apps = filter_apps(apps, user_name=user_name, user_id=user_id)
    return find_one(apps, args.name)


# =============================================================================
# Doctor — keeps `ssh htc` / `ssh htg` working by repairing CCI + DNAT state.
# =============================================================================


class SCOError(Exception):
    pass


def sco_run(args: list[str], timeout: int = 60) -> str:
    """Run the sco CLI. Treats rc!=0 OR 'resp code=4xx/5xx' in stderr as failure.

    Some sco subcommands (notably `eip dnat create`) exit 0 even on HTTP 4xx/5xx.
    """
    cmd = [SCO_BIN, *args]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    err = r.stderr.strip()
    if r.returncode != 0 or re.search(r"resp\s*code=\s*[45]\d\d", err):
        raise SCOError((err + " " + r.stdout.strip()).strip() or f"rc={r.returncode}")
    return r.stdout


def sco_eip_dnat_list(eip_name: str) -> list[dict[str, Any]]:
    """Read-only lookup of DNAT rules on an EIP. Write-side sco APIs are unreliable
    and intentionally not wrapped — if a rule needs to change, do it in the
    SenseCore console.
    """
    out = sco_run(["eip", "dnat", "list", eip_name, "-o", "json"])
    return (json.loads(out) or {}).get("dnat_rules", []) or []


def find_current_binding(rules: list[dict[str, Any]], uid: str) -> dict[str, Any] | None:
    for r in rules:
        p = r.get("properties") or {}
        if (
            p.get("internal_instance_name") == uid
            and str(p.get("internal_port")) == SSH_INTERNAL_PORT
            and (p.get("protocol") or "").lower() == "tcp"
        ):
            return r
    return None


def ssh_probe(host: str, port: int, user: str = SSH_USER, timeout: int = 5) -> tuple[bool, str]:
    cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={timeout}",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-p", str(port),
        f"{user}@{host}",
        "true",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
    except subprocess.TimeoutExpired:
        return False, "timeout"
    return r.returncode == 0, (r.stderr.strip() or r.stdout.strip())


def doctor_one(
    app: dict[str, Any],
    eip_name: str,
    external_ip: str,
    port_template: dict[str, Any],
    args: argparse.Namespace,
) -> int:
    name = app["name"]
    display = app.get("display_name") or "?"
    print(f"\n[doctor] {name} / {display} (ws={app['_workspace']})")

    # 1. Restart only if container is down
    state = (app.get("state") or "").upper()
    if state != "RUNNING":
        if args.dry_run:
            print(f"  state={state} — would start/restart (dry-run)")
        else:
            print(f"  state={state} — restarting")
            if state in INACTIVE_STATES:
                do_start(app)
            elif state in ACTIVE_STATES:
                wait_until(app, target={"RUNNING"}, timeout=120, label="wait")
            else:
                do_stop(app)
                app = get_app(app["_workspace"], app["name"])
                do_start(app)
            app = get_app(app["_workspace"], app["name"])
            state = (app.get("state") or "").upper()
            if state != "RUNNING":
                print(f"  ERROR app failed to reach RUNNING (state={state})", file=sys.stderr)
                return 1

    uid = app.get("uid")
    if not uid:
        print("  ERROR no uid on app object", file=sys.stderr)
        return 1
    print(f"  uid={uid}  state={app.get('state')}")

    # 2. Look up DNAT rule for this uid
    rules = sco_eip_dnat_list(eip_name)
    print(f"  eip={eip_name} rules={len(rules)}")
    binding = find_current_binding(rules, uid)

    if not binding:
        expected_hint = ""
        disp = (app.get("display_name") or "").strip()
        if disp in port_template:
            expected_hint = f" (port_template expects ext_port={port_template[disp]})"
        print(
            f"  ERROR no DNAT rule bound to uid={uid}{expected_hint}\n"
            f"  ACTION: manually create a DNAT rule in the SenseCore console:\n"
            f"          EIP           = {eip_name} ({external_ip})\n"
            f"          protocol      = tcp\n"
            f"          internal_instance_name = {uid}\n"
            f"          internal_port = {SSH_INTERNAL_PORT}\n"
            f"          external_port = <your chosen port>",
            file=sys.stderr,
        )
        return 1

    ext_port = int(binding["properties"].get("external_port"))
    print(f"  binding found: {binding['name']} ext_port={ext_port} → uid:22")

    # Sanity check against port_template (informational)
    disp = (app.get("display_name") or "").strip()
    if disp in port_template and int(port_template[disp]) != ext_port:
        print(
            f"  WARN binding on ext_port={ext_port} but [dnat.port_template][{disp!r}]={port_template[disp]}",
            file=sys.stderr,
        )

    # 3. Poll SSH until it comes up, or give up
    if args.dry_run:
        print(f"  (dry-run) would ssh-probe {external_ip}:{ext_port}")
        return 0
    return _ssh_wait(external_ip, ext_port, total_timeout=args.ssh_timeout, interval=args.ssh_interval)


def _ssh_wait(host: str, port: int, total_timeout: int, interval: int) -> int:
    print(f"  ssh -p {port} {SSH_USER}@{host} (up to {total_timeout}s, retry every {interval}s)")
    start = time.time()
    attempt = 0
    last_err = ""
    while True:
        attempt += 1
        ok, err = ssh_probe(host, port)
        elapsed = int(time.time() - start)
        if ok:
            print(f"    attempt {attempt}: ok (after {elapsed}s)")
            return 0
        last_err = err
        remaining = total_timeout - (time.time() - start)
        if remaining <= 0:
            break
        sleep_for = min(interval, max(1, int(remaining)))
        print(f"    attempt {attempt}: fail ({err[:80]}) — retrying in {sleep_for}s (remaining ~{int(remaining)}s)")
        time.sleep(sleep_for)
    print(f"  ERROR ssh never came up within {total_timeout}s: {last_err[:120]}", file=sys.stderr)
    return 1


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = load_dnat_config()
    eip_name = args.eip or cfg.get("eip_name")
    external_ip = args.external_ip or cfg.get("external_ip")
    port_template = cfg.get("port_template", {}) or {}
    if not eip_name or not external_ip:
        print(
            f"[doctor] missing eip_name/external_ip — set [dnat] in {ACP_CONFIG_PATH} or pass --eip/--external-ip",
            file=sys.stderr,
        )
        return 2

    # Resolve targets
    if args.all:
        if args.name:
            print("[doctor] cannot pass both <name> and --all", file=sys.stderr)
            return 2
        workspaces = [args.workspace] if args.workspace else linked_workspaces()
        apps = list_all_apps(workspaces)
        user_name, user_id = resolve_user(args)
        apps = filter_apps(apps, user_name=user_name, user_id=user_id)
        # Dedup by display_name: each template entry should map to exactly one app.
        # Prefer RUNNING > most-recently-updated.
        by_display: dict[str, list[dict[str, Any]]] = {}
        for a in apps:
            d = a.get("display_name")
            if d in port_template:
                by_display.setdefault(d, []).append(a)
        targets: list[dict[str, Any]] = []
        for d, group in by_display.items():
            running = [a for a in group if (a.get("state") or "").upper() == "RUNNING"]
            pool = running or group
            if len(pool) > 1:
                pool = sorted(pool, key=lambda a: a.get("update_time") or "", reverse=True)
                names = ", ".join(a["name"] for a in pool)
                print(
                    f"[doctor] display_name={d!r} matches {len(pool)} apps ({names}); picking most-recent: {pool[0]['name']}",
                    file=sys.stderr,
                )
            targets.append(pool[0])
        if not targets:
            print(
                f"[doctor] --all matched no apps with a display_name in port_template={list(port_template)}",
                file=sys.stderr,
            )
            return 1
    else:
        if not args.name:
            print("[doctor] specify <name> or pass --all", file=sys.stderr)
            return 2
        targets = [locate(args)]

    rc = 0
    for app in targets:
        rc |= doctor_one(app, eip_name, external_ip, port_template, args)
    return rc


# =============================================================================
# CLI
# =============================================================================


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cci", description="CCI app status & lifecycle (list/status/start/stop/restart).")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="List apps across linked workspaces")
    add_common_filters(pl)
    pl.add_argument("--state", help="Comma-separated state filter (RUNNING,SUSPENDED,...)")
    pl.add_argument("--json", action="store_true", help="Print JSON instead of table")
    pl.set_defaults(func=cmd_list)

    for verb, fn, help_text in [
        ("status",  cmd_status,  "Show one app's full status"),
        ("start",   cmd_start,   "Start a SUSPENDED app"),
        ("stop",    cmd_stop,    "Stop a RUNNING app"),
        ("restart", cmd_restart, "Restart (start if SUSPENDED, else stop+start)"),
    ]:
        sp = sub.add_parser(verb, help=help_text)
        sp.add_argument("name", help="App name (e.g. app-uipl1g7c) or display_name (e.g. zirui-cpu)")
        add_common_filters(sp)
        if verb == "restart":
            sp.add_argument("--force", action="store_true", help="Restart even from a transitional/unknown state")
        sp.set_defaults(func=fn)

    pd = sub.add_parser(
        "doctor",
        help="Restart the CCI app if down; verify DNAT lookup; poll ssh until it answers (or alert for manual rebind)",
    )
    pd.add_argument("name", nargs="?", help="App name or display_name (omit with --all)")
    pd.add_argument("--all", action="store_true", help="Run on every app whose display_name is in [dnat.port_template]")
    pd.add_argument("--eip", help="Override EIP name (otherwise from [dnat].eip_name)")
    pd.add_argument("--external-ip", help="Override EIP public IP (otherwise from [dnat].external_ip)")
    pd.add_argument("--ssh-timeout", type=int, default=180, help="Total seconds to wait for ssh to answer (default 180)")
    pd.add_argument("--ssh-interval", type=int, default=5, help="Seconds between ssh probes (default 5)")
    pd.add_argument("--dry-run", action="store_true", help="Show planned actions; don't restart or probe")
    add_common_filters(pd)
    pd.set_defaults(func=cmd_doctor)

    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except HMACError as e:
        print(f"[cci] {e}", file=sys.stderr)
        return 1
    except SCOError as e:
        print(f"[cci] sco: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
