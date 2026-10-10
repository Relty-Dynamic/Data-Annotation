#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

deploy_home=/home/relty/services/datamark-web
env_file="$deploy_home/intranet.env"
current="$deploy_home/current"
root_ca="$deploy_home/caddy-data/caddy/pki/authorities/local/root.crt"

die() {
    printf 'DataMark deployment: %s\n' "$*" >&2
    exit 1
}

[[ "$(id -un)" == relty ]] || die 'run on relty-server as relty'
[[ -f "$env_file" && -r "$env_file" ]] || die 'stable intranet.env is missing'
[[ -f "$root_ca" && -r "$root_ca" ]] || die 'Caddy public root certificate is missing'
[[ -L "$current" ]] || die 'current release symlink is missing'
[[ -d "$deploy_home/state" ]] || die 'persistent state is missing'
[[ -f "$deploy_home/state/auth.sqlite3" ]] || die 'administrator database is missing'

commit="$(git rev-parse --verify HEAD)"
main_commit="$(git rev-parse --verify refs/remotes/origin/main)"
[[ "$commit" == "$main_commit" ]] || die 'only the checked-out origin/main commit may deploy'
[[ -z "$(git status --porcelain --untracked-files=no)" ]] || die 'tracked source files have changed in the workspace'
docker image inspect "datamark-web:$commit" >/dev/null || die 'tested release image is missing'

