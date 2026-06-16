#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BUILD_DIR="${REPO_ROOT}/packaging/out/build"
DIST_DIR="${REPO_ROOT}/packaging/out/dist"
EDA_BUNDLE_DIR="${REPO_ROOT}/packaging/out/eda_bundle"

require_tool() {
  local name="$1"
  local path
  path="$(command -v "${name}" || true)"
  if [[ -z "${path}" ]]; then
    echo "error: required EDA tool not found on build host: ${name}" >&2
    exit 1
  fi
  readlink -f "${path}"
}

copy_exec() {
  local src="$1"
  local dst="$2"
  cp -L "${src}" "${dst}"
  chmod +x "${dst}"
}

stage_ldd_libs() {
  local exe
  for exe in "$@"; do
    ldd "${exe}" | while IFS= read -r line; do
      local lib=""
      if [[ "${line}" =~ \=\>\ (/[^[:space:]]+) ]]; then
        lib="${BASH_REMATCH[1]}"
      elif [[ "${line}" =~ ^[[:space:]]*(/[^[:space:]]+) ]]; then
        lib="${BASH_REMATCH[1]}"
      fi
      [[ -n "${lib}" ]] || continue
      case "$(basename "${lib}")" in
        ld-linux-*|libc.so.*|libpthread.so.*|libdl.so.*|libm.so.*|librt.so.*|libresolv.so.*)
          continue
          ;;
      esac
      cp -L "${lib}" "${EDA_BUNDLE_DIR}/lib64/"
    done
  done
}

stage_eda_bundle() {
  local yosys_bin berkeley_abc_bin yosys_abc_bin iverilog_bin vvp_bin
  yosys_bin="$(require_tool yosys)"
  berkeley_abc_bin="$(require_tool berkeley-abc)"
  yosys_abc_bin="$(command -v yosys-abc || true)"
  if [[ -n "${yosys_abc_bin}" ]]; then
    yosys_abc_bin="$(readlink -f "${yosys_abc_bin}")"
  else
    yosys_abc_bin="${berkeley_abc_bin}"
  fi
  iverilog_bin="$(require_tool iverilog)"
  vvp_bin="$(require_tool vvp)"

  if [[ ! -d /usr/share/yosys ]]; then
    echo "error: required Yosys data directory not found: /usr/share/yosys" >&2
    exit 1
  fi
  if [[ ! -d /usr/lib/x86_64-linux-gnu/ivl ]]; then
    echo "error: required Icarus Verilog ivl directory not found: /usr/lib/x86_64-linux-gnu/ivl" >&2
    exit 1
  fi

  rm -rf "${EDA_BUNDLE_DIR}"
  mkdir -p \
    "${EDA_BUNDLE_DIR}/bin" \
    "${EDA_BUNDLE_DIR}/share" \
    "${EDA_BUNDLE_DIR}/lib" \
    "${EDA_BUNDLE_DIR}/lib64"

  copy_exec "${yosys_bin}" "${EDA_BUNDLE_DIR}/bin/yosys"
  copy_exec "${berkeley_abc_bin}" "${EDA_BUNDLE_DIR}/bin/berkeley-abc"
  copy_exec "${yosys_abc_bin}" "${EDA_BUNDLE_DIR}/bin/yosys-abc"
  copy_exec "${berkeley_abc_bin}" "${EDA_BUNDLE_DIR}/bin/abc"
  copy_exec "${iverilog_bin}" "${EDA_BUNDLE_DIR}/bin/iverilog.real"
  copy_exec "${vvp_bin}" "${EDA_BUNDLE_DIR}/bin/vvp"

  cp -a /usr/share/yosys "${EDA_BUNDLE_DIR}/share/yosys"
  cp -a /usr/lib/x86_64-linux-gnu/ivl "${EDA_BUNDLE_DIR}/lib/ivl"

  stage_ldd_libs \
    "${EDA_BUNDLE_DIR}/bin/yosys" \
    "${EDA_BUNDLE_DIR}/bin/berkeley-abc" \
    "${EDA_BUNDLE_DIR}/bin/yosys-abc" \
    "${EDA_BUNDLE_DIR}/bin/iverilog.real" \
    "${EDA_BUNDLE_DIR}/bin/vvp"

  echo "Staged EDA bundle at ${EDA_BUNDLE_DIR}"
}

mkdir -p "${BUILD_DIR}" "${DIST_DIR}"
stage_eda_bundle
export CADA_EDA_BUNDLE_DIR="${EDA_BUNDLE_DIR}"

pyinstaller --clean --noconfirm \
  --distpath "${DIST_DIR}" \
  --workpath "${BUILD_DIR}" \
  "${REPO_ROOT}/packaging/cada1078_alpha.spec"
chmod +x "${DIST_DIR}/cada1078_alpha"
cp "${DIST_DIR}/cada1078_alpha" "${REPO_ROOT}/cada1078_alpha"
chmod +x "${REPO_ROOT}/cada1078_alpha"
