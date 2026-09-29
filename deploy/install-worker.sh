#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

deployment_root=/home/relty/services/datamark-worker
marker=datamark-worker-managed-v1
stage=${1:?A controlled staging directory is required}
case "$stage" in /home/relty/services/.datamark-upload.*) ;; *) echo 'Invalid staging directory' >&2; exit 1 ;; esac
test -d "$stage" && test ! -L "$stage"
test "$(id -u)" = 1000 && test "$(id -g)" = 1000 || { echo 'Expected relty UID/GID 1000' >&2; exit 1; }
docker compose version >/dev/null
docker info >/dev/null
for share in homes datasets collector-data docker; do
    mountpoint -q "/mnt/nas/$share" || { echo "NAS mount is unavailable: $share" >&2; exit 1; }
    test -r "/mnt/nas/$share" && test -x "/mnt/nas/$share"
done
test ! -L "$deployment_root" || { echo 'Deployment root must not be a symlink' >&2; exit 1; }
if test -e "$deployment_root"; then
    test -f "$deployment_root/.datamark-worker-managed" && \
        test "$(cat "$deployment_root/.datamark-worker-managed")" = "$marker" || \
        { echo 'Existing deployment directory is not managed by Datamark; stopped' >&2; exit 1; }
else
    mkdir -m 700 "$deployment_root"
    printf '%s\n' "$marker" > "$deployment_root/.datamark-worker-managed"
fi
exec 9>"$deployment_root/.deploy.lock"
flock -n 9 || { echo 'Another deployment is in progress' >&2; exit 1; }
for directory in releases state secrets; do
    test ! -L "$deployment_root/$directory" || { echo 'Managed directories cannot be symlinks' >&2; exit 1; }
    mkdir -p -m 700 "$deployment_root/$directory"
done

previous_release=''
if test -L "$deployment_root/current"; then
    previous_release=$(readlink -f "$deployment_root/current")
    case "$previous_release" in "$deployment_root"/releases/*) ;; *) echo 'Invalid current release link' >&2; exit 1 ;; esac
    test -f "$previous_release/deploy/runtime.env"
elif test -e "$deployment_root/current"; then
    echo 'Current release must be a managed symlink' >&2
    exit 1
fi

managed_container=$(docker ps -aq --filter label=com.docker.compose.project=datamark-worker --filter label=com.docker.compose.service=worker)
if test -n "$managed_container" && test -z "$previous_release"; then
    echo 'An untracked Datamark container already exists; stopped' >&2
    exit 1
fi
if test -n "$(ss -H -ltn 'sport = :18120')"; then
    test -n "$managed_container" && \
        docker inspect --format '{{range (index .HostConfig.PortBindings "18120/tcp")}}{{.HostIp}}:{{.HostPort}}{{end}}' "$managed_container" | grep -qx '127.0.0.1:18120' || \
        { echo 'Port 18120 is already occupied by another service' >&2; exit 1; }
fi

test -f "$stage/payload/worker-token" && test -f "$stage/payload/worker-key.pub"
grep -Eq '^[0-9a-f]{64}$' "$stage/payload/worker-token" || { echo 'Invalid worker token' >&2; exit 1; }
test ! -L "$deployment_root/secrets/worker-token" || { echo 'Worker token must not be a symlink' >&2; exit 1; }
if test -e "$deployment_root/secrets/worker-token"; then
    cmp -s "$stage/payload/worker-token" "$deployment_root/secrets/worker-token" || \
        { echo 'Local and server worker tokens differ; stopped without rotating credentials' >&2; exit 1; }
else
    install -m 600 "$stage/payload/worker-token" "$deployment_root/secrets/worker-token"
fi

release_id=$(cat "$stage/payload/release-id")
[[ "$release_id" =~ ^[0-9]{8}T[0-9]{6}-[0-9a-f]{12}$ ]] || { echo 'Invalid release identifier' >&2; exit 1; }
release="$deployment_root/releases/$release_id"
test ! -e "$release"
mkdir -m 700 "$release"
cp -a "$stage/backend" "$stage/deploy" "$stage/requirements.lock.txt" "$release/"
printf 'DATAMARK_WORKER_HOME=%s\nDATAMARK_WORKER_IMAGE_TAG=%s\n' "$deployment_root" "$release_id" > "$release/deploy/runtime.env"

compose_at() {
    local selected=$1
    shift
    docker compose --project-name datamark-worker --env-file "$selected/deploy/runtime.env" -f "$selected/deploy/compose.yaml" "$@"
}
activated=0
rollback() {
    result=$?
    trap - EXIT
    if test "$result" -ne 0 && test "$activated" = 1; then
        echo 'Deployment failed; restoring the previous container.' >&2
        if test -n "$previous_release"; then
            compose_at "$previous_release" up -d --no-build --wait --wait-timeout 90 || \
                echo 'Previous container recovery failed; manual review is required.' >&2
        else
            compose_at "$release" down || true
        fi
    fi
    exit "$result"
}
trap rollback EXIT

echo 'Building the isolated Python/FFmpeg runtime.'
compose_at "$release" config --quiet
compose_at "$release" build
activated=1
echo 'Starting the worker and waiting for authenticated health checks.'
compose_at "$release" up -d --no-build --wait --wait-timeout 120

read -r key_type key_data key_comment < "$stage/payload/worker-key.pub"
test "$key_type" = ssh-ed25519 && test "$key_comment" = datamark-worker || \
    { echo 'Unexpected deployment public key' >&2; exit 1; }
[[ "$key_data" =~ ^[A-Za-z0-9+/=]+$ ]] || { echo 'Malformed deployment public key' >&2; exit 1; }
test ! -L "$HOME/.ssh" && test ! -L "$HOME/.ssh/authorized_keys" || \
    { echo 'SSH authorization path cannot be a symlink' >&2; exit 1; }
mkdir -p -m 700 "$HOME/.ssh"
touch "$HOME/.ssh/authorized_keys"
chmod 700 "$HOME/.ssh"
chmod 600 "$HOME/.ssh/authorized_keys"
authorization="restrict,port-forwarding,permitopen=\"127.0.0.1:18120\",command=\"/bin/false\" ssh-ed25519 $key_data datamark-worker"
if ! grep -qxF "$authorization" "$HOME/.ssh/authorized_keys"; then
    if grep -qF "$key_data" "$HOME/.ssh/authorized_keys"; then
        echo 'This public key already has different authorization; stopped.' >&2
        exit 1
    fi
    printf '\n%s\n' "$authorization" >> "$HOME/.ssh/authorized_keys"
fi
ln -s "releases/$release_id" "$deployment_root/.current-$release_id"
mv -Tf "$deployment_root/.current-$release_id" "$deployment_root/current"
activated=0
echo "DATAMARK_DEPLOY_READY $release_id"