configured_home="$(sed -n 's/^DATAMARK_HOME=//p' "$env_file" | tail -n 1)"
host="$(sed -n 's/^DATAMARK_HOST=//p' "$env_file" | tail -n 1)"
public_origin="$(sed -n 's/^DATAMARK_PUBLIC_ORIGIN=//p' "$env_file" | tail -n 1)"
public_api_origin="$(sed -n 's/^DATAMARK_PUBLIC_API_ORIGIN=//p' "$env_file" | tail -n 1)"
public_bind_ip="$(sed -n 's/^DATAMARK_PUBLIC_BIND_IP=//p' "$env_file" | tail -n 1)"
[[ "$configured_home" == "$deploy_home" ]] || die 'DATAMARK_HOME does not match the deployment directory'
[[ "$host" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die 'DATAMARK_HOST must be the internal IPv4 address'
[[ -z "$public_origin" && -z "$public_api_origin" && -z "$public_bind_ip" ||
   -n "$public_origin" && -n "$public_api_origin" && -n "$public_bind_ip" ]] ||
    die 'both public origins and DATAMARK_PUBLIC_BIND_IP must be set together'
if [[ -n "$public_api_origin" ]]; then
    public_endpoint="$(python3 - "$public_origin" "$public_api_origin" "$public_bind_ip" "$host" <<'PY'
import ipaddress
import json
import subprocess
import sys
from urllib.parse import urlsplit

front_origin, api_origin, bind_ip, intranet_host = sys.argv[1:]
def hostname(origin, setting, *, allow_port=False):
    parsed = urlsplit(origin)
    port = parsed.port
    if (parsed.scheme != "https" or not parsed.hostname or (port is not None and (not allow_port or port == 443))
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
            or parsed.netloc != parsed.netloc.lower() or "*" in parsed.netloc
            or any(char.isspace() for char in origin)):
        raise SystemExit(f"{setting} must be one exact HTTPS hostname")
    labels = parsed.hostname.split(".")
    if (len(labels) < 2 or len(parsed.hostname) > 253
            or any(not 1 <= len(label) <= 63 or not label[0].isalnum() or not label[-1].isalnum()
                   or any(not (character.isascii() and (character.isalnum() or character == "-"))
                          for character in label) for label in labels)):
        raise SystemExit(f"{setting} must use a valid DNS hostname")
    return parsed.hostname
front_host = hostname(front_origin, "DATAMARK_PUBLIC_ORIGIN")
api_host = hostname(api_origin, "DATAMARK_PUBLIC_API_ORIGIN", allow_port=True)
if front_host == api_host:
    raise SystemExit("public frontend and API must use separate hostnames")
if urlsplit(api_origin).port != 10443:
    raise SystemExit("DATAMARK_PUBLIC_API_ORIGIN must use TCP 10443")
address = ipaddress.ip_address(bind_ip)
if address.is_loopback or address.is_unspecified or bind_ip == intranet_host:
    raise SystemExit("DATAMARK_PUBLIC_BIND_IP must be a separate local interface address")
interfaces = json.loads(subprocess.check_output(["ip", "-j", "address", "show"]))
if bind_ip not in {item["local"] for interface in interfaces for item in interface.get("addr_info", [])}:
    raise SystemExit("DATAMARK_PUBLIC_BIND_IP is not assigned to this server")
print(f"{api_host} {urlsplit(api_origin).port or 443}")
PY
)" || die 'invalid public gateway settings'
    read -r public_host public_port <<< "$public_endpoint"
    [[ -d "$deploy_home/public-caddy-data" && -d "$deploy_home/public-caddy-config" ]] ||
        die 'persistent public Caddy directories are missing'
    cloudflare_token="$deploy_home/secrets/cloudflare-api-token"
    [[ -s "$cloudflare_token" && -r "$cloudflare_token" ]] ||
        die 'Cloudflare DNS API token secret is missing or unreadable'
    [[ "$(stat -c %a "$cloudflare_token")" == 400 || "$(stat -c %a "$cloudflare_token")" == 600 ]] ||
        die 'Cloudflare DNS API token secret must have mode 0400 or 0600'
    docker image inspect 'ghcr.io/caddy-dns/cloudflare@sha256:f46799052f9dfcf7e634326d279d47a58c1ea598f8330c0b77921971f14aeaf0' >/dev/null ||
        die 'pinned public Caddy image is missing'
fi

previous="$(readlink -f "$current")"
[[ "$previous" == "$deploy_home"/releases/* && -f "$previous/deploy/compose.intranet.yaml" ]] || die 'current symlink points outside a valid release'
compose_files=(-f "$deploy_home/releases/$commit/deploy/compose.intranet.yaml")
previous_compose_files=(-f "$previous/deploy/compose.intranet.yaml")
previous_gateway_running="$(docker inspect datamark-intranet-public_gateway-1 \
    --format '{{.State.Running}}' 2>/dev/null || true)"
if [[ -n "$public_api_origin" ]]; then
    compose_files+=(-f "$deploy_home/releases/$commit/deploy/compose.public.yaml")
    if [[ "$previous_gateway_running" == true ]]; then
        [[ -f "$previous/deploy/compose.public.yaml" ]] ||
            die 'running public gateway has no previous Compose definition'
        previous_compose_files+=(-f "$previous/deploy/compose.public.yaml")
    fi
fi
previous_image="$(docker inspect datamark-intranet-web-1 --format '{{.Config.Image}}')"
[[ "$previous_image" == datamark-web:* ]] || die 'running web image is unexpected'
previous_tag="${previous_image#datamark-web:}"

# Keep the persistent Compose image tag in step with the tested release. A
# later manual restart otherwise reads the old tag from intranet.env and can
# silently bring back an older web image.
persist_image_tag() {
    python3 - "$env_file" "$1" <<'PY'
import os
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
tag = sys.argv[2]
original = path.read_text(encoding="utf-8")
updated = []
found = False
for line in original.splitlines(keepends=True):
    if line.startswith("DATAMARK_WEB_IMAGE_TAG="):
        if not found:
            updated.append(f"DATAMARK_WEB_IMAGE_TAG={tag}\n")
            found = True
    else:
        updated.append(line)
if not found:
    if updated and not updated[-1].endswith("\n"):
        updated.append("\n")
    updated.append(f"DATAMARK_WEB_IMAGE_TAG={tag}\n")
fd, temporary = tempfile.mkstemp(prefix=".intranet.env.", dir=path.parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.writelines(updated)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
except BaseException:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
PY
}

release="$deploy_home/releases/$commit"
if [[ -e "$release" ]]; then
    [[ -d "$release" && -f "$release/.release-commit" ]] || die 'release path is incomplete'
    [[ "$(cat "$release/.release-commit")" == "$commit" ]] || die 'release marker does not match commit'
else
    staging="$(mktemp -d "$deploy_home/releases/.release.XXXXXXXX")"
    trap 'rm -rf -- "${staging:-}"' EXIT
    git archive "$commit" | tar -x -C "$staging"
    printf '%s\n' "$commit" > "$staging/.release-commit"
    mv -- "$staging" "$release"
    staging=
    trap - EXIT
fi

export DATAMARK_WEB_IMAGE_TAG="$commit"
docker compose --env-file "$env_file" "${compose_files[@]}" config --quiet

mkdir -p -- "$deploy_home/backups"
backup="$deploy_home/backups/$commit-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -- "$backup"
python3 - "$deploy_home/state" "$backup" <<'PY'
import sqlite3
import sys
from pathlib import Path

state, backup = map(Path, sys.argv[1:])
for name in ("annotations.sqlite3", "auth.sqlite3"):
    source = state / name
    if not source.is_file():
        raise SystemExit(f"missing database: {name}")
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as original:
        with sqlite3.connect(backup / name) as copy:
            original.backup(copy)
            if copy.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise SystemExit(f"invalid backup: {name}")
PY

needs_rollback=0
env_tag_updated=0
on_exit() {
    status=$?
    trap - EXIT
    if ((status != 0 && needs_rollback)); then
        printf 'DataMark deployment failed; restoring prior containers from %s\n' "$previous" >&2
        if [[ "$previous_gateway_running" != true && -n "$public_api_origin" ]]; then
            docker compose --env-file "$env_file" "${compose_files[@]}" rm --stop --force public_gateway ||
                printf 'DataMark rollback could not remove the new public gateway\n' >&2
        fi
        if ((env_tag_updated)); then
            persist_image_tag "$previous_tag" ||
                printf 'DataMark rollback could not restore the persistent image tag\n' >&2
        fi
        if ! DATAMARK_WEB_IMAGE_TAG="$previous_tag" docker compose \
            --env-file "$env_file" "${previous_compose_files[@]}" \
            up -d --no-build --wait --wait-timeout 180; then
            printf 'DataMark rollback failed; inspect the live containers before another deployment\n' >&2
        fi
    fi
    exit "$status"
}
trap on_exit EXIT

needs_rollback=1
docker compose --env-file "$env_file" "${compose_files[@]}" \
    up -d --no-build --wait --wait-timeout 180
[[ "$(docker inspect datamark-intranet-web-1 --format '{{.Config.Image}}')" == "datamark-web:$commit" ]] ||
    die 'running web container is not the tested image'
curl --fail --silent --show-error --cacert "$root_ca" \
    "https://$host/api/health" |
    python3 -c 'import json,sys; data=json.load(sys.stdin); assert data["status"] == "ok" and "account-login" in data["capabilities"]'
if [[ -n "$public_api_origin" ]]; then
    resolve_ip="$public_bind_ip"
    [[ "$resolve_ip" != *:* ]] || resolve_ip="[$resolve_ip]"
    public_ok=0
    for attempt in {1..12}; do
        if curl --fail --silent --show-error --noproxy '*' --connect-timeout 5 --max-time 10 \
            --resolve "$public_host:$public_port:$resolve_ip" "$public_api_origin/api/health" |
            python3 -c 'import json,sys; data=json.load(sys.stdin); assert data["status"] == "ok" and "account-login" in data["capabilities"]' 2>/dev/null; then
            public_ok=1
            break
        fi
        sleep 5
    done
    ((public_ok)) || die 'public HTTPS gateway did not pass its health check'
fi

persist_image_tag "$commit"
env_tag_updated=1
next_link="$deploy_home/.current-$commit-$$"
ln -s -- "$release" "$next_link"
mv -Tf -- "$next_link" "$current"
needs_rollback=0
printf 'DataMark deployed commit %s; database backup: %s\n' "$commit" "$backup"
