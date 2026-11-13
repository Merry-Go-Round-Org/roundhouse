"""Opt-in localhost.run tunnel. Requires explicit operator consent to expose this API.
Run from backend: python -m app.tunnel
Free domains rotate: writes the current URL to ignored storage/INTEGRATION_STATUS.json.
"""

import json
import subprocess
import re
import sys
from datetime import datetime, timezone
from app.config import settings


def main():
    if settings.allow_local_demo_auth:
        raise SystemExit(
            "Refusing a public tunnel with anonymous demo authentication enabled"
        )
    root = settings.storage_path
    root.mkdir(parents=True, exist_ok=True)
    path = root / "INTEGRATION_STATUS.json"

    def save(**changes):
        data = json.loads(path.read_text()) if path.exists() else {}
        data.update(changes, tunnel_updated_at=datetime.now(timezone.utc).isoformat())
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(path)

    save(tunnel_running=False)
    command = [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        "IdentitiesOnly=yes",
        "-i",
        "/dev/null",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={root / 'tunnel_known_hosts'}",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ExitOnForwardFailure=yes",
        "-R",
        "80:127.0.0.1:8000",
        "nokey@localhost.run",
    ]
    print(
        "Starting explicitly requested public HTTPS tunnel. No paid account. Stop with Ctrl+C.",
        flush=True,
    )
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    try:
        for line in process.stdout:
            match = re.search(r"(https://[a-zA-Z0-9.-]+\.lhr\.(?:life|rocks))", line)
            if match:
                save(
                    base_url=match.group(1),
                    tunnel_running=True,
                    hosting="temporary localhost.run tunnel; URL rotates",
                )
                print("Current API: " + match.group(1) + "/api/v1", flush=True)
                print("Sign in: " + match.group(1) + "/login", flush=True)
            elif any(
                word in line.lower() for word in ("error", "failed", "denied", "closed")
            ):
                print(line.strip(), file=sys.stderr, flush=True)
        code = process.wait()
    except KeyboardInterrupt:
        code = 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        save(tunnel_running=False)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
