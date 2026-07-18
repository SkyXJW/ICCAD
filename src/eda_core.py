from __future__ import annotations

import copy
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import networkx as nx

from ir import NetlistIR
from nx_probe import build_comb_graph, cell_node, net_node, node_to_readable
from pyv_extractor import parse_verilog_to_ir
from eda_transform import (
    _cone_seed,
    boolean_expression,
    collapse_back_to_back_inverters,
    articulation_points_between,
    cone_gate_type_count,
    constant_propagation,
    cut_or_articulation,
    deepest_output,
    design_equivalence,
    dff_enable_hold_structures,
    dffs_by_clock,
    find_nand_equivalent,
    floating_or_unconnected,
    gate_info,
    gate_on_max_depth_path,
    largest_fanin_cone_output,
    limit_fanout,
    list_gates_by_type,
    max_fanout,
    merge_functionally_equivalent_gates,
    merge_structural_duplicates,
    outputs_depth_over,
    pi_to_dff_depth,
    primary_io_summary,
    reconnect_gate_input,
    reduce_depth,
    register_path_depth,
    register_paths,
    remove_dangling_logic,
    rename_identifier,
    replace_gate_library,
    shared_fanin_cone,
    signal_constant,
    signal_dependency,
    signal_symmetry,
    write_ir,
    zero_length_paths,
)


GATE_COUNT_ORDER = ["and", "or", "not", "nand", "nor", "xor", "xnor", "buf", "dff"]
COMB_GATES = {"and", "or", "nand", "nor", "not", "buf", "xor", "xnor"}
NUM_RE = re.compile(r"(\d+)")


def _bundled_eda_bin(name: str) -> Optional[str]:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if not frozen_root:
        return None
    path = Path(frozen_root) / "eda" / "bin" / name
    if path.exists() and os.access(path, os.X_OK):
        return str(path)
    return None


def _resolve_yosys_bin() -> Optional[str]:
    override = os.environ.get("YOSYS_BIN")
    candidates = (override,) if override else ("yosys",)
    for name in candidates:
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
    return _bundled_eda_bin("yosys")


def natural_key(text: str):
    return [int(part) if part.isdigit() else part for part in NUM_RE.split(text)]


def range_indices(msb: int, lsb: int) -> List[int]:
    step = 1 if lsb >= msb else -1
    return list(range(msb, lsb + step, step))


def sort_by_order(items: Iterable[str], order: Dict[str, int]) -> List[str]:
    return sorted(set(items), key=lambda name: (order.get(name, 10**12), natural_key(name)))


@dataclass
class EquivalenceResult:
    status: str
    method: str
    support_size: int
    support: List[str]
    message: str
    counterexample: Optional[Dict[str, int]] = None


@dataclass
class DesignSession:
    design_id: str
    netlist_path: Path
    analyzer: "CircuitAnalyzer"
    original_ir: Optional[NetlistIR] = None
    pre_transform_ir: Optional[NetlistIR] = None
    modified: bool = False
    last_stats: Dict[str, Any] = field(default_factory=dict)

    @property
    def ir(self) -> NetlistIR:
        return self.analyzer.ir


_DESIGN_SESSIONS: Dict[str, DesignSession] = {}


