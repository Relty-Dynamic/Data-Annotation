"""Wait for the published HTTPS endpoint, not merely a running gateway process."""
import argparse
import json
import subprocess
import sys
import time


class ReadinessError(RuntimeError):
    pass


def wait_ready(url, ca_file, *, timeout=60, interval=2):
    deadline = time.monotonic() + timeout
    last_error = "no response"
    attempts = 0
    while (remaining := deadline - time.monotonic()) > 0:
        attempts += 1
        request_timeout = min(5, remaining)
        try:
            response = subprocess.run(
                ["curl", "--fail", "--silent", "--show-error", "--noproxy", "*",
                 "--proto", "=https", "--cacert", str(ca_file),
                 "--connect-timeout", str(min(3, request_timeout)),
                 "--max-time", str(request_timeout), url],
                capture_output=True, text=True, timeout=request_timeout, check=False)
        except subprocess.TimeoutExpired:
            last_error = "HTTPS request timed out"
        else:
            if response.returncode == 0:
                try:
                    data = json.loads(response.stdout)
                except ValueError as error:
                    raise ReadinessError("Health endpoint returned invalid JSON") from error
                if (not isinstance(data, dict) or data.get("status") != "ok"
                        or data.get("application") != "datamark"
                        or data.get("storage_backend") != "nas"
                        or not isinstance(data.get("capabilities"), list)
                        or "account-login" not in data["capabilities"]):
                    raise ReadinessError("Health endpoint is not a ready DataMark NAS service")
                print(f"DataMark intranet HTTPS ready after {attempts} attempt(s)", flush=True)
                return
            # Connection refusal, HTTP startup errors and interrupted/slow
            # transfers may recover. Certificate/configuration errors must not.
            if response.returncode not in {7, 22, 28, 52, 55, 56}:
                raise ReadinessError(f"HTTPS verification failed (curl exit {response.returncode})")
            last_error = f"curl exit {response.returncode}"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if attempts == 1:
            print(f"Waiting up to {timeout}s for intranet HTTPS readiness: {last_error}", flush=True)
        time.sleep(min(interval, remaining))
    raise ReadinessError(f"Intranet HTTPS not ready within {timeout}s ({last_error})")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("ca_file")
    args = parser.parse_args()
    try:
        wait_ready(args.url, args.ca_file)
    except (OSError, ReadinessError) as error:
        print(f"DataMark readiness check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
