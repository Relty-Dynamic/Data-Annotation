"""Check the application through its private container listener."""

import json
import urllib.request


def check() -> None:
    request = urllib.request.Request("http://127.0.0.1:8765/api/health")
    with urllib.request.urlopen(request, timeout=3) as response:
        data = json.load(response)
    if data.get("status") != "ok" or data.get("application") != "datamark" or "account-login" not in data.get("capabilities", []):
        raise RuntimeError("DataMark web health response is incompatible")


if __name__ == "__main__":
    check()
