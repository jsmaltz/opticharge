#!/bin/sh
set -eu

BASE_DIR=${BASE_DIR:-/apps/opticharge}
APP_DIR=${APP_DIR:-$BASE_DIR/app}
VENV_DIR=${VENV_DIR:-$BASE_DIR/venv}
SERVICE_NAME=${SERVICE_NAME:-opticharge.service}
REMOTE=${REMOTE:-origin}
BRANCH=${BRANCH:-main}

if [ "$(id -u)" != "0" ]; then
    echo "Run as root on the ReadyNAS." >&2
    exit 1
fi

if [ ! -d "$APP_DIR/.git" ]; then
    echo "Expected git checkout at $APP_DIR" >&2
    exit 1
fi

if [ ! -x "$VENV_DIR/bin/python" ]; then
    echo "Expected venv Python at $VENV_DIR/bin/python" >&2
    echo "Run deploy/readynas/install-runtime.sh first." >&2
    exit 1
fi

cd "$APP_DIR"

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "Refusing to update with local tracked changes:" >&2
    git status --short >&2
    exit 1
fi

old_head=$(git rev-parse --short HEAD)
git fetch "$REMOTE" "$BRANCH"
git merge --ff-only "$REMOTE/$BRANCH"
new_head=$(git rev-parse --short HEAD)

"$VENV_DIR/bin/python" -m pip install -r requirements.txt
"$VENV_DIR/bin/python" -m pip check
"$VENV_DIR/bin/python" -m py_compile opticharge.py readteslaonly.py

systemctl restart "$SERVICE_NAME"
sleep 5
systemctl is-active "$SERVICE_NAME"

echo "Updated $APP_DIR from $old_head to $new_head and restarted $SERVICE_NAME."
echo "Watch logs with: journalctl -u $SERVICE_NAME -f"
