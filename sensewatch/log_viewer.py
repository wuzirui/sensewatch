"""Fetch and display job logs — live (sco stream-logs) or offline (Monitor API)."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import rumps

from . import config
from .api_client import SenseCoreAPIClient, _find_sco
from .state import JobSnapshot

# ANSI escape code stripper
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _clean_log_line(line: str) -> str:
    """Strip ANSI codes and the 'pt-...-worker-N pytorch logs:' prefix."""
    line = _strip_ansi(line)
    # Remove "pt-<uid>-worker-N pytorch logs: " prefix
    if "pytorch logs:" in line:
        line = line.split("pytorch logs:", 1)[1].strip()
    return line


class LogViewer:
    """Fetches logs for ACP jobs — live via sco or offline via Monitor API."""

    def __init__(self, client: SenseCoreAPIClient):
        self.client = client

    # ── Live logs (running jobs) via sco stream-logs ──────────────────────

    def fetch_live_logs(self, job: JobSnapshot, timeout: float = 3.0) -> str:
        """Grab recent log lines from a running job using sco stream-logs.

        Runs sco with -o to a temp file, waits `timeout` seconds, kills it,
        and returns the last lines captured.
        """
        sco = _find_sco()
        if not sco:
            return "(sco CLI not found — cannot fetch live logs)"

        tmp = tempfile.NamedTemporaryFile(
            prefix="sensewatch_log_", suffix=".txt", delete=False
        )
        tmp.close()

        try:
            proc = subprocess.Popen(
                [
                    sco, "acp", "jobs", "stream-logs",
                    f"--workspace-name={job.workspace}",
                    "-o", tmp.name,
                    job.name,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(timeout)
            proc.kill()  # SIGKILL — don't wait for graceful shutdown
            proc.wait(timeout=1)

            text = Path(tmp.name).read_text(errors="replace")
            lines = text.strip().splitlines()
            if not lines:
                return "(No log output captured)"

            tail = lines[-20:]
            return "\n".join(_clean_log_line(l) for l in tail)
        except Exception as e:
            return f"(Failed to fetch live logs: {e})"
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    def fetch_live_log_preview(self, job: JobSnapshot, timeout: float = 3.0) -> list[str]:
        """Return the last 3 cleaned log lines for menu preview."""
        sco = _find_sco()
        if not sco:
            return ["(sco not found)"]

        tmp = tempfile.NamedTemporaryFile(
            prefix="sensewatch_preview_", suffix=".txt", delete=False
        )
        tmp.close()

        try:
            proc = subprocess.Popen(
                [
                    sco, "acp", "jobs", "stream-logs",
                    f"--workspace-name={job.workspace}",
                    "-o", tmp.name,
                    job.name,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(timeout)
            proc.kill()
            proc.wait(timeout=1)

            text = Path(tmp.name).read_text(errors="replace")
            lines = text.strip().splitlines()
            if not lines:
                return ["(no output)"]

            cleaned = [_clean_log_line(l) for l in lines if l.strip()]
            cleaned = [l for l in cleaned if not l.startswith("time=") and not l.startswith("job ")]
            return cleaned[-3:] if cleaned else ["(no training output yet)"]
        except Exception as e:
            return [f"(error: {e})"]
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    # ── Offline logs (terminated jobs) via Monitor API ────────────────────

    def fetch_offline_logs(self, job: JobSnapshot, max_lines: int = 50) -> str:
        """Query the Monitor API for logs of a terminated job."""
        if not job.uid:
            return f"(No UID available for job {job.name} — cannot query logs)"

        resource_id = config.WORKSPACE_RESOURCE_IDS.get(job.workspace)
        if not resource_id:
            # Try sco stream-logs as fallback
            return self.fetch_live_logs(job, timeout=4.0)

        end_ts = str(int(time.time()))
        start_ts = str(int(time.time()) - 86400 * 7)
        if job.create_time:
            try:
                ct = datetime.fromisoformat(job.create_time.replace("Z", "+00:00"))
                start_ts = str(int(ct.timestamp()))
            except (ValueError, OSError):
                pass

        ts = "ts-user-019c3278-4a84-7773-ade4-259d2bc7705f"

        body: dict[str, Any] = {
            "resource_id": [resource_id],
            "start": start_ts,
            "end": end_ts,
            "page_size": max_lines,
            "offset": "0",
            "severity_text": ["TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"],
            "custom_filter": [
                {"key": "Attributes.k8s.job.name", "value": job.uid},
                {"key": "Attributes.k8s.container.name", "value": "pytorch"},
            ],
        }

        try:
            result = self.client.query_logs(
                telemetry_station=ts,
                product="product.lepton-acp-new",
                body=body,
            )
            hits = result.get("hits") or []
            if not hits:
                # Fallback to sco stream-logs (works for completed jobs too)
                return self.fetch_live_logs(job, timeout=4.0)

            lines_out = []
            for hit in reversed(hits):
                ts_str = hit.get("log_time", "")[:19]
                body_text = hit.get("body", "")
                lines_out.append(f"[{ts_str}] {body_text}")
            return "\n".join(lines_out)

        except Exception:
            # Fallback to sco
            return self.fetch_live_logs(job, timeout=4.0)

    # show_logs() removed — app.py handles the dialog in a background thread
