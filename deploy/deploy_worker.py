r"""Install the worker over one password-authenticated SSH connection.

Run from a native console: .venv\Scripts\python.exe deploy\deploy_worker.py
--prepare-only prepares credentials and validates the payload without connecting.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request


HOST = "192.168.2.25"
HOST_ALIAS = "192.168.2.126"
USER = "relty"
REMOTE_PORT = 18120
LOCAL_PORT = 18121
REMOTE_ROOT = "/home/relty/services/datamark-worker"
DEPLOY_FILES = ("Dockerfile", "compose.yaml", "healthcheck.py", "install-worker.sh")
SHARES = ("homes", "datasets", "collector-data", "docker")


def configure_environment(project: Path) -> dict[str, str | None]:
    temporary = project / ".tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    settings = {
        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "PIP_REQUIRE_VIRTUALENV": "1",
        "PIP_CACHE_DIR": str(project / ".cache" / "pip"),
        "NPM_CONFIG_CACHE": str(project / ".cache" / "npm"),
        "UV_CACHE_DIR": str(project / ".cache" / "uv"),
        "UV_PYTHON_INSTALL_DIR": str(project / ".tools" / "uv-python"),
        "TEMP": str(temporary), "TMP": str(temporary),
    }
    previous = {key: os.environ.get(key) for key in settings}
    os.environ.update(settings)
    return previous


def restore_environment(previous: dict[str, str | None]) -> None:
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def update_status(directory: Path, state: str, **extra: object) -> None:
    write_json(directory / "deploy-status.json", {
        "state": state,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "server": f"{USER}@{HOST}",
        "remote_root": REMOTE_ROOT,
        **extra,
    })


def protect_directory(directory: Path) -> None:
    if os.name == "nt":
        identity = subprocess.run(["whoami"], check=True, capture_output=True, text=True).stdout.strip()
        subprocess.run(
            ["icacls", str(directory), "/inheritance:r", "/grant:r", f"{identity}:(OI)(CI)F"],
            check=True, capture_output=True,
        )
    else:
        directory.chmod(0o700)


def copy_trusted_host(ssh_keygen: str, source: Path, destination: Path) -> None:
    if not source.is_file():
        raise RuntimeError("The existing user known_hosts file is missing; verify the server identity first")
    result = subprocess.run(
        [ssh_keygen, "-F", HOST_ALIAS, "-f", str(source)], capture_output=True, text=True, check=False,
    )
    records = [line for line in result.stdout.splitlines() if line.strip() and not line.startswith("#")]
    if result.returncode or not records or any(line.startswith("@") for line in records):
        raise RuntimeError("A trusted server key for 192.168.2.126 was not found; no connection was attempted")
    destination.write_text("\n".join(records) + "\n", encoding="utf-8")


def prepare_credentials(project: Path, ssh_keygen: str) -> Path:
    directory = project / ".local" / "remote"
    directory.mkdir(parents=True, exist_ok=True)
    protect_directory(directory)
    copy_trusted_host(ssh_keygen, Path.home() / ".ssh" / "known_hosts", directory / "known_hosts")
    key = directory / "id_ed25519"
    public_key = directory / "id_ed25519.pub"
    if not key.exists():
        if public_key.exists():
            raise RuntimeError("The deployment public key exists without its private key")
        subprocess.run(
            [ssh_keygen, "-q", "-t", "ed25519", "-N", "", "-C", "datamark-worker", "-f", str(key)],
            stdin=subprocess.DEVNULL, check=True, capture_output=True,
        )
    if not public_key.is_file() or not re.fullmatch(
        r"ssh-ed25519 [A-Za-z0-9+/=]+ datamark-worker\s*", public_key.read_text(encoding="utf-8")
    ):
        raise RuntimeError("The dedicated deployment public key is missing or invalid")
    token_file = directory / "worker-token"
    if not token_file.exists():
        descriptor = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(secrets.token_hex(32) + "\n")
    if not re.fullmatch(r"[0-9a-f]{64}\s*", token_file.read_text(encoding="utf-8")):
        raise RuntimeError("The existing worker token is invalid")
    return directory


def add_bytes(archive: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644) -> None:
    item = tarfile.TarInfo(name)
    item.size = len(data)
    item.mode = mode
    item.mtime = int(time.time())
    archive.addfile(item, io.BytesIO(data))


def build_payload(project: Path, directory: Path, release_id: str) -> bytes:
    if not (project / "backend" / "remote_worker.py").is_file():
        raise RuntimeError("backend/remote_worker.py is missing")
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for path in sorted((project / "backend").rglob("*.py")):
            if "__pycache__" in path.parts or path.name.startswith("test_"):
                continue
            if path.is_symlink():
                raise RuntimeError("The deployment source contains a symlink")
            add_bytes(archive, path.relative_to(project).as_posix(), path.read_bytes())
        add_bytes(archive, "requirements.lock.txt", (project / "requirements.lock.txt").read_bytes())
        for name in DEPLOY_FILES:
            path = project / "deploy" / name
            if path.is_symlink():
                raise RuntimeError("The deployment source contains a symlink")
            data = path.read_bytes().replace(b"\r\n", b"\n")
            add_bytes(archive, "deploy/" + name, data, 0o755 if name.endswith(".sh") else 0o644)
        token = (directory / "worker-token").read_text(encoding="utf-8").strip()
        add_bytes(archive, "payload/worker-token", token.encode("ascii") + b"\n", 0o600)
        public_key = (directory / "id_ed25519.pub").read_text(encoding="utf-8").strip()
        add_bytes(archive, "payload/worker-key.pub", public_key.encode("ascii") + b"\n")
        add_bytes(archive, "payload/release-id", release_id.encode("ascii") + b"\n")
    return stream.getvalue()


def ssh_options(directory: Path) -> list[str]:
    return [
        "-F", "NUL" if os.name == "nt" else "/dev/null",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={directory / 'known_hosts'}",
        "-o", f"HostKeyAlias={HOST_ALIAS}",
        "-o", "ConnectTimeout=15",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
    ]


def remote_install_command() -> str:
    # Only a freshly created directory with this exact prefix is removed.
    script = r'''set -eu
umask 077
base=/home/relty/services
test -d "$base" && test -w "$base"
stage=$(mktemp -d "$base/.datamark-upload.XXXXXXXXXXXX")
cleanup() {
    case "$stage" in /home/relty/services/.datamark-upload.*)
        test ! -L "$stage" && rm -rf -- "$stage" ;;
    esac
}
trap cleanup EXIT HUP INT TERM
tar -xf - -C "$stage"
bash "$stage/deploy/install-worker.sh" "$stage"
'''
    return "bash -c " + shlex.quote(script)


def run_install(ssh: str, directory: Path, payload: bytes, release_id: str) -> None:
    command = [ssh, *ssh_options(directory), "-T", "-o", "PubkeyAuthentication=no",
               "-o", "PreferredAuthentications=password,keyboard-interactive",
               "-o", "NumberOfPasswordPrompts=3", f"{USER}@{HOST}", remote_install_command()]
    print("Enter the server password when SSH asks. Input is hidden and is not logged.", flush=True)
    log_path = directory / "deploy.log"
    print(f"Deployment output is written directly to: {log_path}", flush=True)
    with log_path.open("ab", buffering=0) as log:
        log.write(f"\n--- {release_id} ---\n".encode("ascii"))
        log_offset = log.tell()
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=log, stderr=log)
        transfer_errors: list[Exception] = []

        def send_payload() -> None:
            try:
                assert process.stdin is not None
                process.stdin.write(payload)
                process.stdin.close()
            except (BrokenPipeError, OSError) as exc:
                transfer_errors.append(exc)

        sender = threading.Thread(target=send_payload, daemon=True)
        sender.start()
        try:
            code = process.wait(timeout=1200)
        except BaseException:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            raise
        finally:
            sender.join(timeout=5)
    if code != 0 or transfer_errors or sender.is_alive():
        raise RuntimeError(f"SSH deployment did not complete (exit {code}); inspect .local/remote/deploy.log")
    expected = f"DATAMARK_DEPLOY_READY {release_id}".encode("ascii")
    with log_path.open("rb") as incoming:
        incoming.seek(log_offset)
        confirmed = any(line.rstrip(b"\r\n") == expected for line in incoming)
    if not confirmed:
        raise RuntimeError("The server did not confirm successful deployment")


def worker_configuration(directory: Path) -> dict:
    return {
        "enabled": True,
        "host": HOST,
        "user": USER,
        "port": REMOTE_PORT,
        "local_port": LOCAL_PORT,
        "identity_file": str((directory / "id_ed25519").resolve()),
        "known_hosts_file": str((directory / "known_hosts").resolve()),
        "host_key_alias": HOST_ALIAS,
        "token_file": str((directory / "worker-token").resolve()),
        "mappings": [{"local": "\\\\Relty\\" + share, "remote": "/mnt/nas/" + share} for share in SHARES],
    }


def verify_tunnel(ssh: str, directory: Path) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        verification_port = reservation.getsockname()[1]
    command = [ssh, *ssh_options(directory), "-v", "-N", "-T", "-o", "BatchMode=yes",
               "-o", "IdentitiesOnly=yes", "-o", "ExitOnForwardFailure=yes",
               "-i", str(directory / "id_ed25519"), "-L",
               f"127.0.0.1:{verification_port}:127.0.0.1:{REMOTE_PORT}", f"{USER}@{HOST}"]
    token = (directory / "worker-token").read_text(encoding="utf-8").strip()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    log_path = directory / "tunnel-check.log"
    marker = f"Local forwarding listening on 127.0.0.1 port {verification_port}.".encode("ascii")
    with log_path.open("ab") as log:
        log.seek(0, os.SEEK_END)
        log_offset = log.tell()
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=flags)
        try:
            deadline = time.monotonic() + 25
            listening = False
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("The dedicated SSH tunnel failed; inspect .local/remote/tunnel-check.log")
                if not listening:
                    with log_path.open("rb") as incoming:
                        incoming.seek(log_offset)
                        listening = marker in incoming.read(1024 * 1024)
                    if not listening:
                        time.sleep(0.1)
                        continue
                    if process.poll() is not None:
                        raise RuntimeError("The dedicated SSH tunnel closed before verification")
                try:
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{verification_port}/health", headers={"Authorization": f"Bearer {token}"},
                    )
                    with opener.open(request, timeout=2) as response:
                        data = json.load(response)
                    if data.get("status") == "ok" and data.get("application") == "datamark-worker" and data.get("protocol") == 1:
                        if process.poll() is not None:
                            raise RuntimeError("The verification SSH tunnel closed unexpectedly")
                        return
                    raise RuntimeError("The worker returned an incompatible health response")
                except (OSError, urllib.error.URLError):
                    time.sleep(0.3)
            raise RuntimeError("The worker did not become reachable through its dedicated SSH tunnel")
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", action="store_true", help="Prepare local files without connecting")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    previous_environment = configure_environment(project)
    directory = project / ".local" / "remote"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        ssh = shutil.which("ssh")
        ssh_keygen = shutil.which("ssh-keygen")
        if not ssh or not ssh_keygen:
            raise RuntimeError("Windows OpenSSH client and ssh-keygen are required")
        update_status(directory, "preparing")
        directory = prepare_credentials(project, ssh_keygen)
        release_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(6)
        payload = build_payload(project, directory, release_id)
        if args.prepare_only:
            update_status(directory, "prepared", payload_bytes=len(payload))
            print("Local deployment files are ready. No server connection was made.")
            return 0
        update_status(directory, "connecting", release=release_id)
        run_install(ssh, directory, payload, release_id)
        update_status(directory, "verifying", release=release_id)
        verify_tunnel(ssh, directory)
        write_json(directory / "worker.json", worker_configuration(directory))
        update_status(directory, "ready", release=release_id, local_port=LOCAL_PORT, remote_port=REMOTE_PORT)
        print("Worker is healthy. Local Datamark remote processing is now enabled.")
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        message = "Deployment was interrupted" if isinstance(exc, KeyboardInterrupt) else str(exc)
        update_status(directory, "failed", error=message)
        print(message, file=sys.stderr)
        return 1
    finally:
        restore_environment(previous_environment)


if __name__ == "__main__":
    raise SystemExit(main())
