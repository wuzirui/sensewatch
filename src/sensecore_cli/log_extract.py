#!/usr/bin/env python3
"""Extract run logs from SenseCore ACP jobs via `sco cms userlogs query`.

Usage:
    acp logs pt-v5ifrf3x
    acp logs pt-v5ifrf3x --workspace share-space-01e
    acp logs pt-v5ifrf3x --workspace p18-eacv -o /tmp/job.log
    acp logs pt-v5ifrf3x --worker 0          # only worker-0
    acp logs pt-v5ifrf3x --tail 200          # last 200 lines
    acp logs pt-v5ifrf3x --severity ERROR    # only errors
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

SCO = os.path.expanduser("~/.sco/bin/sco")

WORKSPACE_RESOURCE_IDS = {
    "share-space-01e": "01995848-9da4-7b9a-917c-db5bdea185e5",
    "p18-eacv": "019ebac3-c824-7701-9031-6d48f581ae12",
    "p1-video-world-model-for-robot-learning": "019d9523-828e-7198-88c1-fa43e4b13b93",
}

PRODUCT = "product.lepton-acp-new"
CST = timezone(timedelta(hours=8))
DEFAULT_WORKSPACE = "p18-eacv"


def run_cmd(args: list[str], check: bool = True) -> str:
    r = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if check and r.returncode != 0:
        print(f"Command failed: {' '.join(args)}", file=sys.stderr)
        print(r.stderr, file=sys.stderr)
        sys.exit(1)
    return r.stdout + r.stderr


def get_job_info(workspace: str, job_name: str) -> dict:
    """Get job UID, time range, and worker count from sco acp jobs describe."""
    raw = run_cmd([
        SCO, "acp", "jobs", "describe",
        "--workspace-name", workspace,
        "--debug", "-o", "json",
        job_name,
    ])
    # Extract UID from debug output (json output may have uid:null)
    uid_match = re.search(r'"uid"\s*:\s*"([a-f0-9]+)"', raw)
    if not uid_match:
        print(f"Could not extract UID for job {job_name}", file=sys.stderr)
        sys.exit(1)
    uid = uid_match.group(1)

    # Parse the JSON block — find the last top-level '{' ... '}' that contains "name"
    # The debug output has HTTP headers before the JSON body
    brace_depth = 0
    json_start = None
    json_end = None
    for i, c in enumerate(raw):
        if c == '{':
            if brace_depth == 0:
                json_start = i
            brace_depth += 1
        elif c == '}':
            brace_depth -= 1
            if brace_depth == 0:
                candidate = raw[json_start:i + 1]
                if f'"name":"{job_name}"' in candidate.replace(" ", ""):
                    json_end = i + 1
                    break
                # Also try with spaces
                if f'"name": "{job_name}"' in candidate:
                    json_end = i + 1
                    break

    if json_end is None:
        # Fallback: try parsing everything after "body:" or the last big JSON
        print("Warning: could not locate job JSON by name match, trying fallback", file=sys.stderr)
        info_parsed = {"name": job_name}
    else:
        info_parsed = json.loads(raw[json_start:json_end])

    # Extract total worker count from resource_spec
    total_workers = 0
    for role in info_parsed.get("roles", []):
        total_workers += role.get("total_replicas", 0)
        # Also check resource_spec replicas
        if total_workers == 0:
            for spec in role.get("resource_spec", []):
                total_workers += spec.get("replicas", 0)

    return {
        "uid": uid,
        "name": info_parsed.get("name", job_name),
        "display_name": info_parsed.get("display_name", ""),
        "state": info_parsed.get("state", "UNKNOWN"),
        "create_time": info_parsed.get("create_time"),
        "start_time": info_parsed.get("start_time"),
        "complete_time": info_parsed.get("complete_time"),
        "total_workers": max(total_workers, 1),
    }


def parse_utc_time(ts: str | None) -> datetime | None:
    if not ts:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def to_cst_str(dt: datetime) -> str:
    """Format datetime as CST string for sco CLI (which interprets times as local)."""
    cst_dt = dt.astimezone(CST)
    return cst_dt.strftime("%Y-%m-%d %H:%M:%S")


def query_logs_page(
    resource_id: str,
    start_cst: str,
    end_cst: str,
    filters: list[tuple[str, str]],
    page_size: int,
    offset: int,
) -> dict:
    """Run a single sco cms userlogs query and return parsed JSON."""
    cmd = [
        SCO, "cms", "userlogs", "query",
        "--start", start_cst,
        "--end", end_cst,
        "--resource-id", resource_id,
        "--product", PRODUCT,
        "--pageSize", str(page_size),
        "--offset", str(offset),
    ]
    for key, val in filters:
        cmd.extend(["--filter", f"{key}={val}"])

    raw = run_cmd(cmd, check=False)

    # Parse "query user logs success, body: <JSON>"
    match = re.search(r'body:\s*(\{.*)', raw, re.DOTALL)
    if not match:
        if "please specify the custom filter" in raw:
            print("Error: sco requires at least one --filter", file=sys.stderr)
            sys.exit(1)
        print(f"Unexpected output:\n{raw}", file=sys.stderr)
        sys.exit(1)

    return json.loads(match.group(1))


def paginate_worker(
    resource_id: str,
    start_cst: str,
    end_cst: str,
    filters: list[tuple[str, str]],
    page_size: int,
    label: str,
    limit: int | None = None,
) -> list[dict]:
    """Paginate through log pages for one set of filters.

    SenseCore returns newest-first pages. When limit is set, stop after enough
    newest lines have been fetched for this worker instead of reading the full
    log history.
    """
    all_hits = []
    offset = 0
    total = None

    while True:
        query_page_size = page_size
        if limit is not None:
            remaining = limit - len(all_hits)
            if remaining <= 0:
                break
            query_page_size = min(page_size, remaining)

        data = query_logs_page(resource_id, start_cst, end_cst, filters, query_page_size, offset)
        page_total = int(data.get("total", "0"))

        if total is None:
            total = page_total
            limit_note = f" (tail {limit} requested)" if limit is not None else ""
            print(f"  {label}: {total} lines{limit_note}", file=sys.stderr)
            if total == 0:
                break

        hits = data.get("hits", [])
        all_hits.extend(hits)

        fetched = len(all_hits)
        print(f"  {label}: {fetched}/{total}...", file=sys.stderr, end="\r")

        if fetched >= total or len(hits) == 0:
            break
        if limit is not None and fetched >= limit:
            break

        offset = fetched

    if total and total > 0:
        print(f"  {label}: {len(all_hits)}/{total} done.", file=sys.stderr)
    return all_hits


def extract_logs(
    workspace: str,
    job_name: str,
    worker: int | None = None,
    severity: str | None = None,
    page_size: int = 200,
    tail: int | None = None,
) -> tuple[dict, list[dict]]:
    """Extract log lines for a job. Returns (job_info, sorted_hits)."""
    # Step 1: get job info
    info = get_job_info(workspace, job_name)
    uid = info["uid"]
    total_workers = info["total_workers"]

    print(f"Job: {info['display_name']} ({info['name']})", file=sys.stderr)
    print(f"UID: {uid}", file=sys.stderr)
    print(f"State: {info['state']}, Workers: {total_workers}", file=sys.stderr)

    # Step 2: compute time range with padding
    create_dt = parse_utc_time(info["create_time"])
    complete_dt = parse_utc_time(info["complete_time"])
    start_dt = parse_utc_time(info["start_time"])

    range_start = (start_dt or create_dt or datetime.now(timezone.utc) - timedelta(days=1)) - timedelta(minutes=5)
    if complete_dt:
        range_end = complete_dt + timedelta(minutes=5)
    else:
        # Job still running — use now + buffer
        range_end = datetime.now(timezone.utc) + timedelta(minutes=5)

    start_cst = to_cst_str(range_start)
    end_cst = to_cst_str(range_end)
    print(f"Time range (CST): {start_cst} → {end_cst}", file=sys.stderr)

    # Step 3: resolve resource ID
    resource_id = WORKSPACE_RESOURCE_IDS.get(workspace)
    if not resource_id:
        print(f"Unknown workspace '{workspace}'. Known: {list(WORKSPACE_RESOURCE_IDS.keys())}", file=sys.stderr)
        sys.exit(1)

    if tail is not None and tail <= 0:
        print("Total collected: 0 lines", file=sys.stderr)
        return info, []

    # Step 4: collect logs — per-worker for multi-node, or all-at-once for single.
    # Severity remains client-side because the Monitor shim does not expose a
    # reliable server-side severity filter. In that case, read full history first
    # so older matching ERROR/WARN lines are not skipped.
    all_hits = []
    per_worker_limit = tail if tail is not None and severity is None else None

    if worker is not None:
        # User requested a specific worker
        filters = [
            ("Attributes.k8s.job.name", uid),
            ("Attributes.k8s.pod.name", f"pt-{uid}-worker-{worker}"),
        ]
        all_hits = paginate_worker(
            resource_id, start_cst, end_cst, filters, page_size, f"worker-{worker}",
            limit=per_worker_limit,
        )
    elif total_workers > 1:
        # Multi-node: query each worker separately to ensure we get all logs
        # (the API may truncate if total across all workers is very large)
        for w in range(total_workers):
            filters = [
                ("Attributes.k8s.job.name", uid),
                ("Attributes.k8s.pod.name", f"pt-{uid}-worker-{w}"),
            ]
            hits = paginate_worker(
                resource_id, start_cst, end_cst, filters, page_size, f"worker-{w}",
                limit=per_worker_limit,
            )
            all_hits.extend(hits)
    else:
        # Single node: query without pod filter
        filters = [("Attributes.k8s.job.name", uid)]
        all_hits = paginate_worker(
            resource_id, start_cst, end_cst, filters, page_size, "all",
            limit=per_worker_limit,
        )

    print(f"Total collected: {len(all_hits)} lines", file=sys.stderr)

    # Step 5: sort by timestamp (API returns newest-first)
    all_hits.sort(key=lambda h: h.get("log_time", ""))

    # Step 6: client-side severity filter
    if severity:
        sev_upper = severity.upper()
        all_hits = [h for h in all_hits if h.get("severity_text", "").upper() == sev_upper]
        print(f"  After severity filter ({sev_upper}): {len(all_hits)} lines", file=sys.stderr)

    if tail is not None:
        all_hits = all_hits[-tail:]

    return info, all_hits


def format_line(hit: dict, show_worker: bool = False) -> str:
    ts = hit.get("log_time", "")
    # Trim nanosecond precision to milliseconds for readability
    ts = re.sub(r'(\.\d{3})\d*Z$', r'\1Z', ts)
    sev = hit.get("severity_text", "INFO")
    body = hit.get("body", "")
    if show_worker:
        pod = hit.get("attributes", {}).get("k8s.pod.name", "")
        worker_match = re.search(r'worker-(\d+)', pod)
        rank = f"[w{worker_match.group(1)}]" if worker_match else ""
        return f"{ts} {sev:5s} {rank} {body}"
    return f"{ts} {sev:5s} {body}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract full ACP job logs via sco cms userlogs query")
    parser.add_argument("job_name", help="Job name (e.g. pt-v5ifrf3x)")
    parser.add_argument(
        "--workspace", "-w",
        default=DEFAULT_WORKSPACE,
        help=f"Workspace name (default: {DEFAULT_WORKSPACE})",
    )
    parser.add_argument("--output", "-o", help="Output file path (default: stdout)")
    parser.add_argument("--worker", type=int, default=None, help="Filter to specific worker rank (e.g. 0)")
    parser.add_argument(
        "--tail",
        type=int,
        default=None,
        help="Only print last N lines; without --severity, fetch bounded newest pages",
    )
    parser.add_argument("--severity", default=None, help="Filter by severity (e.g. ERROR, WARN)")
    parser.add_argument("--page-size", type=int, default=200, help="Page size per query (default: 200)")
    parser.add_argument("--json", action="store_true", help="Output raw JSON instead of formatted text")
    args = parser.parse_args(argv)

    info, hits = extract_logs(
        workspace=args.workspace,
        job_name=args.job_name,
        worker=args.worker,
        severity=args.severity,
        page_size=args.page_size,
        tail=args.tail,
    )

    show_worker = args.worker is None  # show worker tag when not filtering

    out = sys.stdout
    if args.output:
        out = open(args.output, "w")

    if args.json:
        json.dump({"job": info, "total": len(hits), "hits": hits}, out, indent=2, ensure_ascii=False)
        out.write("\n")
    else:
        for hit in hits:
            out.write(format_line(hit, show_worker=show_worker) + "\n")

    if args.output:
        out.close()
        print(f"Written to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
