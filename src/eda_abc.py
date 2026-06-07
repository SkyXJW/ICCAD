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
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ir import NetlistIR, Cell


# ---------------------------------------------------------------------------
# Configuration (override via environment variables)
# ---------------------------------------------------------------------------
ABC_BIN = os.environ.get("ABC_BIN", "abc")
ABC_CEC_BIN = os.environ.get("ABC_CEC_BIN", "")

# Default to the project's bundled ABC resources so the repo is self-contained.
# Users can still override via env vars if they want a custom genlib/abc.rc.
_HERE = Path(__file__).resolve().parent
_BUNDLED = _HERE.parent / "abc_resources"
_DEFAULT_GENLIB = _BUNDLED / "my.genlib"
_DEFAULT_ABC_RC = _BUNDLED / "abc.rc"

GENLIB_PATH = os.environ.get("GENLIB_PATH") or (str(_DEFAULT_GENLIB) if _DEFAULT_GENLIB.exists() else "")
ABC_RC_PATH = os.environ.get("ABC_RC_PATH") or (str(_DEFAULT_ABC_RC) if _DEFAULT_ABC_RC.exists() else "")
ABC_TIMEOUT = int(os.environ.get("ABC_TIMEOUT", "120"))  # seconds per abc call

COMB_GATES = {"and", "or", "nand", "nor", "not", "buf", "xor", "xnor"}
CONSTANTS = {"1'b0", "1'b1"}


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
    if ABC_CEC_BIN:
        return ABC_CEC_BIN
    candidate = shutil.which("yosys-abc")
    if candidate:
        return candidate
    abc_path = Path(ABC_BIN)
    if abc_path.name == "abc":
        sibling = abc_path.parent.parent / "bin" / "yosys-abc"
        if sibling.exists():
            return str(sibling)
    default_oss = Path("/home/zzj/tools/oss-cad-suite/bin/yosys-abc")
    if default_oss.exists():
        return str(default_oss)
    return ABC_BIN


def _run_abc(script: str, workdir: Path, *, abc_bin: Optional[str] = None) -> str:
    """Run a single `abc -c "<script>"` and return combined stdout/stderr."""
    binary = abc_bin or ABC_BIN
    if shutil.which(binary) is None:
        raise RuntimeError(
            f"abc binary not found (looked for {binary!r}). "
            f"Install berkeley-abc or set ABC_BIN."
        )
    proc = subprocess.run(
        [binary, "-c", script],
        cwd=str(workdir),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=ABC_TIMEOUT,
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
) -> Dict[str, Any]:
    """Optimize the combinational logic for depth using ABC, rebuild the IR.

    This is the function that fills the stubbed reduce_depth(). It:
      1. lowers the comb frame to BLIF (DFFs cut)
      2. runs: source abc.rc; strash; <recipe>; dch; map -D K
      3. reads optimized gate-level Verilog back and rebuilds comb cells
      4. keeps every DFF cell exactly as-is

    NOTE: step 3 (reading ABC's mapped output back into this exact IR and
    re-stitching the DFFs) is the part you will finish tomorrow -- the BLIF
    lowering, ABC invocation, and stats parsing below are ready. The rebuild
    is left as a clearly marked TODO because it must match pyv_extractor's
    naming so the round-trip stays lossless.
    """
    from eda_transform import _max_depth  # local import to avoid cycle

    before_depth = _max_depth(ir)

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        src = work / "in.blif"
        src.write_text(ir_to_comb_blif(ir, model="top", preserve_dff_control=True))

        k = max_depth if max_depth is not None else 0
        map_cmd = f"map -D {k}" if k > 0 else "map"

        # Source abc.rc for resyn2/dch aliases if provided.
        prelude = f"source {ABC_RC_PATH}; " if ABC_RC_PATH else ""
        lib = f"read_library {GENLIB_PATH}; " if GENLIB_PATH else ""

        script = (
            f"{prelude}{lib}"
            f"read {src.name}; strash; print_stats; "
            f"{recipe}; dch; {map_cmd}; "
            f"write_verilog out.v; print_stats"
        )
        out = _run_abc(script, work)
        opt_v = work / "out.v"
        opt_text = opt_v.read_text() if opt_v.exists() else ""

    # ABC prints stats twice: after strash (before) and after map (after).
    all_stats = [(int(m.group(1)), int(m.group(2))) for m in _STATS_RE.finditer(out)]
    abc_depth_before = all_stats[0][1] if all_stats else None
    gates_after, depth_after = all_stats[-1] if all_stats else (None, None)

    result = {
        "status": "optimized" if opt_text else "kept_original",
        "original_depth": abc_depth_before if abc_depth_before is not None else before_depth,
        "depth": depth_after if depth_after is not None else before_depth,
        "target_depth": max_depth,
        "abc_depth_before": abc_depth_before,
        "abc_gates_after": gates_after,
        "recipe": recipe,
        "message": out.strip()[:400],
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


def rebuild_comb_from_abc(ir: NetlistIR, abc_verilog: str) -> Dict[str, Any]:
    """Replace the IR's combinational cells with ABC's optimized gates.

    DFFs are preserved untouched. The IR's signals (ports + buses) are kept;
    new internal wires introduced by ABC (new_nXXX) are declared as scalar
    wires. After this, design_write() will serialize the optimized netlist.

    IMPORTANT: this mutates ``ir`` in place and calls rebuild_indices().
    Validate with cec_equivalent(ir, original) right after calling this.
    """
    new_cells, _ = parse_abc_verilog(abc_verilog)

    # 1. keep DFFs, drop all combinational cells
    dff_names = [n for n in ir.cell_order if ir.cells[n].cell_type == "dff"]
    dff_cells = {n: ir.cells[n] for n in dff_names}

    ir.cells.clear()
    ir.cell_order.clear()
    for n in dff_names:
        ir.cells[n] = dff_cells[n]
        ir.cell_order.append(n)

    # 2. add ABC's combinational gates, declaring any unseen internal wires
    known_nets = set(ir.nets)
    added = 0
    for c in new_cells:
        out = c["output"]
        # declare any net ABC invented (e.g. new_n219) as a scalar wire
        for net in [out, *c["inputs"].values()]:
            if net in CONSTANTS:
                continue
            if net not in known_nets and "[" not in net:
                ir.add_signal(net, "wire")
                known_nets.add(net)

        port_order = ["Y", "A"] if c["type"] in ("buf", "not") else ["Y", "A", "B"]
        cell = Cell(
            name=c["inst"],
            cell_type=c["type"],
            inputs=c["inputs"],
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

    ir.rebuild_indices()
    return {"comb_cells_added": added, "dffs_preserved": len(dff_names)}


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
