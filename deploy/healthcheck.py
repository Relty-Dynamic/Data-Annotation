"""Container-local authenticated health check; never prints the token."""

import json
import os
from pathlib import Path
import urllib.request


def check() -> None:
    token = Path(os.environ["DATAMARK_WORKER_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
    request = urllib.request.Request(
        "http://127.0.0.1:18120/health", headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        data = json.load(response)
    if data.get("status") != "ok" or data.get("application") != "datamark-worker" or data.get("protocol") != 1:
        raise RuntimeError("Worker health response is incompatible")


if __name__ == "__main__":
    check()
