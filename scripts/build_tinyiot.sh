#!/usr/bin/env bash
# Build the edge MN-CSE: tinyIoT at a pinned commit + this project's patches + MN configuration.
#
#   scripts/build_tinyiot.sh [target_dir]        (default: ../tinyIoT next to this repository)
#
# Needs: git, gcc, make, pkg-config, libsqlite3-dev, libwebsockets-dev, libssl-dev
#   sudo apt install -y build-essential pkg-config libsqlite3-dev libwebsockets-dev libssl-dev
# Environment overrides: MN_PORT (3001), IN_HOST (127.0.0.1), IN_PORT (7599)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:-$REPO/../tinyIoT}"
COMMIT=c140495
MN_PORT="${MN_PORT:-3001}"; IN_HOST="${IN_HOST:-127.0.0.1}"; IN_PORT="${IN_PORT:-7599}"

if [[ ! -d "$DEST/.git" ]]; then
  git clone -q https://github.com/seslabSJU/tinyIoT.git "$DEST"
fi
cd "$DEST"
git checkout -q "$COMMIT"
git checkout -q -- source/server        # start from pristine sources so the script can be re-run
for p in "$REPO"/patches/*.patch; do
  echo "apply $(basename "$p")"
  git apply "$p"
done

cd source/server
sed -i \
 -e 's|^#define SERVER_TYPE IN_CSE|#define SERVER_TYPE MN_CSE|' \
 -e "s|^#define SERVER_PORT \"3000\"|#define SERVER_PORT \"$MN_PORT\"|" \
 -e 's|^#define CSE_BASE_NAME "TinyIoT"|#define CSE_BASE_NAME "EdgeMN"|' \
 -e 's|^#define CSE_BASE_RI "tinyiot"|#define CSE_BASE_RI "EdgeMN"|' \
 -e 's|^#define CSE_BASE_SP_ID "tinyiot.example.com"|#define CSE_BASE_SP_ID "icing.edge"|' \
 -e 's|^#define REMOTE_CSE_ID ""|#define REMOTE_CSE_ID "/Mobius4"|' \
 -e 's|^#define REMOTE_CSE_NAME ""|#define REMOTE_CSE_NAME "Mobius"|' \
 -e "s|^#define REMOTE_CSE_HOST \"\"|#define REMOTE_CSE_HOST \"$IN_HOST\"|" \
 -e 's|^#define REMOTE_CSE_SP_ID ""|#define REMOTE_CSE_SP_ID "icing.cloud"|' \
 -e "s|^#define REMOTE_CSE_PORT 0|#define REMOTE_CSE_PORT $IN_PORT|" \
 -e 's|^// #define DB_TYPE DB_SQLITE|#define DB_TYPE DB_SQLITE|' \
 -e 's|^#define DB_TYPE DB_POSTGRESQL|// #define DB_TYPE DB_POSTGRESQL|' \
 -e 's|^#define ALLOWED_REMOTE_CSE_ID "/id-in,/id-mn"|#define ALLOWED_REMOTE_CSE_ID "/*"|' \
 config.h
grep -q '^#define INTEROP_R2A_CSR' config.h || sed -i 's|^#define CSE_RVI RVI_3|#define CSE_RVI RVI_3\n#define INTEROP_R2A_CSR|' config.h

echo "config:"; grep -E '^#define (SERVER_TYPE|SERVER_PORT|CSE_BASE_NAME|CSE_BASE_RI|REMOTE_CSE_ID|REMOTE_CSE_HOST|REMOTE_CSE_PORT|DB_TYPE|INTEROP_R2A_CSR)' config.h | sed 's/^/  /'
make clean > /dev/null 2>&1 || true
make CFLAGS="$(pkg-config --cflags libwebsockets)" > build.log 2>&1 || { tail -20 build.log; exit 1; }
echo "built $(pwd)/server"
echo "run:   cd $(pwd) && ./server     (start Mobius4 first so the MN can register)"
