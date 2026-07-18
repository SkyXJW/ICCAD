"""ABC backend for the contest agent.

Two jobs live here, both built on Berkeley ABC via subprocess:

1. cec_equivalent(ir_a, ir_b)
   Real combinational equivalence checking (CEC). Each IR is lowered to BLIF,
   sequential boundaries (DFFs) are CUT at Q/D so the comparison is purely
   combinational, and `abc -c "cec a.blif b.blif"` decides equivalence.
   This replaces the text-only design_equivalence() so a transformed netlist
   that LOOKS different can still be proven functionally identical -- which is
   what the contest's "no unintended functional change" hard requirement needs.

2. reduce_depth_abc(ir, max_depth=None)
   Real depth optimization. Lowers the combinational part to BLIF, runs the
   ABC recipe validated on test23 (strash; resyn2; dch; map -D K), reads the
   optimized gate-level netlist back, and rebuilds the IR's combinational cells
   while keeping every DFF untouched. Returns before/after depth + gate counts.

DFF-cut model (all contest DFFs are 5-port: RN/SN/CK/D/Q):
  * each DFF.Q net becomes a PRIMARY INPUT of the combinational frame
  * each DFF.D net becomes a PRIMARY OUTPUT of the combinational frame
  * RN/SN/CK are clock/reset control -- ignored for combinational equivalence
  Two netlists are sequentially equivalent iff their combinational frames are
  combinationally equivalent AND the DFF state mapping matches. Because both
  sides share the SAME DFFs (transforms never touch DFFs), the Q->D frame
  comparison is sufficient here.

Assumptions / things to set for your machine:
  * ABC_BIN: path to the abc binary (default "abc", found on PATH)
  * GENLIB_PATH: your 8-gate my.genlib (only needed by reduce_depth_abc's map)
  * ABC_RC_PATH: abc.rc that defines resyn2/dch aliases (source'd inside abc -c)
Both reduce_depth_abc knobs are read from env vars so you don't hardcode paths.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ir import NetlistIR, Cell


# ---------------------------------------------------------------------------
# Configuration (override via environment variables)
# ---------------------------------------------------------------------------
ABC_CANDIDATES: Tuple[str, ...] = ("abc", "berkeley-abc", "yosys-abc")


def _resource_root() -> Path:
    """Return the project/resource root in source and PyInstaller-frozen runs."""
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root)
    return Path(__file__).resolve().parent.parent


# Default to the project's bundled ABC resources so the repo is self-contained.
# Users can still override via env vars if they want a custom genlib/abc.rc.
def _bundled_resource(name: str) -> Path:
    return _resource_root() / "abc_resources" / name


def _env_path(env_name: str, bundled_name: str) -> Optional[Path]:
    override = os.environ.get(env_name)
    if override:
        return Path(override).expanduser()
    candidate = _bundled_resource(bundled_name)
    return candidate if candidate.exists() else None


def _abc_timeout() -> int:
    return int(os.environ.get("ABC_TIMEOUT", "120"))


# best-of 候选循环的墙钟预算（秒）。
#
# 赛题 3.1 / Q&A A18 / A21.5 三处口径一致：非基本操作每条 prompt 限 300 秒。
# 候选池从 4 扩到 10 之后（2026-07-10，换来 6 降 3 平、7/9 追平反超 DC），每条优化
# prompt 的耗时线性上涨。实测 test33#19（64,430 门）：
#     v01(4 候选) 86.7s -> v02 183.5s -> v03 263.9s -> 2026-07-15 389.6s
# 同一份代码、同一份输入，两次运行相差 48%（机器负载/散热）。也就是说这条题过不过
# 300 秒红线取决于当天机器的心情；v09_llm 那趟的 327s 已经实打实违规（status 却是
# ok —— 本地 watchdog 是 600s，只有 timing.csv 的 over_spec 列会说实话）。
#
# best-of 的语义本来就是「取跑出来的最好的那个」，因此中途停下只是候选少几个：
# 结果只会不如满跑，绝不会比不做更差，也不可能违反任何硬约束（CEC 与「未变小则回退」
# 闸门都在下游，与本预算正交）。
#
# 设 0 或负数 = 不限（完全恢复扩池后的旧行为）。默认 180s，给 blif 写出、deepcopy
# 快照、rebuild、CEC 留出余量（它们都不在本预算内）。
#
# 2026-07-16：240 -> 180。原来 240 的假设是"后处理约 60s"，但锥语义修好之后，
# 锥题的 _gate_library_satisfied 不再真空成立，会真的去跑 replace_gate_library +
# 第二次 CEC —— test33#19 实测 236s -> 267s，只剩 33s 余量（267/300 = 89%），
# TSRI 机器慢一点就超时归零。
# 180 的代价 ≈ 0：全 40 题里只有 test33#19 会碰到这个预算（第二慢的 test28#7
# 整条请求才 90s、它的 ABC 循环约 85s，要慢到 2.1 倍才会碰到 180），而 test33#19
# 本来就是 kept_original（整设计 ABC 压不动 n8 那个 17 门的锥，见 reduce_depth 的
# 说明），少跑几个候选一分不损。改完 test33#19 约 217s，余量 83s。
_DEFAULT_BESTOF_BUDGET_SEC = 180.0


def _bestof_budget() -> float:
    raw = os.environ.get("ABC_BESTOF_BUDGET_SEC", "").strip()
    if not raw:
        return _DEFAULT_BESTOF_BUDGET_SEC
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_BESTOF_BUDGET_SEC


def _bundled_eda_bin(name: str) -> Optional[str]:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if not frozen_root:
        return None
    path = Path(frozen_root) / "eda" / "bin" / name
    if path.exists() and os.access(path, os.X_OK):
        return str(path)
    return None


def _resolve_executable(env_name: str, candidates: Tuple[str, ...]) -> Optional[str]:
    override = os.environ.get(env_name)
    names = (override,) if override else candidates
    for name in names:
        if not name:
            continue
        found = shutil.which(name)
        if found:
            return found
        path = Path(name).expanduser()
        if path.exists() and os.access(path, os.X_OK):
            return str(path.resolve())
        bundled = _bundled_eda_bin(Path(name).name)
        if bundled:
            return bundled
    if not override:
        for name in candidates:
            bundled = _bundled_eda_bin(Path(name).name)
            if bundled:
                return bundled
    return None


def _abc_binary() -> str:
    binary = _resolve_executable("ABC_BIN", ABC_CANDIDATES)
    if binary:
        return binary
    raise RuntimeError(
        "abc binary not found (tried ABC_BIN, abc, berkeley-abc, yosys-abc). "
        "Install Berkeley ABC or set ABC_BIN."
    )


def _copy_resource_to_workdir(resource: Optional[Path], workdir: Path, filename: str) -> Optional[str]:
    if resource is None:
        return None
    if not resource.exists():
        return None
    dest = workdir / filename
    shutil.copyfile(resource, dest)
    return dest.name


COMB_GATES = {"and", "or", "nand", "nor", "not", "buf", "xor", "xnor"}
CONSTANTS = {"1'b0", "1'b1"}


# ---------------------------------------------------------------------------
# Restricted gate-library genlibs for constrained depth optimization.
# When a depth task also says "remains AND and NOT only", optimizing on the FULL
# 9-gate library and THEN decomposing to AND/NOT re-introduces levels (an OR or
# XOR expands into several AND/NOT stages), wiping out the depth win. Instead we
# hand ABC a genlib that ONLY contains the allowed gates, so 'if -g' minimizes
# depth *already inside that library* and no post-hoc decomposition is needed.
# ---------------------------------------------------------------------------
_GENLIB_HEADER = "GATE zero    0 O=CONST0;\nGATE one     0 O=CONST1;\n"
_GENLIB_GATES = {
    "buf":   "GATE buf     1 O=a;           PIN * NONINV 1 999 1 0 1 0\n",
    "inv":   "GATE inv     1 O=!a;          PIN * INV    1 999 1 0 1 0\n",
    "and2":  "GATE and2    1 O=a*b;         PIN * NONINV 1 999 1 0 1 0\n",
    "or2":   "GATE or2     1 O=a+b;         PIN * NONINV 1 999 1 0 1 0\n",
    "nand2": "GATE nand2   1 O=!(a*b);      PIN * INV    1 999 1 0 1 0\n",
    "nor2":  "GATE nor2    1 O=!(a+b);      PIN * INV    1 999 1 0 1 0\n",
}
# Which 2-input gates each restricted library is allowed to use (besides inv/buf,
# which every library keeps so ABC can always realize an inverter).
_LIB_GATESET = {
    "and_not":     ["and2"],
    "or_not":      ["or2"],
    "and_or_not":  ["and2", "or2"],
    "nand_not":    ["nand2"],
    "nor_not":     ["nor2"],
}


def _restricted_genlib_text(library: str) -> Optional[str]:
    """Build a genlib string containing only the gates allowed by `library`.

    Returns None for an unknown library (caller then falls back to full genlib).
    Every restricted library includes const0/const1, inv AND buf.

    关于 buf（2026-07 修正，实测推翻了原先的假设）：
    原实现把 buf 故意排除在 genlib 之外，注释称 "ABC 会用两级反相器或直接连线实现
    pass-through，从而保证结果严格落在受限门库内"。**该假设是错的。**
    实测（test28, and_not）：当某个组合帧输出（这里是若干 DFF 的 D 端）在优化后
    等价于一个 PI(直通) 时，genlib 里没有 buf，ABC 既不会用两级反相器、也不会直接连线，
    而是把该输出**留成悬空**（write_verilog 里只在端口列表出现、无任何门驱动、也无 assign）。
    读回 IR 后这些网没有驱动 -> ABC 在 CEC 时给悬空网补常量 0 -> 功能改变 -> 判不等价
    -> 整个优化被回滚（test28 报 65->65 kept_original 即此因；实测 5 个悬空端点）。

    因此这里把 buf 放回 genlib：它只用于让 ABC 有能力**表达** pass-through。
    读回时 rebuild_comb_from_abc() 会用网络别名把 buf 全部消掉（见该函数），
    所以最终网表里不会残留 buf，门库约束依旧严格满足。
    实测 test28：加 buf 后悬空端点 5 -> 1（仅剩原网表自带的 floating PO n10，
    snapshot 侧同样悬空，两边一致故 CEC 通过），CEC 由 not_equivalent 变 equivalent，
    深度 135 -> 56。
    """
    gates = _LIB_GATESET.get(library)
    if gates is None:
        return None
    body = _GENLIB_HEADER + _GENLIB_GATES["inv"] + _GENLIB_GATES["buf"]
    for g in gates:
        body += _GENLIB_GATES[g]
    return body


# ---------------------------------------------------------------------------
# BLIF lowering: IR (with DFFs cut at Q/D) -> combinational BLIF
# ---------------------------------------------------------------------------
def _blif_name(net: str) -> str:
    """Sanitize a net name into a BLIF/ABC-legal identifier.

    BLIF identifiers cannot contain '[' ']' or quotes. We use a token-based
    scheme (``__bo__`` / ``__bc__``) instead of plain underscores so the
    mapping is INVERTIBLE and does not collide with real names that already
    contain underscores. ABC passes plain alnum+underscore identifiers through
    unchanged, so these tokens survive the round-trip and let us map ABC's
    output nets back to the original IR net names exactly.

      n8[15] -> n8__bo__15__bc__
      1'b0   -> __const0
    """
    if net == "1'b0":
        return "__const0"
    if net == "1'b1":
        return "__const1"
    return net.replace("[", "__bo__").replace("]", "__bc__").replace("'", "_")


def _unblif_name(name: str) -> str:
    """Inverse of _blif_name for the bus-bit tokens (constants handled separately)."""
    if name == "__const0":
        return "1'b0"
    if name == "__const1":
        return "1'b1"
    return name.replace("__bo__", "[").replace("__bc__", "]")


# Truth tables for each gate type as BLIF .names ON-set rows.
# Pin order is [A, B] for binary gates, [A] for unary.
_GATE_ONSET: Dict[str, Tuple[int, List[str]]] = {
    "and":  (2, ["11 1"]),
    "or":   (2, ["1- 1", "-1 1"]),
    "nand": (2, ["0- 1", "-0 1"]),
    "nor":  (2, ["00 1"]),
    "xor":  (2, ["10 1", "01 1"]),
    "xnor": (2, ["00 1", "11 1"]),
    "buf":  (1, ["1 1"]),
    "not":  (1, ["0 1"]),
}


def _collect_dff_boundaries(ir: NetlistIR) -> Tuple[List[str], List[str]]:
    """Return (q_nets, d_nets): DFF Q nets become PIs, D nets become POs."""
    q_nets: List[str] = []
    d_nets: List[str] = []
    for cell in ir.cells.values():
        if cell.cell_type != "dff":
            continue
        for pin, net in cell.outputs.items():       # Q
            q_nets.append(net)
        if "D" in cell.inputs:
            d_nets.append(cell.inputs["D"])
    return q_nets, d_nets


def _collect_dff_control_nets(ir: NetlistIR) -> List[str]:
    """DFF 的 CK/RN/SN（时钟/异步复位/置位）所连的网线（排除常量）。

    组合帧默认只把 DFF.D 当输出，喂给 CK/RN/SN 的逻辑会被 ABC 当死代码删掉。当限扇出
    先给时钟/复位插了缓冲树（DFF.CK/RN/SN 改接内部缓冲网线）后再跑 reduce_depth，这些
    缓冲就被 abc 删除 → 重建后 DFF 的时钟/复位/置位失驱（test24 等）。把这些内部网线也
    列为帧输出，ABC 便会保留其驱动逻辑，重建后 DFF 控制脚仍有正确驱动。"""
    nets: List[str] = []
    for cell in ir.cells.values():
        if cell.cell_type != "dff":
            continue
        for pin in ("CK", "RN", "SN"):
            net = cell.inputs.get(pin)
            if net and net not in ("1'b0", "1'b1"):
                nets.append(net)
    return nets


def _stable_dff_pin_name(cell_name: str, pin: str) -> str:
    return f"__obs__dff__{_blif_name(cell_name)}__{pin}"


def _stable_dff_q_name(cell_name: str) -> str:
    return f"__state__dff__{_blif_name(cell_name)}__Q"


def _stable_po_name(net: str) -> str:
    return f"__obs__po__{_blif_name(net)}"


def ir_to_comb_blif(
    ir: NetlistIR,
    model: str = "top",
    *,
    preserve_dff_control: bool = False,
    stable_observation_names: bool = False,
) -> str:
    """Lower the combinational portion of an IR to BLIF.

    DFFs are cut: their Q is a PI, their D is a PO. Primary inputs/outputs of
    the design are added to the PI/PO lists as well.

    preserve_dff_control: 额外把 DFF 的 CK/RN/SN 内部网线也列为帧输出，使 ABC 不会把喂给
    时钟/异步复位/置位的逻辑（如限扇出插入的缓冲树）当死代码删除（reduce_depth 用，防控制
    脚失驱）。CEC 不开此项，以免变换重命名这些网线后两侧 PO 名不一致而误判。
    """
    # Primary IO bit nets
    pi_bits: List[str] = []
    po_bits: List[str] = []
    for name in ir.signal_order:
        sig = ir.signals[name]
        bits = _signal_bits(ir, name)
        if sig.direction == "input":
            pi_bits.extend(bits)
        elif sig.direction == "output":
            po_bits.extend(bits)

    q_nets, d_nets = _collect_dff_boundaries(ir)
    net_name_override: Dict[str, str] = {}
    observation_outputs: List[Tuple[str, str]] = []

    if stable_observation_names:
        q_nets = []
        for cell_name in ir.cell_order:
            cell = ir.cells.get(cell_name)
            if cell is None or cell.cell_type != "dff":
                continue
            q_net = next(iter(cell.outputs.values()), None)
            if q_net:
                stable_q = _stable_dff_q_name(cell_name)
                net_name_override.setdefault(q_net, stable_q)
                q_nets.append(stable_q)
            for pin in ("D", "CK", "RN", "SN"):
                src = cell.inputs.get(pin)
                if src is not None:
                    observation_outputs.append((_stable_dff_pin_name(cell_name, pin), src))

    def blif_net(net: str) -> str:
        return _blif_name(net_name_override.get(net, net))

    # Frame PIs = design PIs + DFF Q ; Frame POs = design POs + DFF D
    # 去重（保序）：有些网表用多个 DFF.Q 驱动同一根网（带 set/reset 的寄存器变体），
    # 不去重会让 .inputs 出现重复项，触发 ABC 的 Abc_ObjAddFanin 断言。
    def _dedupe(seq):
        seen, out = set(), []
        for x in seq:
            if x not in seen:
                seen.add(x); out.append(x)
        return out
    inputs = _dedupe(pi_bits + q_nets)
    extra_outputs: List[str] = []
    if preserve_dff_control:
        in_set = set(inputs)
        # 只保留“内部网线驱动”的 CK/RN/SN（已是 PI/Q 的本就不会被删，无需作输出）。
        extra_outputs = [n for n in _collect_dff_control_nets(ir) if n not in in_set]
    if stable_observation_names:
        observation_outputs = [(_stable_po_name(n), n) for n in po_bits] + observation_outputs
        outputs = _dedupe([name for name, _ in observation_outputs])
    else:
        outputs = _dedupe(po_bits + d_nets + extra_outputs)

    lines: List[str] = []
    lines.append(f".model {model}")
    lines.append(".inputs " + " ".join(blif_net(n) for n in inputs))
    lines.append(".outputs " + " ".join(_blif_name(n) for n in outputs))
    # constants
    lines.append(".names __const0")
    lines.append(".names __const1")
    lines.append("1")

    for cell_name in ir.cell_order:
        cell = ir.cells.get(cell_name)
        if cell is None or cell.cell_type == "dff":
            continue
        if cell.cell_type not in _GATE_ONSET:
            raise ValueError(f"cannot lower gate type {cell.cell_type!r} to BLIF")
        arity, onset = _GATE_ONSET[cell.cell_type]
        out = blif_net(cell.outputs["Y"])
        if arity == 1:
            ins = [blif_net(cell.inputs["A"])]
        else:
            ins = [blif_net(cell.inputs["A"]), blif_net(cell.inputs["B"])]
        lines.append(".names " + " ".join(ins) + " " + out)
        lines.extend(onset)

    if stable_observation_names:
        for obs_name, src_net in observation_outputs:
            lines.append(".names " + blif_net(src_net) + " " + _blif_name(obs_name))
            lines.append("1 1")

    lines.append(".end")
    return "\n".join(lines) + "\n"


def _signal_bits(ir: NetlistIR, name: str) -> List[str]:
    sig = ir.signals.get(name)
    if sig is None or sig.width == 1:
        return [name]
    lo, hi = min(sig.msb, sig.lsb), max(sig.msb, sig.lsb)
    step = 1 if sig.lsb >= sig.msb else -1
    return [f"{name}[{i}]" for i in range(sig.msb, sig.lsb + step, step)]


# ---------------------------------------------------------------------------
# 1. CEC equivalence
# ---------------------------------------------------------------------------
def _abc_for_cec() -> str:
    override = _resolve_executable("ABC_CEC_BIN", ())
    if override:
        return override

    yosys_abc = shutil.which("yosys-abc")
    if yosys_abc:
        return yosys_abc

    abc = _abc_binary()
    abc_path = Path(abc).resolve()
    for sibling in (
        abc_path.with_name("yosys-abc"),
        abc_path.parent.parent / "bin" / "yosys-abc",
    ):
        if sibling.exists() and os.access(sibling, os.X_OK):
            return str(sibling)
    return abc


def _run_abc(script: str, workdir: Path, *, abc_bin: Optional[str] = None) -> str:
    """Run a single `abc -c "<script>"` and return combined stdout/stderr."""
    binary = abc_bin or _abc_binary()
    proc = subprocess.run(
        [binary, "-c", script],
        cwd=str(workdir),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=_abc_timeout(),
        check=False,
    )
    return proc.stdout


def cec_equivalent(ir_a: NetlistIR, ir_b: Optional[NetlistIR]) -> Dict[str, Any]:
    """Combinational equivalence check between two IRs via ABC cec.

    Returns:
      {"status": "equivalent"|"not_equivalent"|"unknown",
       "equivalent": True|False|None,
       "method": "abc-cec",
       "message": "<abc output excerpt>"}
    """
    if ir_b is None:
        return {"status": "unknown", "equivalent": None, "method": "abc-cec",
                "message": "reference design is not available"}

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        a_blif = work / "a.blif"
        b_blif = work / "b.blif"
        a_blif.write_text(ir_to_comb_blif(ir_a, model="top", stable_observation_names=True))
        b_blif.write_text(ir_to_comb_blif(ir_b, model="top", stable_observation_names=True))

        out = _run_abc(f'cec {a_blif.name} {b_blif.name}', work, abc_bin=_abc_for_cec())

    low = out.lower()
    if "are equivalent" in low or "networks are equivalent" in low:
        return {"status": "equivalent", "equivalent": True,
                "method": "abc-cec", "message": out.strip()[:400]}
    if "are not equivalent" in low or "not equivalent" in low:
        return {"status": "not_equivalent", "equivalent": False,
                "method": "abc-cec", "message": out.strip()[:400]}
    # different #PI/#PO, parse error, etc.
    return {"status": "unknown", "equivalent": None,
            "method": "abc-cec", "message": out.strip()[:400]}


# ---------------------------------------------------------------------------
# 2. Depth optimization
# ---------------------------------------------------------------------------
_STATS_RE = re.compile(r"(?:and|nd)\s*=\s*(\d+).*?lev\s*=\s*(\d+)", re.IGNORECASE)


def _parse_stats(abc_output: str) -> Tuple[Optional[int], Optional[int]]:
    """Pull (gate_count, depth) from the last print_stats line."""
    gates = depth = None
    for m in _STATS_RE.finditer(abc_output):
        gates, depth = int(m.group(1)), int(m.group(2))
    return gates, depth


def reduce_depth_abc(
    ir: NetlistIR,
    max_depth: Optional[int] = None,
    recipe: str = "resyn2",
    library: Optional[str] = None,
) -> Dict[str, Any]:
    """Optimize the combinational logic for depth using ABC, rebuild the IR.

    When `library` is one of the restricted sets (and_not / or_not / and_or_not /
    nand_not / nor_not), ABC maps onto a genlib containing ONLY those gates, so
    the depth it reports is already achievable within the required library and no
    post-hoc decomposition is needed. When `library` is None the full 9-gate
    genlib is used (original behaviour).
    """
    from eda_transform import _max_depth  # local import to avoid cycle

    before_depth = _max_depth(ir)

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        src = work / "in.blif"
        src.write_text(ir_to_comb_blif(ir, model="top", preserve_dff_control=True))

        k = max_depth if max_depth is not None else 0

        # Source abc.rc for resyn2/dch aliases if provided. Copy resources into
        # the temporary ABC cwd so the ABC command script never depends on an
        # absolute path (or on quoting paths with spaces from PyInstaller _MEIPASS).
        rc_name = _copy_resource_to_workdir(_env_path("ABC_RC_PATH", "abc.rc"), work, "abc.rc")

        # Pick the genlib: restricted set if the task constrains the library,
        # else the bundled full 9-gate genlib.
        restricted = _restricted_genlib_text(library) if library else None
        if restricted is not None:
            (work / "lib.genlib").write_text(restricted)
            genlib_name = "lib.genlib"
        else:
            genlib_name = _copy_resource_to_workdir(_env_path("GENLIB_PATH", "my.genlib"), work, "my.genlib")

        prelude = f"source {rc_name}; " if rc_name else ""
        lib = f"read_library {genlib_name}; " if genlib_name else ""

        # Depth-oriented flow with MULTI-RECIPE best-of selection.
        # A single recipe (resyn2) sometimes fails to reduce depth on a given
        # circuit (e.g. test28). Different AIG-optimization scripts reach different
        # local optima, so we try several, map each with depth-oriented 'if -g',
        # and keep the candidate with the SMALLEST final depth. Runs comfortably
        # in the single-threaded 300s budget for these circuits.
        map_tail = f"if -g; map -D {k}" if k > 0 else "if -g; map"
        # Iterated depth push: a second global-delay restructuring + depth map
        # round often peels extra levels on unconstrained cases (test22/23/24),
        # where a single pass leaves ABC 2-5 levels behind DC.
        map_iter = (
            f"dch -f; if -g; map -D {k}; st; dch -f; if -g; map -D {k}"
            if k > 0 else
            "dch -f; if -g; map; st; dch -f; if -g; map"
        )
        # v2: an even deeper 3-round iterate for the last levels on the cases that
        # were still 1-6 behind DC (test22/24/26/28).
        map_iter3 = (
            f"dch -f; if -g; map -D {k}; st; dch -f; if -g; map -D {k}; st; dch -f; if -g; map -D {k}"
            if k > 0 else
            "dch -f; if -g; map; st; dch -f; if -g; map; st; dch -f; if -g; map"
        )

        # Candidate opt-scripts (the middle of the ABC run). Each yields one
        # mapped netlist; we keep the SMALLEST-depth (then smallest-gate) result.
        # The first four are the ORIGINAL portfolio, kept byte-for-byte so every
        # case already won reproduces exactly. The rest are stronger, iterated
        # depth pushers ADDED to the best-of pool: because selection is best-of
        # AND each run is wrapped in try/except, adding candidates can only lower
        # or hold the depth, never raise it -- cases ABC already wins cannot
        # regress, and a candidate using a command absent in this ABC build is
        # simply skipped.
        if recipe and recipe != "resyn2":
            candidates = [f"{recipe}; dch; {map_tail}"]
        else:
            candidates = [
                # --- original portfolio (byte-for-byte, reproduces v0 results) ---
                f"resyn2; dch; {map_tail}",
                f"resyn2rs; dch; {map_tail}",
                f"compress2rs; dch; {map_tail}",
                f"resyn3; dch; {map_tail}",
                # --- v1 iterated pushers ---
                f"resyn2; resyn2; {map_iter}",
                f"compress2rs; resyn2; {map_iter}",
                f"dc2; {map_iter}",
                # --- v2 push-to-the-limit (deeper iterate + GIA '&' engines) ---
                f"resyn2; resyn2; resyn2; {map_iter3}",
                f"dc2; dc2; {map_iter3}",
                f"&get -n; &dch -f; &if -g -K 6; &put; {map_iter}",
            ]

        abc_depth_before = None
        best_depth = None
        best_gates = None
        best_text = ""
        best_recipe = None
        best_out_log = ""

        # 墙钟预算（见 _bestof_budget 的说明）。不变量：
        #   1) 第一个候选永远跑完 —— 一个结果都没有比候选少几个糟糕得多；
        #   2) 只在「已经握着一个可用结果」时才提前收工；
        #   3) 用已跑候选的最大耗时预估下一个，装不下就不开工（而不是开了再被砍），
        #      这样总耗时才真的被 budget 兜住，而不是 budget + 一个候选的时长。
        _budget = _bestof_budget()
        _t_pool = time.monotonic()
        _cand_secs: List[float] = []
        _ran = 0
        _stopped_early = False

        for idx, rec in enumerate(candidates):
            if _budget > 0 and best_text and _cand_secs:
                _elapsed = time.monotonic() - _t_pool
                _est_next = max(_cand_secs)  # 保守估计：按最慢的那个算
                if _elapsed + _est_next > _budget:
                    _stopped_early = True
                    break
            out_name = f"out{idx}.v"
            script = (
                f"{prelude}{lib}"
                f"read {src.name}; strash; print_stats; "
                f"{rec}; "
                f"write_verilog {out_name}; print_stats"
            )
            _t_cand = time.monotonic()
            try:
                out = _run_abc(script, work)
            except Exception:
                continue
            finally:
                # 失败/超时的候选同样消耗了墙钟，必须计入预估。
                _cand_secs.append(time.monotonic() - _t_cand)
                _ran += 1
            stats = [(int(m.group(1)), int(m.group(2))) for m in _STATS_RE.finditer(out)]
            if not stats:
                continue
            if abc_depth_before is None:
                abc_depth_before = stats[0][1]
            cand_gates, cand_depth = stats[-1]
            cand_path = work / out_name
            cand_text = cand_path.read_text() if cand_path.exists() else ""
            if not cand_text:
                continue
            if (best_depth is None
                    or cand_depth < best_depth
                    or (cand_depth == best_depth and cand_gates < (best_gates or 1 << 30))):
                best_depth, best_gates = cand_depth, cand_gates
                best_text, best_recipe, best_out_log = cand_text, rec, out

        opt_text = best_text
        out = best_out_log
        _pool_seconds = time.monotonic() - _t_pool

    gates_after, depth_after = best_gates, best_depth

    result = {
        "status": "optimized" if opt_text else "kept_original",
        "original_depth": abc_depth_before if abc_depth_before is not None else before_depth,
        "depth": depth_after if depth_after is not None else before_depth,
        "target_depth": max_depth,
        "abc_depth_before": abc_depth_before,
        "abc_gates_after": gates_after,
        "recipe": best_recipe or recipe,
        # best-of 诊断：跑了几个候选 / 是否因预算提前收工 / 候选池总耗时。
        # 只是记录，不参与任何判断。
        "bestof_candidates_total": len(candidates),
        "bestof_candidates_run": _ran,
        "bestof_stopped_early": _stopped_early,
        "bestof_seconds": round(_pool_seconds, 2),
        "message": (out or "").strip()[:400],
        "_optimized_verilog": opt_text,
    }
    return result


# ---------------------------------------------------------------------------
# Read ABC's mapped Verilog back into the IR (combinational region only)
# ---------------------------------------------------------------------------
# genlib cell name (ABC mapper output) -> IR cell_type
_ABC_CELL_TO_TYPE = {
    "and2": "and", "or2": "or", "nand2": "nand", "nor2": "nor",
    "xor2": "xor", "xnor2": "xnor", "inv": "not", "buf": "buf",
    # some genlibs name the buffer differently:
    "buff": "buf", "not1": "not",
    # genlib 的常量单元（_GENLIB_HEADER 里始终提供）。它们不是 IR 的 cell_type，
    # 用哨兵类型透传给 rebuild_comb_from_abc()，由它把消费者直接接到 1'b0/1'b1。
    # 旧实现没有这两项 -> ABC 一旦真的吐出 zero/one，解析器会静默跳过 ->
    # 该网失去驱动 -> 悬空 -> CEC 补常量0 -> 不等价 -> 优化被回滚（与 buf 同一类雷）。
    "zero": "__const0__", "one": "__const1__",
}

_INST_RE = re.compile(
    r"^\s*([A-Za-z0-9_]+)\s+([A-Za-z0-9_]+)\s*\((.*?)\)\s*;",
    re.MULTILINE | re.DOTALL,
)
_PIN_RE = re.compile(r"\.([A-Za-z0-9_]+)\s*\(\s*([A-Za-z0-9_]+)\s*\)")


def parse_abc_verilog(text: str) -> Tuple[List[Dict[str, Any]], str]:
    """Parse ABC `write_verilog` gate-level output.

    Returns (cells, module_name) where each cell dict is:
        {"type": <ir cell_type>, "inst": <abc inst name>,
         "inputs": {"A": net, "B": net?}, "output": net}
    Net names are mapped back through _unblif_name so bus bits / constants are
    restored to IR form (n8__bo__15__bc__ -> n8[15]).
    """
    mod_m = re.search(r"\bmodule\s+([A-Za-z0-9_]+)", text)
    module_name = mod_m.group(1) if mod_m else "top"

    cells: List[Dict[str, Any]] = []
    for m in _INST_RE.finditer(text):
        cell_kw, inst_name, pin_blob = m.group(1), m.group(2), m.group(3)
        if cell_kw == "module" or cell_kw not in _ABC_CELL_TO_TYPE:
            continue  # skip the module header and anything not a known gate
        ir_type = _ABC_CELL_TO_TYPE[cell_kw]

        pins = {pin: net for pin, net in _PIN_RE.findall(pin_blob)}
        # ABC mapper convention: .a/.b inputs, .O output (case-insensitive).
        out_net = None
        in_nets = {}
        for pin, net in pins.items():
            pl = pin.lower()
            net_ir = _unblif_name(net)
            if pl in ("o", "y", "out"):
                out_net = net_ir
            elif pl == "a":
                in_nets["A"] = net_ir
            elif pl == "b":
                in_nets["B"] = net_ir
            else:
                in_nets[pin.upper()] = net_ir
        if out_net is None:
            raise ValueError(f"instance {inst_name}: no output pin in {pins}")
        cells.append({"type": ir_type, "inst": inst_name,
                      "inputs": in_nets, "output": out_net})
    return cells, module_name


def _design_output_bits(ir: NetlistIR) -> set:
    """设计主输出的 bit-net 名集合（这些网必须有驱动，不能靠别名消掉）。"""
    bits = set()
    for sig in ir.signals.values():
        if sig.direction != "output":
            continue
        if sig.width == 1:
            bits.add(sig.name)
        else:
            lo, hi = min(sig.msb, sig.lsb), max(sig.msb, sig.lsb)
            for i in range(lo, hi + 1):
                bits.add(f"{sig.name}[{i}]")
    return bits


def rebuild_comb_from_abc(ir: NetlistIR, abc_verilog: str) -> Dict[str, Any]:
    """Replace the IR's combinational cells with ABC's optimized gates.

    DFFs are preserved untouched. The IR's signals (ports + buses) are kept;
    new internal wires introduced by ABC (new_nXXX) are declared as scalar
    wires. After this, design_write() will serialize the optimized netlist.

    IMPORTANT: this mutates ``ir`` in place and calls rebuild_indices().
    Validate with cec_equivalent(ir, original) right after calling this.

    buf / 常量单元的消解（2026-07 新增）：
    genlib 里提供 buf 与 zero/one，只是为了让 ABC 有能力**表达** "某输出直通某个 PI"
    或 "某输出恒为常量"（否则 ABC 会把该输出留成悬空，见 _restricted_genlib_text 的说明）。
    但受限门库题（如 "AND and NOT only"）不允许最终网表里出现 buf。
    因此这里在读回时就把它们消解掉，最终网表只含真正的逻辑门：
      * buf(dst <- src)：dst 不是设计 PO -> 网络别名，把所有消费者（含 DFF 引脚）
        直接重连到 src，buf 本身丢弃 —— 不加门、不加深度。
      * zero/one(dst)：dst 不是设计 PO -> 消费者直接接 1'b0 / 1'b1。
      * dst 是设计 PO（必须有驱动，不能只做别名）：
          - 直通 -> 用两级 NOT 实现（NOT 在所有受限库与全库中均合法）；
          - 常量 -> 用一级 NOT 取反相反的常量实现。
        这两种情况只影响极少数网（test28 实测仅 1 个 buf），深度代价可忽略。
    """
    new_cells, _ = parse_abc_verilog(abc_verilog)

    # ---- 1. 先把 buf / 常量单元挑出来，建立别名表与常量表（不进 IR）
    alias: Dict[str, str] = {}      # dst -> src（直通）
    const_of: Dict[str, str] = {}   # dst -> "1'b0" / "1'b1"
    gate_cells: List[Dict[str, Any]] = []
    for c in new_cells:
        ctype = c["type"]
        if ctype == "__const0__":
            const_of[c["output"]] = "1'b0"
        elif ctype == "__const1__":
            const_of[c["output"]] = "1'b1"
        elif ctype == "buf":
            src = next(iter(c["inputs"].values()), None)
            if src is not None:
                alias[c["output"]] = src
        else:
            gate_cells.append(c)

    def resolve(net: str) -> str:
        """顺着别名链走到真正的驱动源（可能落到常量）。"""
        seen = set()
        while True:
            if net in const_of:
                return const_of[net]
            if net in alias and net not in seen:
                seen.add(net)
                net = alias[net]
                continue
            return net

    # ---- 2. 保留 DFF，清掉旧的组合 cell
    # 先记录"替换前哪些网是有驱动的"。原始网表允许存在 floating PO（A5.6 明确说明会有
    # floating / unconnected ports，如 test28 的 n10、test25 的 n13[0..3]）。这些 PO 必须
    # 保持悬空：若给它们凭空补一个驱动（哪怕是常量 0），功能就被改变了。
    # 注意 ABC 的 cec 会给两边的悬空网都补常量 0，因此它看不出这种差别；只有独立的
    # verify_equiv（拿 _out.v 与原始 .v 直接仿真）才会抓到。故这里必须自己守住。
    driven_before = {net for c in ir.cells.values() for net in c.outputs.values()}

    dff_names = [n for n in ir.cell_order if ir.cells[n].cell_type == "dff"]
    dff_cells = {n: ir.cells[n] for n in dff_names}

    ir.cells.clear()
    ir.cell_order.clear()
    for n in dff_names:
        ir.cells[n] = dff_cells[n]
        ir.cell_order.append(n)

    known_nets = set(ir.nets)

    def _declare(net: str) -> None:
        if net in CONSTANTS:
            return
        if net not in known_nets and "[" not in net:
            ir.add_signal(net, "wire")
            known_nets.add(net)

    # ---- 3. 加入 ABC 的逻辑门（输入端顺着别名/常量解析）
    added = 0
    for c in gate_cells:
        out = c["output"]
        ins = {pin: resolve(net) for pin, net in c["inputs"].items()}
        _declare(out)
        for net in ins.values():
            _declare(net)

        port_order = ["Y", "A"] if c["type"] in ("buf", "not") else ["Y", "A", "B"]
        cell = Cell(
            name=c["inst"],
            cell_type=c["type"],
            inputs=ins,
            outputs={"Y": out},
            port_order=port_order,
            src="abc-mapped",
        )
        # avoid name clash with a surviving DFF
        if cell.name in ir.cells:
            cell = Cell(name=f"{cell.name}_c", cell_type=cell.cell_type,
                        inputs=cell.inputs, outputs=cell.outputs,
                        port_order=cell.port_order, src=cell.src)
        ir.add_cell(cell)
        added += 1

    # ---- 4. DFF 引脚同样顺着别名/常量重连（这一步修好了 test28：
    #        原先 DFF.D 指向的网因 buf 被丢弃而悬空 -> 补常量0 -> CEC 不等价）
    for name in dff_names:
        cell = ir.cells[name]
        for pin, net in list(cell.inputs.items()):
            r = resolve(net)
            if r != net:
                _declare(r)
                cell.inputs[pin] = r

    # ---- 5. 设计 PO 必须有真实驱动，不能只做别名 -> 用 NOT 门兑现
    #        例外：替换前就没有驱动的 PO（原网表自带的 floating port）保持悬空，
    #        绝不能凭空补驱动，否则功能改变（test28 n10 / test25 n13[*] 即此坑）。
    po_bits = _design_output_bits(ir)
    fixed_po = 0
    kept_floating = 0

    # sorted()（2026-07 新增）：_design_output_bits 返回 set，而 CPython 默认开启 str hash
    # 随机化(PYTHONHASHSEED=random)，直接迭代 set 会让下面这些修复门的编号在不同进程间漂移
    # ——同一份输入两次运行会写出门名/顺序不同的 _out.v。功能与门数、深度都不受影响，
    # 但赛题第 1 节明确要求 deterministic 行为，且这会给逐格比对制造噪声。固定顺序即可。
    for po in sorted(po_bits):
        if po not in driven_before:
            kept_floating += 1
            continue  # 原本就是 floating PO -> 保持悬空
        r = resolve(po)
        if r == po:
            continue  # 未被别名/常量化：已有门驱动
        if r in CONSTANTS:
            # 常量 PO：not(相反常量) -> 目标常量
            opposite = "1'b1" if r == "1'b0" else "1'b0"
            ir.add_cell(Cell(name=f"abc_po_const_{fixed_po}", cell_type="not",
                             inputs={"A": opposite}, outputs={"Y": po},
                             port_order=["Y", "A"], src="abc-po-fix"))
        else:
            # 直通 PO：两级 NOT（保持门库合规，不引入 buf）
            mid = f"abc_po_buf_{fixed_po}"
            ir.add_signal(mid, "wire")
            known_nets.add(mid)
            _declare(r)
            ir.add_cell(Cell(name=f"abc_po_inv_a_{fixed_po}", cell_type="not",
                             inputs={"A": r}, outputs={"Y": mid},
                             port_order=["Y", "A"], src="abc-po-fix"))
            ir.add_cell(Cell(name=f"abc_po_inv_b_{fixed_po}", cell_type="not",
                             inputs={"A": mid}, outputs={"Y": po},
                             port_order=["Y", "A"], src="abc-po-fix"))
        fixed_po += 1

    ir.rebuild_indices()
    return {"comb_cells_added": added, "dffs_preserved": len(dff_names),
            "bufs_aliased": len(alias), "consts_resolved": len(const_of),
            "po_drivers_fixed": fixed_po, "po_kept_floating": kept_floating}


# ---------------------------------------------------------------------------
# Self-test (build a tiny IR and lower it, no ABC needed)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    ir = NetlistIR(module_name="top")
    ir.add_constant_nets()
    ir.add_signal("a", "input")
    ir.add_signal("b", "input")
    ir.add_signal("y", "output")
    ir.add_signal("n1", "wire")
    ir.port_order = ["a", "b", "y"]
    ir.add_cell(Cell(name="U1", cell_type="and",
                     inputs={"A": "a", "B": "b"}, outputs={"Y": "n1"},
                     port_order=["Y", "A", "B"]))
    ir.add_cell(Cell(name="U2", cell_type="buf",
                     inputs={"A": "n1"}, outputs={"Y": "y"},
                     port_order=["Y", "A"]))
    ir.rebuild_indices()
    print(ir_to_comb_blif(ir))
