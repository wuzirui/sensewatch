#!/usr/bin/env python3
"""Minimal SenseCore HMAC request helper using macOS Keychain credentials."""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import subprocess
import sys
import urllib.error
import urllib.request
from email.utils import formatdate


def read_keychain_password(service: str, account: str) -> str:
    return subprocess.check_output(
        ["security", "find-generic-password", "-w", "-a", account, "-s", service],
        text=True,
    ).strip()


def build_auth_header(access_key_id: str, secret: str, method: str, path: str, x_date: str) -> str:
    request_line = f"{method} {path} HTTP/1.1"
    string_to_sign = f"x-date: {x_date}\n{request_line}"
    signature = base64.b64encode(
        hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")
    return (
        f'hmac accesskey="{access_key_id}", algorithm="hmac-sha256", '
        f'headers="x-date request-line", signature="{signature}"'
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send a signed SenseCore API request.")
    parser.add_argument("--service-base", required=True, help="Base URL, e.g. https://aec2.cn-sh-01.sensecoreapi.cn")
    parser.add_argument("--path", required=True, help="Request path including query string, e.g. /compute/acp/.../trainingJobs?page_size=10")
    parser.add_argument("--method", default="GET", help="HTTP method")
    parser.add_argument("--data", help="Inline JSON request body")
    parser.add_argument("--data-file", help="Path to a JSON body file")
    parser.add_argument("--header", action="append", default=[], help="Extra header in Key:Value form")
    parser.add_argument("--akid-service", default="sensecore_access_key_id", help="Keychain service name for AccessKey ID")
    parser.add_argument("--secret-service", default="sensecore_access_key_secret", help="Keychain service name for AccessKey Secret")
    parser.add_argument("--account", default=getpass.getuser(), help="Keychain account name")
    parser.add_argument("--timeout", type=int, default=30, help="Request timeout in seconds")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    method = args.method.upper()
    body_bytes = None

    if args.data and args.data_file:
        print("Use only one of --data or --data-file", file=sys.stderr)
        return 2

    if args.data_file:
        with open(args.data_file, "rb") as handle:
            body_bytes = handle.read()
    elif args.data:
        body_bytes = args.data.encode("utf-8")

    akid = read_keychain_password(args.akid_service, args.account)
    secret = read_keychain_password(args.secret_service, args.account)
    x_date = formatdate(usegmt=True)
    auth = build_auth_header(akid, secret, method, args.path, x_date)

    headers = {
        "X-Date": x_date,
        "Authorization": auth,
        "Accept": "application/json",
    }
    if body_bytes is not None:
        headers["Content-Type"] = "application/json"

    for item in args.header:
        if ":" not in item:
            print(f"Invalid --header value: {item}", file=sys.stderr)
            return 2
        key, value = item.split(":", 1)
        headers[key.strip()] = value.strip()

    request = urllib.request.Request(
        args.service_base.rstrip("/") + args.path,
        data=body_bytes,
        method=method,
        headers=headers,
    )

    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            payload = response.read().decode("utf-8", "replace")
            print(f"STATUS {response.status}")
            try:
                print(json.dumps(json.loads(payload), ensure_ascii=False, indent=2))
            except json.JSONDecodeError:
                print(payload)
            return 0
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        payload = exc.read().decode("utf-8", "replace")
        print(f"STATUS {exc.code}", file=sys.stderr)
        print(payload, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
