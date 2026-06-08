#!/usr/bin/env bash
set -euo pipefail

cd /src
mkdir -p /src/packaging/out/build /src/packaging/out/dist
pyinstaller --clean --noconfirm \
  --distpath /src/packaging/out/dist \
  --workpath /src/packaging/out/build \
  /src/packaging/cada1078_alpha.spec
chmod +x /src/packaging/out/dist/cada1078_alpha
