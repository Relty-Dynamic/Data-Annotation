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
public_bind_ip="$(sed -n 's/^DATAMARK_PUBLIC_BIND_IP=//p' "$env_file" | tail -n 1)"
[[ "$configured_home" == "$deploy_home" ]] || die 'DATAMARK_HOME does not match the deployment directory'
[[ "$host" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die 'DATAMARK_HOST must be the internal IPv4 address'
[[ -z "$public_origin" && -z "$public_bind_ip" || -n "$public_origin" && -n "$public_bind_ip" ]] ||
    die 'DATAMARK_PUBLIC_ORIGIN and DATAMARK_PUBLIC_BIND_IP must be set together'
if [[ -n "$public_origin" ]]; then
    public_host="$(python3 - "$public_origin" "$public_bind_ip" "$host" <<'PY'
import ipaddress
import json
import subprocess
import sys
from urllib.parse import urlsplit

origin, bind_ip, intranet_host = sys.argv[1:]
parsed = urlsplit(origin)
if (parsed.scheme != "https" or not parsed.hostname or parsed.port is not None
        or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
        or parsed.netloc != parsed.netloc.lower() or "*" in parsed.netloc
        or any(char.isspace() for char in origin)):
    raise SystemExit("DATAMARK_PUBLIC_ORIGIN must be one exact HTTPS hostname")
labels = parsed.hostname.split(".")
if (len(labels) < 2 or len(parsed.hostname) > 253
        or any(not 1 <= len(label) <= 63 or not label[0].isalnum() or not label[-1].isalnum()
               or any(not (character.isascii() and (character.isalnum() or character == "-"))
                      for character in label) for label in labels)):
    raise SystemExit("DATAMARK_PUBLIC_ORIGIN must use a valid DNS hostname")
address = ipaddress.ip_address(bind_ip)
if address.is_loopback or address.is_unspecified or bind_ip == intranet_host:
    raise SystemExit("DATAMARK_PUBLIC_BIND_IP must be a separate local interface address")
interfaces = json.loads(subprocess.check_output(["ip", "-j", "address", "show"]))
if bind_ip not in {item["local"] for interface in interfaces for item in interface.get("addr_info", [])}:
    raise SystemExit("DATAMARK_PUBLIC_BIND_IP is not assigned to this server")
print(parsed.hostname)
PY
)" || die 'invalid public gateway settings'
    [[ -d "$deploy_home/public-caddy-data" && -d "$deploy_home/public-caddy-config" ]] ||
        die 'persistent public Caddy directories are missing'
fi

previous="$(readlink -f "$current")"
[[ "$previous" == "$deploy_home"/releases/* && -f "$previous/deploy/compose.intranet.yaml" ]] || die 'current symlink points outside a valid release'
compose_files=(-f "$deploy_home/releases/$commit/deploy/compose.intranet.yaml")
previous_compose_files=(-f "$previous/deploy/compose.intranet.yaml")
if [[ -n "$public_origin" ]]; then
    [[ -f "$previous/deploy/compose.public.yaml" ]] ||
        die 'deploy an intranet-only release with public gateway support before enabling its public settings'
    compose_files+=(-f "$deploy_home/releases/$commit/deploy/compose.public.yaml")
    previous_compose_files+=(-f "$previous/deploy/compose.public.yaml")
fi
previous_image="$(docker inspect datamark-intranet-web-1 --format '{{.Config.Image}}')"
[[ "$previous_image" == datamark-web:* ]] || die 'running web image is unexpected'
previous_tag="${previous_image#datamark-web:}"

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
on_exit() {
    status=$?
    trap - EXIT
    if ((status != 0 && needs_rollback)); then
        printf 'DataMark deployment failed; restoring prior containers from %s\n' "$previous" >&2
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
if [[ -n "$public_origin" ]]; then
    resolve_ip="$public_bind_ip"
    [[ "$resolve_ip" != *:* ]] || resolve_ip="[$resolve_ip]"
    public_ok=0
    for attempt in {1..12}; do
        if curl --fail --silent --show-error --noproxy '*' --connect-timeout 5 --max-time 10 \
            --resolve "$public_host:443:$resolve_ip" "$public_origin/api/health" |
            python3 -c 'import json,sys; data=json.load(sys.stdin); assert data["status"] == "ok" and "account-login" in data["capabilities"]' 2>/dev/null; then
            public_ok=1
            break
        fi
        sleep 5
    done
    ((public_ok)) || die 'public HTTPS gateway did not pass its health check'
fi

next_link="$deploy_home/.current-$commit-$$"
ln -s -- "$release" "$next_link"
mv -Tf -- "$next_link" "$current"
needs_rollback=0
printf 'DataMark deployed commit %s; database backup: %s\n' "$commit" "$backup"