def _ok(tool: str, **payload: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {"ok": True, "tool": tool}
    result.update(payload)
    return result


def _get_session(design_id: str = "current") -> DesignSession:
    if design_id not in _DESIGN_SESSIONS:
        raise KeyError(f"design is not loaded: {design_id}")
    return _DESIGN_SESSIONS[design_id]


def _refresh_session(session: DesignSession) -> None:
    session.ir.rebuild_indices()
    session.analyzer = CircuitAnalyzer(
        session.ir,
        miter_dir=session.analyzer.miter_dir,
        yosys_timeout=session.analyzer.yosys_timeout,
    )


def _mark_transform(session: DesignSession, stats: Dict[str, Any]) -> Dict[str, Any]:
    session.modified = True
    session.last_stats = stats
    _refresh_session(session)
    return stats


class CircuitAnalyzer:
    def __init__(
        self,
        ir: NetlistIR,
        *,
        miter_dir: Optional[Path] = None,
        yosys_timeout: int = 240,
    ):
        self.ir = ir
        self._graph = None  # 懒加载：首次被分析用到时才构建，见 graph property
        self.cell_order = {name: idx for idx, name in enumerate(ir.cell_order)}
        self.miter_dir = miter_dir
        self.yosys_timeout = yosys_timeout

    @property
    def graph(self):
        # 一连串变换之间不会触发任何重建；图只在下一次需要它的分析时构建一次。
        if self._graph is None:
            self._graph = build_comb_graph(self.ir)
        return self._graph

    def signal_bits(self, name: str) -> List[str]:
        if name in self.ir.nets:
            return [name]

        sig = self.ir.signals.get(name)
        if sig is None:
            raise KeyError(f"unknown net or signal: {name}")

        if sig.width == 1:
            return [name]

        return [f"{name}[{i}]" for i in range_indices(sig.msb, sig.lsb)]

    def normalize_node(self, name: str) -> str:
        if name in self.ir.nets:
            return net_node(name)
        if name in self.ir.cells:
            return cell_node(name)
        if name in self.ir.signals and self.ir.signals[name].width == 1:
            return net_node(name)
        raise KeyError(f"unknown net/cell name: {name}")

    def gate_counts(self) -> Dict[str, int]:
        counts = {gate: 0 for gate in GATE_COUNT_ORDER}
        for cell in self.ir.cells.values():
            counts[cell.cell_type] = counts.get(cell.cell_type, 0) + 1
        return counts

    def total_gate_count(self) -> int:
        return len(self.ir.cells)

    def direct_net_fanout_gates(self, net_or_signal: str) -> List[str]:
        cells: List[str] = []
        for net in self.signal_bits(net_or_signal):
            cells.extend(ref.cell for ref in self.ir.loads.get(net, []))
        return sort_by_order(cells, self.cell_order)

    def gate_successors(self, cell_name: str) -> List[str]:
        if cell_name not in self.ir.cells:
            raise KeyError(f"unknown cell: {cell_name}")

        successors: List[str] = []
        cell = self.ir.cells[cell_name]
        for out_net in cell.outputs.values():
            successors.extend(ref.cell for ref in self.ir.loads.get(out_net, []))

        return [name for name in sort_by_order(successors, self.cell_order) if name != cell_name]

    def path_exists(self, src: str, dst: str) -> bool:
        s = self.normalize_node(src)
        t = self.normalize_node(dst)
        return nx.has_path(self.graph, s, t)

    def avoid_path_exists(self, src: str, dst: str, avoid: str) -> bool:
        s = self.normalize_node(src)
        t = self.normalize_node(dst)
        a = self.normalize_node(avoid)

        graph = self.graph.copy()
        if a in graph:
            graph.remove_node(a)
        return nx.has_path(graph, s, t)

    def _relevant_subgraph(self, src: str, dst: str):
        s = self.normalize_node(src)
        t = self.normalize_node(dst)

        if not nx.has_path(self.graph, s, t):
            return None, s, t

        relevant = (nx.descendants(self.graph, s) | {s}) & (nx.ancestors(self.graph, t) | {t})
        return self.graph.subgraph(relevant).copy(), s, t

    def _path_count_in_dag(self, graph, src_node: str, dst_node: str) -> int:
        if not nx.is_directed_acyclic_graph(graph):
            raise ValueError("combinational graph has a cycle; path count is not well-defined")

        counts = {node: 0 for node in graph.nodes}
        counts[src_node] = 1
        for node in nx.topological_sort(graph):
            for succ in graph.successors(node):
                counts[succ] += counts[node]
        return counts[dst_node]

    def path_count(self, src: str, dst: str) -> int:
        subgraph, s, t = self._relevant_subgraph(src, dst)
        if subgraph is None:
            return 0
        return self._path_count_in_dag(subgraph, s, t)

    def all_paths(self, src: str, dst: str, limit: int = 100) -> Tuple[int, List[List[str]], bool]:
        subgraph, s, t = self._relevant_subgraph(src, dst)
        if subgraph is None:
            return 0, [], True

        count = self._path_count_in_dag(subgraph, s, t)
        if count == 0:
            return 0, [], True
        if count > limit:
            return count, [], False

        # Enumerate only the src/dst-relevant subgraph. On large benchmarks,
        # all_simple_paths() on the full graph can spend minutes exploring
        # branches that can never reach dst.
        paths = [
            [node_to_readable(node) for node in path]
            for path in nx.all_simple_paths(subgraph, s, t)
        ]
        return count, paths, True

    @staticmethod
    def _depth_weight(graph, u: str, v: str) -> int:
        return 1 if graph.nodes[u].get("kind") == "cell" and graph.nodes[v].get("kind") == "net" else 0

    def max_gate_depth(self, src: str, dst: str) -> Optional[int]:
        subgraph, s, t = self._relevant_subgraph(src, dst)
        if subgraph is None:
            return None

        if not nx.is_directed_acyclic_graph(subgraph):
            raise ValueError("combinational graph has a cycle; max depth is not well-defined")

        neg_inf = -10**18
        dist = {node: neg_inf for node in subgraph.nodes}
        dist[s] = 0
        for node in nx.topological_sort(subgraph):
            if dist[node] == neg_inf:
                continue
            for succ in subgraph.successors(node):
                cand = dist[node] + self._depth_weight(subgraph, node, succ)
                if cand > dist[succ]:
                    dist[succ] = cand

        return dist[t]

    def max_fanin_depth(self, dst: str) -> int:
        t = self.normalize_node(dst)
        relevant = nx.ancestors(self.graph, t) | {t}
        subgraph = self.graph.subgraph(relevant).copy()

        if not nx.is_directed_acyclic_graph(subgraph):
            raise ValueError("combinational graph has a cycle; max depth is not well-defined")

        dist = {node: 0 for node in subgraph.nodes}
        for node in nx.topological_sort(subgraph):
            for succ in subgraph.successors(node):
                cand = dist[node] + self._depth_weight(subgraph, node, succ)
                if cand > dist[succ]:
                    dist[succ] = cand
        return dist[t]

    def fanin_cone_cells(self, dst: str) -> List[str]:
        """dst 的扇入锥里的组合门。

        过 _cone_seed：dst 由 DFF.Q 驱动时组合图里没有祖先，旧实现恒返回 []
        （test32#13 "How many gates are in the logic cone of output n12?" -> 0；
        test38#19 "list all gates that contribute to this output" -> 0 gates）。
        本方法只回答"X 的锥里有哪些门"这类【锥成员】问题。

        注意隔壁的 max_fanin_depth 【故意不加】种子：它是 A21.2
        "Combinational depth only; treat DFF.Q outputs as primary inputs"
        明文管辖的深度口径，官方已经给过答案，不能动。
        """
        target = self.normalize_node(_cone_seed(self.ir, dst))
        upstream = nx.ancestors(self.graph, target)
        cells = [
            self.graph.nodes[node]["name"]
            for node in upstream
            if self.graph.nodes[node].get("kind") == "cell"
        ]
        return sort_by_order(cells, self.cell_order)

    def fanout_cone_cells(self, src: str) -> List[str]:
        source = self.normalize_node(src)
        downstream = nx.descendants(self.graph, source)
        cells = [
            self.graph.nodes[node]["name"]
            for node in downstream
            if self.graph.nodes[node].get("kind") == "cell"
        ]
        return sort_by_order(cells, self.cell_order)

    def functional_equivalence(self, left: str, right: str) -> EquivalenceResult:
        checker = YosysEquivalenceChecker(
            self.ir,
            miter_dir=self.miter_dir,
            timeout=self.yosys_timeout,
        )
        return checker.equivalent(left, right)


def verilog_safe_name(raw: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]", "_", raw)
    if not name or name[0].isdigit():
        name = "n_" + name
    return name


class VerilogNameMap:
    def __init__(self):
        self.raw_to_safe: Dict[str, str] = {}
        self.used: Set[str] = set()

    def get(self, raw: str) -> str:
        if raw in ("1'b0", "1'b1"):
            return raw
        if raw in self.raw_to_safe:
            return self.raw_to_safe[raw]

        base = verilog_safe_name(raw)
        name = base
        index = 0
        while name in self.used:
            index += 1
            name = f"{base}_{index}"
        self.used.add(name)
        self.raw_to_safe[raw] = name
        return name


class YosysEquivalenceChecker:
    def __init__(
        self,
        ir: NetlistIR,
        *,
        miter_dir: Optional[Path] = None,
        timeout: int = 240,
    ):
        self.ir = ir
        self.miter_dir = Path(miter_dir) if miter_dir is not None else Path(tempfile.gettempdir()) / "iccad_yosys_miters"
        self.timeout = timeout

    def equivalent(self, left: str, right: str) -> EquivalenceResult:
        yosys_bin = _resolve_yosys_bin()
        if yosys_bin is None:
            return EquivalenceResult(
                status="unknown",
                method="yosys-sat-miter",
                support_size=0,
                support=[],
                message="yosys executable was not found (tried YOSYS_BIN and yosys)",
            )

        try:
            miter_path, support = self._emit_miter(left, right)
            output = self._run_yosys(miter_path, yosys_bin)
        except subprocess.TimeoutExpired:
            return EquivalenceResult(
                status="unknown",
                method="yosys-sat-miter",
                support_size=0,
                support=[],
                message=f"yosys timed out after {self.timeout}s",
            )
        except Exception as exc:
            return EquivalenceResult(
                status="unknown",
                method="yosys-sat-miter",
                support_size=0,
                support=[],
                message=f"yosys miter failed: {exc}",
            )

        if "SAT proof finished - no model found: SUCCESS!" in output:
            return EquivalenceResult(
                status="equivalent",
                method="yosys-sat-miter",
                support_size=len(support),
                support=support,
                message="proved diff is always 0 with Yosys SAT",
            )

        if "SAT proof finished - model found: FAIL!" in output:
            return EquivalenceResult(
                status="different",
                method="yosys-sat-miter",
                support_size=len(support),
                support=support,
                message="Yosys SAT found an assignment with diff=1",
            )

        tail = " | ".join(output.splitlines()[-8:])
        return EquivalenceResult(
            status="unknown",
            method="yosys-sat-miter",
            support_size=len(support),
            support=support,
            message=f"could not parse Yosys SAT result: {tail}",
        )

    def _collect_cone(self, targets: Sequence[str]) -> Tuple[Set[str], Set[str], Set[str]]:
        cells: Set[str] = set()
        source_nets: Set[str] = set()
        all_nets: Set[str] = {net for net in targets if net not in ("1'b0", "1'b1")}
        stack = list(targets)

        while stack:
            net = stack.pop()
            if net in ("1'b0", "1'b1"):
                continue
            if net not in self.ir.nets:
                raise KeyError(f"unknown net for Yosys equivalence: {net}")

            drivers = self.ir.drivers.get(net, [])
            if len(drivers) > 1:
                raise ValueError(f"multi-driver net is not supported by Yosys miter emitter: {net}")

            if not drivers:
                source_nets.add(net)
                all_nets.add(net)
                continue

            cell = self.ir.cells[drivers[0].cell]
            if cell.cell_type not in COMB_GATES:
                source_nets.add(net)
                all_nets.add(net)
                continue

            if cell.name in cells:
                continue
            cells.add(cell.name)

            for input_net in cell.inputs.values():
                if input_net not in ("1'b0", "1'b1"):
                    all_nets.add(input_net)
                stack.append(input_net)
            for output_net in cell.outputs.values():
                if output_net not in ("1'b0", "1'b1"):
                    all_nets.add(output_net)

        return cells, source_nets, all_nets

    def _emit_miter(self, left: str, right: str) -> Tuple[Path, List[str]]:
        cells, source_nets, all_nets = self._collect_cone([left, right])
        names = VerilogNameMap()
        support = sorted(source_nets, key=natural_key)
        wires = sorted(all_nets - source_nets, key=natural_key)

        self.miter_dir.mkdir(parents=True, exist_ok=True)
        miter_path = self.miter_dir / f"{verilog_safe_name(left)}_vs_{verilog_safe_name(right)}.v"

        ports = [names.get(net) for net in support] + ["diff"]
        lines: List[str] = [f"module miter({', '.join(ports)});"]
        if support:
            lines.append("  input " + ", ".join(names.get(net) for net in support) + ";")
        lines.append("  output diff;")
        if wires:
            lines.append("  wire " + ", ".join(names.get(net) for net in wires) + ";")

        for cell_name in self.ir.cell_order:
            if cell_name not in cells:
                continue
            cell = self.ir.cells[cell_name]
            inst = "u_" + names.get(f"cell_{cell.name}")
            output_net = names.get(cell.outputs["Y"])
            if cell.cell_type in {"buf", "not"}:
                lines.append(f"  {cell.cell_type} {inst}({output_net}, {names.get(cell.inputs['A'])});")
            else:
                lines.append(
                    f"  {cell.cell_type} {inst}({output_net}, "
                    f"{names.get(cell.inputs['A'])}, {names.get(cell.inputs['B'])});"
                )

        lines.append(f"  assign diff = {names.get(left)} ^ {names.get(right)};")
        lines.append("endmodule")
        miter_path.write_text("\n".join(lines) + "\n")
        return miter_path, support

    def _run_yosys(self, miter_path: Path, yosys_bin: str) -> str:
        proc = subprocess.run(
            [
                yosys_bin,
                "-p",
                f"read_verilog {miter_path.name}; prep -top miter; sat -prove diff 0 -show diff",
            ],
            cwd=str(miter_path.parent),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=self.timeout,
            check=False,
        )
        return proc.stdout


def load_design(
    netlist_path: str | Path,
    *,
    miter_dir: Optional[Path] = None,
    yosys_timeout: int = 240,
) -> CircuitAnalyzer:
    ir = parse_verilog_to_ir(str(netlist_path))
    return CircuitAnalyzer(
        ir,
        miter_dir=miter_dir,
        yosys_timeout=yosys_timeout,
    )


def _design_metadata(session: DesignSession) -> Dict[str, Any]:
    ir = session.ir
    return {
        "design_id": session.design_id,
        "netlist_path": str(session.netlist_path),
        "module": ir.module_name,
        "signal_count": len(ir.signals),
        "net_count": len(ir.nets),
        "cell_count": len(ir.cells),
        "port_order": list(ir.port_order),
    }


def design_load(
    netlist_path: str | Path,
    *,
    design_id: str = "current",
    miter_dir: Optional[str | Path] = None,
    yosys_timeout: int = 240,
) -> Dict[str, Any]:
    """Load a gate-level Verilog netlist and make it the active design."""
    path = Path(netlist_path)
    analyzer = load_design(
        path,
        miter_dir=Path(miter_dir) if miter_dir is not None else None,
        yosys_timeout=yosys_timeout,
    )
    session = DesignSession(
        design_id=design_id,
        netlist_path=path,
        analyzer=analyzer,
        original_ir=copy.deepcopy(analyzer.ir),
    )
    _DESIGN_SESSIONS[design_id] = session
    return _ok("design_load", **_design_metadata(session))


def design_write(
    output_path: str | Path,
    *,
    design_id: str = "current",
) -> Dict[str, Any]:
    """Write the current design to a Verilog file."""
    session = _get_session(design_id)
    out_path = Path(output_path)
    payload = write_ir(session.ir, out_path)
    return _ok(
        "design_write",
        design_id=session.design_id,
        source_path=str(session.netlist_path),
        **payload,
    )


def analysis_count_gates(*, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    counts = session.analyzer.gate_counts()
    return _ok(
        "analysis_count_gates",
        design_id=session.design_id,
        total=sum(counts.values()),
        by_type={gate.upper(): counts.get(gate, 0) for gate in GATE_COUNT_ORDER},
    )


def analysis_fanin_cone_size(output: str, *, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    cells = session.analyzer.fanin_cone_cells(output)
    return _ok(
        "analysis_fanin_cone_size",
        design_id=session.design_id,
        output=output,
        gate_count=len(cells),
    )


def analysis_transitive_fanin_cone(output: str, *, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    cells = session.analyzer.fanin_cone_cells(output)
    return _ok(
        "analysis_transitive_fanin_cone",
        design_id=session.design_id,
        output=output,
        gate_count=len(cells),
        gates=cells,
    )


def analysis_direct_fanout(signal: str, *, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    gates = session.analyzer.direct_net_fanout_gates(signal)
    return _ok(
        "analysis_direct_fanout",
        design_id=session.design_id,
        signal=signal,
        gate_count=len(gates),
        gates=gates,
    )


def analysis_transitive_fanout_cone(input: str, *, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    cells = session.analyzer.fanout_cone_cells(input)
    return _ok(
        "analysis_transitive_fanout_cone",
        design_id=session.design_id,
        input=input,
        gate_count=len(cells),
        gates=cells,
    )


def analysis_path_exists(source: str, target: str, *, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    exists = session.analyzer.path_exists(source, target)
    return _ok(
        "analysis_path_exists",
        design_id=session.design_id,
        source=source,
        target=target,
        exists=exists,
    )


def analysis_path_exists_avoiding(
    source: str,
    target: str,
    avoid: str,
    *,
    design_id: str = "current",
) -> Dict[str, Any]:
    session = _get_session(design_id)
    exists = session.analyzer.avoid_path_exists(source, target, avoid)
    return _ok(
        "analysis_path_exists_avoiding",
        design_id=session.design_id,
        source=source,
        target=target,
        avoid=avoid,
        exists=exists,
    )


def analysis_enumerate_paths(
    source: str,
    target: str,
    *,
    design_id: str = "current",
    path_limit: int = 1_000_000,
) -> Dict[str, Any]:
    session = _get_session(design_id)
    path_count, paths, complete = session.analyzer.all_paths(source, target, limit=path_limit)
    return _ok(
        "analysis_enumerate_paths",
        design_id=session.design_id,
        source=source,
        target=target,
        path_count=path_count,
        complete=complete,
        path_limit=path_limit,
        paths=paths,
    )


def analysis_max_logic_depth(
    target: Optional[str] = None,
    *,
    source: Optional[str] = None,
    design_id: str = "current",
) -> Dict[str, Any]:
    session = _get_session(design_id)
    # whole-design 模式：不指定 target / source 时，统计整设计的最大组合深度。
    # 按 A21.2 / A30 的组合帧定义，端点集是 {PO, DFF.D}、源集是 {PI, DFF.Q}，共四类路径：
    #   PI->PO / DFF.Q->PO / PI->DFF.D / DFF.Q->DFF.D
    # 组合图（nx_probe）已把 DFF 切成时序边界（DFF.Q 是源、DFF.D 是汇），所以：
    #   * 对每个 PO 位调 max_fanin_depth，已覆盖 PI->PO 与 DFF.Q->PO；
    #   * 对每个 DFF.D 网求深度（复用 pi_to_dff_depth，同一张图、同一边权），覆盖 PI->DFF.D 与 DFF.Q->DFF.D。
    # 早先只数 PO 端点，导致输出全走 DFF 的“纯寄存器型”设计漏掉 reg2reg 深度、报 0 或偏小。
    if target in (None, "") and source in (None, ""):
        depths: List[Tuple[str, int]] = []
        for name in session.ir.signal_order:
            sig = session.ir.signals[name]
            if sig.direction != "output":
                continue
            for bit in session.analyzer.signal_bits(name):
                depths.append((bit, session.analyzer.max_fanin_depth(bit)))
        po_depth = max((value for _, value in depths), default=0)

        # 终点落在 DFF.D 的两类（含 reg2reg），复用已验证的 pi_to_dff_depth（同尺）。
        dff_info = pi_to_dff_depth(session.ir)
        dff_endpoints = [
            {"endpoint": cell_d, "kind": "dff_d", "depth": d}
            for cell_d, d in [
                (item[1], item[2]) for item in dff_info.get("dff_depths", [])
            ]
        ]
        dff_depth = dff_info.get("max_depth", 0)

        depth = max(po_depth, dff_depth)
        endpoints = (
            [{"endpoint": output, "kind": "primary_output", "depth": value} for output, value in depths]
            + dff_endpoints
        )
        return _ok(
            "analysis_max_logic_depth",
            design_id=session.design_id,
            source=None,
            target=None,
            mode="design",
            path_exists=bool(depths) or bool(dff_endpoints),
            depth=depth,
            # 兼容旧字段：outputs 仍只列 PO；新增 endpoints 覆盖 {PO, DFF.D} 全部四类帧的端点。
            outputs=[{"output": output, "depth": value} for output, value in depths],
            endpoints=endpoints,
        )
    if target in (None, ""):
        raise ValueError("analysis_max_logic_depth requires target when source is specified")
    if source is None:
        depth = session.analyzer.max_fanin_depth(target)
        return _ok(
            "analysis_max_logic_depth",
            design_id=session.design_id,
            source=None,
            target=target,
            mode="fanin_cone",
            path_exists=True,
            depth=depth,
        )

    depth = session.analyzer.max_gate_depth(source, target)
    return _ok(
        "analysis_max_logic_depth",
        design_id=session.design_id,
        source=source,
        target=target,
        mode="source_to_target",
        path_exists=depth is not None,
        depth=depth,
    )


def analysis_gate_successors(gate: str, *, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    successors = session.analyzer.gate_successors(gate)
    return _ok(
        "analysis_gate_successors",
        design_id=session.design_id,
        gate=gate,
        successor_count=len(successors),
        successors=successors,
    )


def verification_functional_equivalence(
    left: str,
    right: str,
    *,
    design_id: str = "current",
) -> Dict[str, Any]:
    session = _get_session(design_id)
    result = session.analyzer.functional_equivalence(left, right)
    equivalent: Optional[bool]
    if result.status == "equivalent":
        equivalent = True
    elif result.status == "different":
        equivalent = False
    else:
        equivalent = None

    payload: Dict[str, Any] = {
        "design_id": session.design_id,
        "left": left,
        "right": right,
        "equivalent": equivalent,
        "status": result.status,
        "method": result.method,
        "support_size": result.support_size,
        "support": result.support,
        "message": result.message,
    }
    if result.counterexample is not None:
        payload["counterexample"] = result.counterexample
    return _ok("verification_functional_equivalence", **payload)


# Transformation tools: wrappers around isolated implementations in eda_transform.py.
def transformation_limit_fanout(
    *,
    max_fanout: int = 4,
    signal: Optional[str] = None,
    dedicated: bool = False,
    design_id: str = "current",
) -> Dict[str, Any]:
    session = _get_session(design_id)
    session.pre_transform_ir = copy.deepcopy(session.ir)
    stats = limit_fanout(session.ir, max_fanout=max_fanout, signal=signal, dedicated=dedicated)
    _mark_transform(session, stats)
    stats["final_total_gate_count"] = session.analyzer.total_gate_count()
    return _ok("transformation_limit_fanout", design_id=session.design_id, **stats)


def transformation_insert_dedicated_buffers(signal: str, *, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    session.pre_transform_ir = copy.deepcopy(session.ir)
    stats = limit_fanout(session.ir, max_fanout=1, signal=signal, dedicated=True)
    _mark_transform(session, stats)
    return _ok("transformation_insert_dedicated_buffers", design_id=session.design_id, signal=signal, **stats)


def transformation_remove_dangling_logic(*, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    session.pre_transform_ir = copy.deepcopy(session.ir)
    stats = remove_dangling_logic(session.ir)
    _mark_transform(session, stats)
    return _ok("transformation_remove_dangling_logic", design_id=session.design_id, **stats)


def transformation_rename_identifier(
    old_name: str,
    new_name: str,
    *,
    kind: str = "auto",
    design_id: str = "current",
) -> Dict[str, Any]:
    session = _get_session(design_id)
    session.pre_transform_ir = copy.deepcopy(session.ir)
    stats = rename_identifier(session.ir, old_name, new_name, kind=kind)
    _mark_transform(session, stats)
    return _ok("transformation_rename_identifier", design_id=session.design_id, **stats)


def transformation_reconnect_gate_input(
    gate: str,
    pin: str,
    signal: str,
    *,
    design_id: str = "current",
) -> Dict[str, Any]:
    session = _get_session(design_id)
    session.pre_transform_ir = copy.deepcopy(session.ir)
    stats = reconnect_gate_input(session.ir, gate, pin, signal)
    _mark_transform(session, stats)
    return _ok("transformation_reconnect_gate_input", design_id=session.design_id, **stats)


def transformation_collapse_back_to_back_inverters(*, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    session.pre_transform_ir = copy.deepcopy(session.ir)
    stats = collapse_back_to_back_inverters(session.ir)
    _mark_transform(session, stats)
    return _ok("transformation_collapse_back_to_back_inverters", design_id=session.design_id, **stats)


def transformation_constant_propagation(
    *,
    gate_type: Optional[str] = None,
    constant: Optional[str] = None,
    report_only: bool = False,
    design_id: str = "current",
) -> Dict[str, Any]:
    session = _get_session(design_id)
    if report_only:
        stats = constant_propagation(session.ir, gate_type=gate_type, constant=constant, report_only=True)
        return _ok("transformation_constant_propagation", design_id=session.design_id, **stats)
    session.pre_transform_ir = copy.deepcopy(session.ir)
    stats = constant_propagation(session.ir, gate_type=gate_type, constant=constant, report_only=False)
    _mark_transform(session, stats)
    return _ok("transformation_constant_propagation", design_id=session.design_id, **stats)


def transformation_replace_gate_library(
    *,
    scope: str = "design",
    target: Optional[str] = None,
    from_gate: Optional[str] = None,
    to_library: str = "nand_not",
    design_id: str = "current",
) -> Dict[str, Any]:
    session = _get_session(design_id)
    session.pre_transform_ir = copy.deepcopy(session.ir)
    stats = replace_gate_library(session.ir, scope=scope, target=target, from_gate=from_gate, to_library=to_library)
    _mark_transform(session, stats)
    return _ok("transformation_replace_gate_library", design_id=session.design_id, **stats)


def optimization_reduce_depth(
    *,
    target: Optional[str] = None,
    max_depth: Optional[int] = None,
    scope: str = "design",
    library: Optional[str] = None,
    lib_scope: Optional[str] = None,
    lib_target: Optional[str] = None,
    design_id: str = "current",
) -> Dict[str, Any]:
    session = _get_session(design_id)
    previous_pre_transform_ir = session.pre_transform_ir
    session.pre_transform_ir = copy.deepcopy(session.ir)
    stats = reduce_depth(session.ir, target=target, max_depth=max_depth, scope=scope,
                         library=library, lib_scope=lib_scope, lib_target=lib_target)
    if stats.get("status") == "kept_original":
        session.pre_transform_ir = previous_pre_transform_ir
        session.last_stats = stats
    else:
        _mark_transform(session, stats)
    return _ok("optimization_reduce_depth", design_id=session.design_id, **stats)


def optimization_merge_equivalent_or_duplicate_gates(
    *,
    mode: str = "structural",
    design_id: str = "current",
) -> Dict[str, Any]:
    session = _get_session(design_id)
    session.pre_transform_ir = copy.deepcopy(session.ir)
    stats = merge_functionally_equivalent_gates(session.ir) if mode == "functional" else merge_structural_duplicates(session.ir)
    stats["mode"] = mode
    _mark_transform(session, stats)
    return _ok("optimization_merge_equivalent_or_duplicate_gates", design_id=session.design_id, **stats)


# Extended analysis tools.
def analysis_max_fanout(
    signal: Optional[str] = None,
    *,
    scope: Optional[str] = None,
    design_id: str = "current",
) -> Dict[str, Any]:
    session = _get_session(design_id)
    return _ok("analysis_max_fanout", design_id=session.design_id, **max_fanout(session.ir, signal, scope=scope))


def analysis_primary_io_summary(*, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    return _ok("analysis_primary_io_summary", design_id=session.design_id, **primary_io_summary(session.ir))


def analysis_gate_info(gate: str, *, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    info = gate_info(session.ir, gate)
    # 若当前网表里找不到（可能已被前序变换删除/改名），回退到原始网表回答其类型/连接。
    if info["type"] == "NOT_FOUND" and session.original_ir is not None:
        info = gate_info(session.original_ir, gate)
    return _ok("analysis_gate_info", design_id=session.design_id, **info)


def analysis_list_gates_by_type(gate_type: str, *, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    return _ok("analysis_list_gates_by_type", design_id=session.design_id, **list_gates_by_type(session.ir, gate_type))


def analysis_cone_gate_type_count(target: str, *, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    return _ok("analysis_cone_gate_type_count", design_id=session.design_id, **cone_gate_type_count(session.ir, target))


def analysis_shared_fanin_cone(left: str, right: str, *, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    return _ok("analysis_shared_fanin_cone", design_id=session.design_id, **shared_fanin_cone(session.ir, left, right))


def analysis_zero_length_paths(*, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    return _ok("analysis_zero_length_paths", design_id=session.design_id, **zero_length_paths(session.ir))


def analysis_register_paths(*, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    return _ok("analysis_register_paths", design_id=session.design_id, **register_paths(session.ir))


def analysis_register_path_depth(*, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    return _ok("analysis_register_path_depth", design_id=session.design_id, **register_path_depth(session.ir))


def analysis_pi_to_dff_depth(*, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    return _ok("analysis_pi_to_dff_depth", design_id=session.design_id, **pi_to_dff_depth(session.ir))


def analysis_outputs_depth_over(threshold: int, *, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    return _ok("analysis_outputs_depth_over", design_id=session.design_id, **outputs_depth_over(session.ir, threshold))


def analysis_gate_on_max_depth_path(gate: str, *, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    return _ok("analysis_gate_on_max_depth_path", design_id=session.design_id, **gate_on_max_depth_path(session.ir, gate))


def analysis_deepest_output(*, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    return _ok("analysis_deepest_output", design_id=session.design_id, **deepest_output(session.ir))


def analysis_largest_fanin_cone_output(*, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    return _ok("analysis_largest_fanin_cone_output", design_id=session.design_id, **largest_fanin_cone_output(session.ir))


def analysis_dffs_by_clock(clock: str, *, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    return _ok("analysis_dffs_by_clock", design_id=session.design_id, **dffs_by_clock(session.ir, clock))


def analysis_floating_or_unconnected(*, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    return _ok("analysis_floating_or_unconnected", design_id=session.design_id, **floating_or_unconnected(session.ir))


def analysis_cut_or_articulation(
    signal: str,
    *,
    source: Optional[str] = None,
    target: Optional[str] = None,
    scope: Optional[str] = None,
    design_id: str = "current",
) -> Dict[str, Any]:
    session = _get_session(design_id)
    return _ok(
        "analysis_cut_or_articulation",
        design_id=session.design_id,
        **cut_or_articulation(session.ir, signal, source=source, target=target, scope=scope),
    )


def analysis_articulation_points_between(source: str, target: str, *, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    return _ok(
        "analysis_articulation_points_between",
        design_id=session.design_id,
        **articulation_points_between(session.ir, source, target),
    )


def analysis_boolean_expression(target: str, *, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    return _ok("analysis_boolean_expression", design_id=session.design_id, **boolean_expression(session.ir, target))


def analysis_signal_dependency(output: str, input: str, *, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    return _ok("analysis_signal_dependency", design_id=session.design_id, **signal_dependency(session.ir, output, input))


def analysis_signal_symmetry(target: str, input_a: str, input_b: str, *, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    return _ok("analysis_signal_symmetry", design_id=session.design_id, **signal_symmetry(session.ir, target, input_a, input_b))


def analysis_signal_constant(signal: str, value: Optional[str] = None, *, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    payload = _ok("analysis_signal_constant", design_id=session.design_id, **signal_constant(session.ir, signal))
    if value is not None:
        payload["requested_value"] = value
    return payload


def analysis_find_nand_equivalent(target: str, *, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    return _ok("analysis_find_nand_equivalent", design_id=session.design_id, **find_nand_equivalent(session.ir, target))


def analysis_dff_enable_hold_structures(*, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    return _ok("analysis_dff_enable_hold_structures", design_id=session.design_id, **dff_enable_hold_structures(session.ir))


def verification_design_equivalence(reference: str = "original", *, design_id: str = "current") -> Dict[str, Any]:
    session = _get_session(design_id)
    ref_ir = session.pre_transform_ir if reference == "pre_transform" else session.original_ir
    return _ok("verification_design_equivalence", design_id=session.design_id, reference=reference, **design_equivalence(session.ir, ref_ir))
