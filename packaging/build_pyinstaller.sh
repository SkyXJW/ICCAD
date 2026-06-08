#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

rm -rf build dist submission

pyinstaller --clean --noconfirm packaging/cada1078_alpha.spec

mkdir -p submission/app submission/bin submission/configs submission/abc_resources
cp -r dist/cada1078_alpha_dist submission/app/
cp packaging/cada1078_alpha submission/cada1078_alpha
chmod +x submission/cada1078_alpha

cp configs/contest.yml submission/configs/contest.yml
cp abc_resources/abc.rc submission/abc_resources/abc.rc
cp abc_resources/my.genlib submission/abc_resources/my.genlib
cp mcp_tools_spec.json submission/mcp_tools_spec.json

copy_tool() {
  local name="$1"
  if command -v "$name" >/dev/null 2>&1; then
    cp "$(command -v "$name")" "submission/bin/$name"
    chmod +x "submission/bin/$name"
  fi
}

copy_tool yosys
copy_tool yosys-abc
copy_tool abc
copy_tool iverilog

if [ -d /out ]; then
  rm -rf /out/submission
  cp -r submission /out/submission
fi

printf 'Submission generated at %s/submission\n' "$ROOT"
