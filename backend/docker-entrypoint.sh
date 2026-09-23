#!/bin/sh
set -eu

secret_path="${VERDA_ENV_FILE:-/run/secrets/backend_env}"
if [ ! -f "$secret_path" ]; then
    echo "Backend secret file is missing" >&2
    exit 1
fi

# Compose 的文件型 Secret 继承宿主机权限；复制到 tmpfs 后再降权。
install -m 0400 -o verda -g verda "$secret_path" /tmp/verda.env
export VERDA_ENV_FILE=/tmp/verda.env

exec setpriv --reuid=10001 --regid=10001 --init-groups "$@"
