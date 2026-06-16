from __future__ import annotations

import copy
import itertools
import shutil
import subprocess
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

import networkx as nx

from ir import Cell, DeclRecord, Net, NetlistIR, PinRef, SignalDecl
from nx_probe import cell_node, net_node, node_to_readable


COMB_GATES = {"and", "or", "nand", "nor", "not", "buf", "xor", "xnor"}
BINARY_GATES = {"and", "or", "nand", "nor", "xor", "xnor"}
CONSTANTS = {"1'b0", "1'b1"}


# Transformation: IR serialization and safe editing primitives.
class IRWriter:
    def __init__(self, ir: NetlistIR):
        self.ir = ir

    def write(self, path: str | Path) -> Dict[str, Any]:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        text = self.to_verilog()
        out.write_text(text)
        return {"output_path": str(out), "bytes": out.stat().st_size}

    def to_verilog(self) -> str:
        rn = self._safe_names()
        em = lambda x: rn.get(x, x)
        lines: List[str] = []
        lines.extend(self._dff_model())
        ports = ", ".join(em(pp) for pp in self.ir.port_order)
        lines.append(f"module {self.ir.module_name}({ports});")

        for direction in ("input", "output"):
            for sig in self._signals_by_direction(direction):
                lines.append(f"  {direction} {self._width(sig)}{em(sig.name)};")

        wire_names = [
            name
            for name in self.ir.signal_order
            if self.ir.signals[name].direction == "internal"
        ]
        for chunk in self._chunks(wire_names, 8):
            scalar = [name for name in chunk if self.ir.signals[name].width == 1]
            if scalar:
                lines.append("  wire " + ", ".join(em(n) for n in scalar) + ";")
            for name in chunk:
                sig = self.ir.signals[name]
                if sig.width != 1:
                    lines.append(f"  wire {self._width(sig)}{em(name)};")

        for cell_name in self.ir.cell_order:
            if cell_name not in self.ir.cells:
                continue
            cell = self.ir.cells[cell_name]
            if cell.cell_type == "dff":
                ports = []
                for pin in cell.port_order:
                    if pin in cell.outputs:
                        ports.append(f".{pin}({em(cell.outputs[pin])})")
                    elif pin in cell.inputs:
                        ports.append(f".{pin}({em(cell.inputs[pin])})")
                lines.append(f"  dff {em(cell.name)}(" + ", ".join(ports) + ");")
            elif cell.cell_type in {"buf", "not"}:
                lines.append(
                    f"  {cell.cell_type} {em(cell.name)}({em(cell.outputs['Y'])}, {em(cell.inputs['A'])});"
                )
            else:
                lines.append(
                    f"  {cell.cell_type} {em(cell.name)}({em(cell.outputs['Y'])}, {em(cell.inputs['A'])}, {em(cell.inputs['B'])});"
                )

        lines.append("endmodule")
        return "\n".join(lines) + "\n"

    def _safe_names(self) -> Dict[str, str]:
        """把'非法的标量内部线名'和'实例名'映射成合法 Verilog 标识符。
        合法名、总线位选(如 n4[0])、常量、端口一律不动；映射一致且去重。"""
        import re as _re
        legal = _re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
        names: List[str] = [
            n for n in self.ir.signal_order
            if self.ir.signals[n].direction == "internal" and self.ir.signals[n].width == 1
        ]
        names += list(self.ir.cells.keys())
        used: Set[str] = {n for n in names if legal.match(n)}
        rename: Dict[str, str] = {}
        for n in names:
            if legal.match(n):
                continue
            base = _re.sub(r"[^A-Za-z0-9_$]", "_", n)
            if not _re.match(r"[A-Za-z_]", base):
                base = "n_" + base
            cand, k = base, 0
            while cand in used:
                k += 1
                cand = f"{base}_{k}"
            used.add(cand)
            rename[n] = cand
        return rename

    def _signals_by_direction(self, direction: str) -> List[SignalDecl]:
        return [
            self.ir.signals[name]
            for name in self.ir.signal_order
            if self.ir.signals[name].direction == direction
        ]

    @staticmethod
    def _width(sig: SignalDecl) -> str:
        if sig.width == 1:
            return ""
        return f"[{sig.msb}:{sig.lsb}] "

    @staticmethod
    def _dff_model() -> List[str]:
        return [
            "module dff(input RN, input SN, input CK, input D, output reg Q);",
            "  always @(posedge CK or negedge RN) begin",
            "    if (!RN) Q <= 1'b0;",
            "    else if (!SN) Q <= 1'b1;",
            "    else Q <= D;",
            "  end",
            "endmodule",
            "",
        ]

    @staticmethod
    def _chunks(items: List[str], size: int) -> Iterable[List[str]]:
        for i in range(0, len(items), size):
            yield items[i : i + size]


class TransformContext:
    def __init__(self, ir: NetlistIR):
        self.ir = ir

    def clone_ir(self) -> NetlistIR:
        return copy.deepcopy(self.ir)

    def rebuild(self) -> None:
        self.ir.rebuild_indices()

    def ensure_wire(self, name: str) -> str:
        if name in CONSTANTS:
            return name
        if name in self.ir.nets:
            return name
        self.ir.add_signal(name, "wire")
        return name

    def unique_name(self, prefix: str, existing: Optional[Set[str]] = None) -> str:
        cells = self.ir.cells
        sigs = self.ir.signals
        nets = self.ir.nets
        ex = existing if existing else ()
        # Direct dict membership = O(1); avoid building a fresh set(N) every call.
        if prefix not in cells and prefix not in sigs and prefix not in nets and prefix not in ex:
            return prefix
        for idx in itertools.count(0):
            candidate = f"{prefix}_{idx}"
            if candidate not in cells and candidate not in sigs and candidate not in nets and candidate not in ex:
                return candidate
        raise RuntimeError("unreachable")

    def new_wire(self, prefix: str = "tr_wire") -> str:
        name = self.unique_name(prefix)
        self.ir.add_signal(name, "wire")
        return name

    def add_cell(self, cell_type: str, inputs: Dict[str, str], output: str, prefix: str) -> str:
        name = self.unique_name(prefix)
        self.ensure_wire(output)
        for net in inputs.values():
            self.ensure_wire(net)
        port_order = ["Y"] + list(inputs) if cell_type != "dff" else list(inputs) + ["Q"]
        self.ir.add_cell(
            Cell(
                name=name,
                cell_type=cell_type,  # type: ignore[arg-type]
                inputs=dict(inputs),
                outputs={"Y": output},
                port_order=port_order,
            )
        )
        return name

    def delete_cell(self, name: str) -> bool:
        if name not in self.ir.cells:
            return False
        del self.ir.cells[name]
        self.ir.cell_order = [cell for cell in self.ir.cell_order if cell != name]
        return True

    def reconnect_load(self, cell_name: str, pin: str, new_net: str) -> None:
        self.ensure_wire(new_net)
        self.ir.cells[cell_name].inputs[pin] = new_net

    def rename_cell(self, old: str, new: str) -> bool:
        if old not in self.ir.cells or new in self.ir.cells:
            return False
        cell = self.ir.cells.pop(old)
        cell.name = new
        self.ir.cells[new] = cell
        self.ir.cell_order = [new if name == old else name for name in self.ir.cell_order]
        return True

    def rename_signal_or_net(self, old: str, new: str) -> bool:
        if old in CONSTANTS or new in self.ir.signals or new in self.ir.nets:
            return False
        changed = False
        if old in self.ir.signals:
            sig = self.ir.signals.pop(old)
            sig.name = new
            self.ir.signals[new] = sig
            self.ir.signal_order = [new if name == old else name for name in self.ir.signal_order]
            changed = True
        nets = [old]
        if old in self.ir.signals and self.ir.signals[old].width != 1:
            nets = [f"{old}[{i}]" for i in range(min(self.ir.signals[old].msb, self.ir.signals[old].lsb), max(self.ir.signals[old].msb, self.ir.signals[old].lsb) + 1)]
        for net in list(self.ir.nets):
            if net == old or net.startswith(old + "["):
                suffix = net[len(old) :]
                renamed = new + suffix
                record = self.ir.nets.pop(net)
                record.name = renamed
                record.base = new if record.base == old else record.base
                self.ir.nets[renamed] = record
                for cell in self.ir.cells.values():
                    for pin, value in list(cell.inputs.items()):
                        if value == net:
                            cell.inputs[pin] = renamed
                    for pin, value in list(cell.outputs.items()):
                        if value == net:
                            cell.outputs[pin] = renamed
                changed = True
        if not changed and old in self.ir.nets:
            record = self.ir.nets.pop(old)
            record.name = new
            record.base = new
            self.ir.nets[new] = record
            changed = True
        return changed

    def replace_loads(self, old_net: str, new_net: str, skip: Optional[Set[str]] = None) -> int:
        skip = skip or set()
        count = 0
        self.ensure_wire(new_net)
        for cell in self.ir.cells.values():
            if cell.name in skip:
                continue
            for pin, net in list(cell.inputs.items()):
                if net == old_net:
                    cell.inputs[pin] = new_net
                    count += 1
        return count

    def output_net(self, cell: Cell) -> str:
        return next(iter(cell.outputs.values()))

    def input_nets(self, cell: Cell) -> List[str]:
        return [cell.inputs[pin] for pin in cell.inputs]


# Transformation: netlist editing operations.
def write_ir(ir: NetlistIR, path: str | Path) -> Dict[str, Any]:
    return IRWriter(ir).write(path)


def _primary_output_nets(ir: NetlistIR) -> Set[str]:
    """所有主输出的 bit-net 名集合。A29: PO 连接也算一个 fanout load。"""
    nets: Set[str] = set()
    for sig in _signals(ir, "output"):
        for bit in _signal_bits(ir, sig.name):
            nets.add(bit)
    return nets


def _net_load_count(ir: NetlistIR, net: str, po_nets: Set[str]) -> int:
    """net 的真实扇出 load 数 = cell 输入引脚 sink 数 + (该 net 是主输出则 +1)。"""
    return len(ir.loads.get(net, [])) + (1 if net in po_nets else 0)


def limit_fanout(ir: NetlistIR, max_fanout: int = 4, signal: Optional[str] = None, dedicated: bool = False) -> Dict[str, Any]:
    ctx = TransformContext(ir)
    ctx.rebuild()
    po_nets = _primary_output_nets(ir)  # A29: 主输出抽头也算 load
    added = 0
    processed = 0
    targets = [signal] if signal else [net for net in list(ir.nets) if net not in CONSTANTS]

    for net in targets:
        if net not in ir.nets:
            continue
        loads = list(ir.loads.get(net, []))
        reserved = 1 if net in po_nets else 0
        effective = len(loads) + reserved  # A29: 把 PO 抽头计入扇出判定
        if effective <= max_fanout and not dedicated:
            continue
        if not loads:
            # 只有 PO 抽头、没有 cell 引脚 sink：无可重连的负载，跳过（也不会超阈值）。
            continue
        processed += 1
        added += _buffer_loads(ctx, net, loads, max_fanout, dedicated, reserved_loads=reserved)
        # 原来每个 net 都 ctx.rebuild()（O(cells)）→ 大网表 O(nets×cells) 爆炸（test34 310s）。
        # buffer 只改“被处理 net 自己”的 loads，不影响其它 net，故循环内无需重建索引。

    ctx.rebuild()  # 全部处理完统一刷新一次索引
    return {"processed_nets": processed, "added_buffers": added, "max_fanout": max_fanout}


def _buffer_loads(ctx: TransformContext, source_net: str, loads, max_fanout: int, dedicated: bool, reserved_loads: int = 0) -> int:
    if not loads:
        return 0
    if len(loads) + reserved_loads <= max_fanout and not dedicated:
        return 0

    added = 0
    if dedicated:
        groups = [[load] for load in loads]
        for group in groups:
            buf_net = ctx.new_wire(f"{source_net}_buf")
            ctx.add_cell("buf", {"A": source_net}, buf_net, f"buf_{source_net}")
            added += 1
            for load in group:
                ctx.reconnect_load(load.cell, load.pin, buf_net)
        return added

    if max_fanout <= 1:
        return added

    # Cost-aware fanout repair: each BUF consumes one load slot on source_net but can
    # absorb up to max_fanout existing loads, so it reduces source fanout by at most
    # max_fanout - 1.  Group only as many loads as needed and keep the rest directly
    # driven by source_net.  This minimizes the number of added BUFs for the final
    # total-gate-count cost, unlike chunking every load into BUF groups.
    work: List[PinRef] = list(loads)
    while len(work) + reserved_loads > max_fanout:
        need_reduction = len(work) + reserved_loads - max_fanout
        group_size = min(max_fanout, need_reduction + 1)
        group = work[:group_size]
        work = work[group_size:]

        buf_net = ctx.new_wire(f"{source_net}_buf")
        buf_name = ctx.add_cell("buf", {"A": source_net}, buf_net, f"buf_{source_net}")
        added += 1
        for load in group:
            ctx.reconnect_load(load.cell, load.pin, buf_net)
        work.append(PinRef(buf_name, "A"))
    return added


def remove_dangling_logic(ir: NetlistIR) -> Dict[str, Any]:
    ctx = TransformContext(ir)
    ctx.rebuild()
    roots: Set[str] = set()
    for sig in ir.signals.values():
        if sig.direction == "output":
            roots.update(_signal_bits(ir, sig.name))
    for cell in ir.cells.values():
        if cell.cell_type == "dff":
            roots.update(cell.inputs.values())
            roots.update(cell.outputs.values())

    live: Set[str] = set()
    stack = list(roots)
    while stack:
        net = stack.pop()
        for driver in ir.drivers.get(net, []):
            if driver.cell in live:
                continue
            live.add(driver.cell)
            stack.extend(ir.cells[driver.cell].inputs.values())

    before = len(ir.cells)
    for cell_name in list(ir.cell_order):
        if cell_name not in live:
            ctx.delete_cell(cell_name)
    ctx.rebuild()
    used_nets = set(CONSTANTS)
    for cell in ir.cells.values():
        used_nets.update(cell.inputs.values())
        used_nets.update(cell.outputs.values())
    for sig in ir.signals.values():
        if sig.direction in {"input", "output"}:
            used_nets.update(_signal_bits(ir, sig.name))
    removed_wires = 0
    for name in list(ir.signal_order):
        sig = ir.signals[name]
        if sig.direction != "internal":
            continue
        bits = set(_signal_bits(ir, name))
        if not bits & used_nets:
            ir.signal_order.remove(name)
            del ir.signals[name]
            for bit in bits:
                ir.nets.pop(bit, None)
            removed_wires += 1
    ctx.rebuild()
    return {"removed_gates": before - len(ir.cells), "removed_wires": removed_wires}


def rename_identifier(ir: NetlistIR, old_name: str, new_name: str, kind: str = "auto") -> Dict[str, Any]:
    ctx = TransformContext(ir)
    changed = False
    actual_kind = kind
    if kind in {"auto", "gate"} and old_name in ir.cells:
        changed = ctx.rename_cell(old_name, new_name)
        actual_kind = "gate"
    elif kind in {"auto", "wire", "signal", "net"}:
        changed = ctx.rename_signal_or_net(old_name, new_name)
        actual_kind = "signal"
    ctx.rebuild()
    return {"renamed": changed, "old_name": old_name, "new_name": new_name, "kind": actual_kind}


def reconnect_gate_input(ir: NetlistIR, gate: str, pin: str, signal: str) -> Dict[str, Any]:
    ctx = TransformContext(ir)
    if gate not in ir.cells:
        return {"changed": False, "message": f"unknown gate: {gate}"}
    cell = ir.cells[gate]
    if pin not in cell.inputs:
        return {"changed": False, "message": f"gate {gate} has no input pin {pin}"}
    old = cell.inputs[pin]
    ctx.reconnect_load(gate, pin, signal)
    ctx.rebuild()
    return {"changed": True, "gate": gate, "pin": pin, "old_signal": old, "new_signal": signal}


def collapse_back_to_back_inverters(ir: NetlistIR) -> Dict[str, Any]:
    ctx = TransformContext(ir)
    ctx.rebuild()
    pairs: List[Tuple[str, str]] = []
    to_delete: Set[str] = set()

    def mark_delete(name: str) -> None:
        if name in ir.cells:
            ir.cells.pop(name, None)
            to_delete.add(name)

    def remove_pinref(items, cell: str, pin: Optional[str] = None):
        return [pr for pr in items if not (pr.cell == cell and (pin is None or pr.pin == pin))]

    def rewire_known_loads(old_net: str, new_net: str, skip: Set[str]) -> List[Any]:
        rewired = []
        for pr in list(ir.loads.get(old_net, [])):
            if pr.cell in skip or pr.cell in to_delete or pr.cell not in ir.cells:
                continue
            ir.cells[pr.cell].inputs[pr.pin] = new_net
            rewired.append(pr)
        if rewired:
            rewired_set = set(rewired)
            ir.loads[old_net] = [pr for pr in ir.loads.get(old_net, []) if pr not in rewired_set]
            ir.loads[new_net] = list(ir.loads.get(new_net, [])) + rewired
        return rewired

    for first_name in list(ir.cell_order):
        if first_name in to_delete or first_name not in ir.cells:
            continue
        first = ir.cells[first_name]
        if first.cell_type != "not":
            continue
        mid = first.outputs.get("Y")
        loads = [pr for pr in ir.loads.get(mid, []) if pr.cell not in to_delete and pr.cell in ir.cells]
        if len(loads) != 1:
            continue
        second_name = loads[0].cell
        if second_name in to_delete or second_name not in ir.cells:
            continue
        second = ir.cells[second_name]
        if second.cell_type != "not" or loads[0].pin != "A":
            continue
        original = first.inputs["A"]
        out = second.outputs["Y"]
        if original == out:
            continue   # 退化自环，跳过
        # 语义：mid = ¬original，out = ¬mid = original。mid、out 任一为主输出端口时，
        # 不能直接删除其驱动门，否则该 PO 会失驱；只对相关 load/driver 做局部维护，最后统一 rebuild。
        mid_is_po = _drives_primary_output(ir, mid)
        out_is_po = _drives_primary_output(ir, out)
        pair_cells = {first_name, second_name}

        if out_is_po:
            # out 是 PO：保留第二个实例作为 BUF(original)->out，保证 PO 仍有驱动。
            second.cell_type = "buf"
            second.inputs = {"A": original}
            second.port_order = ["Y", "A"]
            ir.loads[mid] = remove_pinref(ir.loads.get(mid, []), second_name, "A")
            if all(pr.cell != second_name or pr.pin != "A" for pr in ir.loads.get(original, [])):
                ir.loads[original] = list(ir.loads.get(original, [])) + [loads[0]]
        else:
            # out 是内部网线：只重接实际读取 out 的负载，不再全表扫描。
            rewire_known_loads(out, original, pair_cells)
            ir.drivers[out] = [pr for pr in ir.drivers.get(out, []) if pr.cell != second_name]
            mark_delete(second_name)
            ir.loads[mid] = remove_pinref(ir.loads.get(mid, []), second_name, "A")

        if not mid_is_po:
            # mid 非 PO 且第二级已不再读取 mid，可删除第一级；从 original 的 loads 中移除它。
            mark_delete(first_name)
            ir.loads[original] = remove_pinref(ir.loads.get(original, []), first_name, "A")
            ir.drivers[mid] = [pr for pr in ir.drivers.get(mid, []) if pr.cell != first_name]

        pairs.append((first_name, second_name))

    if to_delete:
        ir.cell_order = [name for name in ir.cell_order if name not in to_delete]
    ctx.rebuild()  # 循环结束统一刷新一次索引
    return {"collapsed_pairs": len(pairs), "pairs": pairs}


def constant_propagation(
    ir: NetlistIR,
    gate_type: Optional[str] = None,
    constant: Optional[str] = None,
    report_only: bool = False,
) -> Dict[str, Any]:
    ctx = TransformContext(ir)
    ctx.rebuild()
    reported = []
    eliminated = 0
    for name in list(ir.cell_order):
        if name not in ir.cells:
            continue
        cell = ir.cells[name]
        if cell.cell_type not in BINARY_GATES:
            continue
        if gate_type and cell.cell_type != gate_type.lower():
            continue
        inputs = cell.inputs
        const_pins = {pin: net for pin, net in inputs.items() if net in CONSTANTS}
        if constant and constant not in const_pins.values():
            continue
        if not const_pins:
            continue
        reported.append({"gate": name, "type": cell.cell_type, "inputs": dict(inputs), "output": cell.outputs["Y"]})
        if report_only:
            continue
        replacement = _constant_replacement(cell)
        if replacement is None:
            continue
        out = cell.outputs["Y"]
        if replacement in CONSTANTS:
            driver_name = ctx.add_cell("buf", {"A": replacement}, out, f"const_{name}")
            ctx.delete_cell(name)
            eliminated += 1
        elif replacement[0] == "wire":
            ctx.replace_loads(out, replacement[1], skip={name})
            ctx.delete_cell(name)
            eliminated += 1
        elif replacement[0] == "not":
            cell.cell_type = "not"
            cell.inputs = {"A": replacement[1]}
            cell.port_order = ["Y", "A"]
            eliminated += 1
        ctx.rebuild()
    return {"reported_gates": reported, "eliminated_gates": eliminated, "report_only": report_only}


def _constant_replacement(cell: Cell):
    vals = set(cell.inputs.values())
    non_const = [net for net in cell.inputs.values() if net not in CONSTANTS]
    x = non_const[0] if non_const else "1'b0"
    if cell.cell_type == "nand":
        if "1'b0" in vals:
            return "1'b1"
        if "1'b1" in vals and len(non_const) == 1:
            return ("not", x)
    if cell.cell_type == "and":
        if "1'b0" in vals:
            return "1'b0"
        if "1'b1" in vals and len(non_const) == 1:
            return ("wire", x)
    if cell.cell_type == "or":
        if "1'b1" in vals:
            return "1'b1"
        if "1'b0" in vals and len(non_const) == 1:
            return ("wire", x)
    if cell.cell_type == "nor":
        if "1'b1" in vals:
            return "1'b0"
        if "1'b0" in vals and len(non_const) == 1:
            return ("not", x)
    # gap④: XOR/XNOR 带常量输入化简
    if cell.cell_type == "xor":
        if "1'b0" in vals and len(non_const) == 1:
            return ("wire", x)   # XOR(a,0)=a
        if "1'b1" in vals and len(non_const) == 1:
            return ("not", x)    # XOR(a,1)=NOT a
    if cell.cell_type == "xnor":
        if "1'b0" in vals and len(non_const) == 1:
            return ("not", x)    # XNOR(a,0)=NOT a
        if "1'b1" in vals and len(non_const) == 1:
            return ("wire", x)   # XNOR(a,1)=a
    return None


def replace_gate_library(
    ir: NetlistIR,
    scope: str = "design",
    target: Optional[str] = None,
    from_gate: Optional[str] = None,
    to_library: str = "nand_not",
) -> Dict[str, Any]:
    # gap②: 未知目标库不再静默 no-op，显式报告 unsupported
    _SUPPORTED_LIBS = {"nand_not", "nor_not", "and_not", "and_or_not", "or_not"}
    if to_library not in _SUPPORTED_LIBS:
        return {
            "scope": scope, "target": target, "to_library": to_library,
            "replaced_gates": 0, "added_by_type": {},
            "unsupported_library": to_library,
            "error": f"unsupported target library '{to_library}'; supported: {sorted(_SUPPORTED_LIBS)}",
        }
    ctx = TransformContext(ir)
    ctx.rebuild()
    selected = _selected_cells(ir, scope, target)
    replaced = 0
    added = Counter()
    to_delete: Set[str] = set()
    for name in list(ir.cell_order):
        if name not in selected or name not in ir.cells:
            continue
        cell = ir.cells[name]
        if cell.cell_type == "dff":
            continue
        if from_gate and cell.cell_type != from_gate.lower():
            continue
        if cell.cell_type == "buf" and to_library in {"nand_not", "nor_not", "and_not", "and_or_not", "or_not"}:
            out = cell.outputs.get("Y")
            a = cell.inputs.get("A")
            if not out or not a:
                continue
            if _drives_primary_output(ir, out):
                # 严格门库约束里 BUF 不算合法门；若 BUF 直接驱动 PO，不能简单旁路，
                # 否则 writer 无 assign 可写，会让 PO 失去门级驱动。用两级 NOT 等价替换。
                mid = ctx.new_wire(f"{cell.name}_buf_inv")
                ctx.add_cell("not", {"A": a}, mid, f"{cell.name}_buf_inv")
                ctx.add_cell("not", {"A": mid}, out, f"{cell.name}_buf_out")
                added["NOT"] += 2
            else:
                # 非 PO BUF 可直接旁路：把所有负载从 BUF 输出改接到输入，再删除 BUF。
                ctx.replace_loads(out, a, skip={cell.name})
            replaced += 1
            to_delete.add(name)
            continue
        if _gate_allowed(cell.cell_type, to_library):
            continue
        stats = _replace_one_gate(ctx, cell, to_library)
        if stats:
            replaced += 1
            added.update(stats)
            to_delete.add(name)
    # Batch delete: avoids O(N) cell_order rebuild per call
    for n in to_delete:
        ir.cells.pop(n, None)
    ir.cell_order = [c for c in ir.cell_order if c not in to_delete]
    ctx.rebuild()
    return {"scope": scope, "target": target, "to_library": to_library, "replaced_gates": replaced, "added_by_type": dict(added)}


def _selected_cells(ir: NetlistIR, scope: str, target: Optional[str]) -> Set[str]:
    if scope == "cone" and target:
        graph = _build_graph(ir)
        t = net_node(target) if target in ir.nets else cell_node(target)
        if t not in graph:
            return set()
        return {node_to_readable(n) for n in nx.ancestors(graph, t) if n.startswith("cell:")}
    return set(ir.cells)


def _gate_allowed(gate: str, library: str) -> bool:
    allowed = {
        "nand_not": {"nand", "not"},
        "nor_not": {"nor", "not"},
        "and_not": {"and", "not"},
        "and_or_not": {"and", "or", "not"},
        "or_not": {"or", "not"},
    }.get(library, set())
    return gate in allowed


def _replace_one_gate(ctx: TransformContext, cell: Cell, library: str) -> Counter:
    stats = Counter()
    out = cell.outputs["Y"]
    a = cell.inputs.get("A")
    b = cell.inputs.get("B")
    if cell.cell_type in {"buf", "not"}:
        if cell.cell_type == "buf" and library in {"nand_not", "nor_not", "and_not", "and_or_not", "or_not"}:
            ctx.replace_loads(out, a, skip={cell.name})
            return stats
        if cell.cell_type == "not" and _gate_allowed("not", library):
            return stats
    if not a or (cell.cell_type in BINARY_GATES and not b):
        return stats

    def add(gate: str, ins: Dict[str, str], output: Optional[str] = None) -> str:
        nonlocal stats
        net = output or ctx.new_wire(f"{cell.name}_{gate}")
        ctx.add_cell(gate, ins, net, f"{cell.name}_{gate}")
        stats[gate.upper()] += 1
        return net

    if library == "nand_not":
        if cell.cell_type == "and":
            n = add("nand", {"A": a, "B": b})
            add("not", {"A": n}, out)
        elif cell.cell_type == "or":
            na = add("not", {"A": a})
            nb = add("not", {"A": b})
            add("nand", {"A": na, "B": nb}, out)
        elif cell.cell_type == "nor":
            o = add("nand", {"A": add("not", {"A": a}), "B": add("not", {"A": b})})
            add("not", {"A": o}, out)
        elif cell.cell_type == "xor":
            n1 = add("nand", {"A": a, "B": b})
            n2 = add("nand", {"A": a, "B": n1})
            n3 = add("nand", {"A": b, "B": n1})
            add("nand", {"A": n2, "B": n3}, out)
        elif cell.cell_type == "xnor":
            x = add("nand", {"A": add("nand", {"A": a, "B": add("nand", {"A": a, "B": b})}), "B": add("nand", {"A": b, "B": add("nand", {"A": a, "B": b})})})
            add("not", {"A": x}, out)
        else:
            return stats
        return stats

    if library == "and_not":
        if cell.cell_type == "nand":
            add("not", {"A": add("and", {"A": a, "B": b})}, out)
        elif cell.cell_type == "or":
            add("not", {"A": add("and", {"A": add("not", {"A": a}), "B": add("not", {"A": b})})}, out)
        elif cell.cell_type == "nor":
            add("and", {"A": add("not", {"A": a}), "B": add("not", {"A": b})}, out)
        elif cell.cell_type == "xor":
            t1 = add("and", {"A": a, "B": add("not", {"A": b})})
            t2 = add("and", {"A": add("not", {"A": a}), "B": b})
            add("not", {"A": add("and", {"A": add("not", {"A": t1}), "B": add("not", {"A": t2})})}, out)
        elif cell.cell_type == "xnor":
            t1 = add("and", {"A": a, "B": b})
            t2 = add("and", {"A": add("not", {"A": a}), "B": add("not", {"A": b})})
            add("not", {"A": add("and", {"A": add("not", {"A": t1}), "B": add("not", {"A": t2})})}, out)
        else:
            return stats
        return stats

    if library == "nor_not":
        if cell.cell_type == "or":
            n = add("nor", {"A": a, "B": b})
            add("not", {"A": n}, out)
        elif cell.cell_type == "and":
            na = add("not", {"A": a})
            nb = add("not", {"A": b})
            add("nor", {"A": na, "B": nb}, out)
        elif cell.cell_type == "nand":
            and_net = add("nor", {"A": add("not", {"A": a}), "B": add("not", {"A": b})})
            add("not", {"A": and_net}, out)
        elif cell.cell_type == "xor":
            na = add("nor", {"A": a, "B": a})
            nb = add("nor", {"A": b, "B": b})
            a_and_not_b = add("nor", {"A": na, "B": b})
            not_a_and_b = add("nor", {"A": a, "B": nb})
            add("nor", {"A": add("nor", {"A": a_and_not_b, "B": not_a_and_b}), "B": add("nor", {"A": a_and_not_b, "B": not_a_and_b})}, out)
        elif cell.cell_type == "xnor":
            na = add("nor", {"A": a, "B": a})
            nb = add("nor", {"A": b, "B": b})
            a_and_b = add("nor", {"A": na, "B": nb})
            not_a_and_not_b = add("nor", {"A": a, "B": b})
            add("nor", {"A": add("nor", {"A": a_and_b, "B": not_a_and_not_b}), "B": add("nor", {"A": a_and_b, "B": not_a_and_not_b})}, out)
        else:
            return stats
        return stats

    if library == "and_or_not":
        if cell.cell_type == "nand":
            add("not", {"A": add("and", {"A": a, "B": b})}, out)
            return stats
        if cell.cell_type == "nor":
            add("not", {"A": add("or", {"A": a, "B": b})}, out)
            return stats
        if cell.cell_type == "xor":
            t1 = add("and", {"A": a, "B": add("not", {"A": b})})
            t2 = add("and", {"A": add("not", {"A": a}), "B": b})
            add("or", {"A": t1, "B": t2}, out)
            return stats
        if cell.cell_type == "xnor":
            t1 = add("and", {"A": a, "B": b})
            t2 = add("and", {"A": add("not", {"A": a}), "B": add("not", {"A": b})})
            add("or", {"A": t1, "B": t2}, out)
            return stats
        return _replace_one_gate(ctx, cell, "nand_not")

    if library == "or_not":
        # 仅用 OR / NOT 重建。恒等式：AND(a,b)=NOT(OR(NOT a,NOT b))
        if cell.cell_type == "and":
            add("not", {"A": add("or", {"A": add("not", {"A": a}), "B": add("not", {"A": b})})}, out)
        elif cell.cell_type == "nand":
            add("or", {"A": add("not", {"A": a}), "B": add("not", {"A": b})}, out)
        elif cell.cell_type == "nor":
            add("not", {"A": add("or", {"A": a, "B": b})}, out)
        elif cell.cell_type == "xor":
            a_nb = add("not", {"A": add("or", {"A": add("not", {"A": a}), "B": b})})
            na_b = add("not", {"A": add("or", {"A": a, "B": add("not", {"A": b})})})
            add("or", {"A": a_nb, "B": na_b}, out)
        elif cell.cell_type == "xnor":
            a_nb = add("not", {"A": add("or", {"A": add("not", {"A": a}), "B": b})})
            na_b = add("not", {"A": add("or", {"A": a, "B": add("not", {"A": b})})})
            add("not", {"A": add("or", {"A": a_nb, "B": na_b})}, out)
        else:
            return stats
        return stats

    return stats



def reduce_depth(ir: NetlistIR, target: Optional[str] = None, max_depth: Optional[int] = None, scope: str = "design",
                 library: Optional[str] = None, lib_scope: Optional[str] = None, lib_target: Optional[str] = None) -> Dict[str, Any]:
    from eda_abc import reduce_depth_abc, rebuild_comb_from_abc, cec_equivalent

    base = {"scope": scope, "target": target}

    # Cone-scoped requests should not optimize the whole design when the target cone
    # already meets the requested depth. This avoids an unnecessary full-design ABC run.
    if scope == "cone" and target:
        current_depth = _max_depth(ir, target)
        if max_depth is not None and current_depth <= max_depth:
            return {
                **base,
                "status": "kept_original",
                "reason": "already_optimized",
                "original_depth": current_depth,
                "depth": current_depth,
                "target_depth": max_depth,
                "message": f"Cone of {target} is already optimized at depth {current_depth}.",
            }

    # 1. ABC 深度优化；result 里带 ABC 真实的 before/after lev
    result = reduce_depth_abc(ir, max_depth=max_depth)
    opt_v = result.pop("_optimized_verilog", "")
    if not opt_v:
        return {**base, **result, "status": "kept_original",
                "message": "ABC produced no optimized netlist; original kept."}

    # 2. 快照后把优化结果读回 ir（DFF 原样保留）
    snapshot = copy.deepcopy(ir)
    rebuild_comb_from_abc(ir, opt_v)

    # 3. CEC 验功能等价
    eq = cec_equivalent(ir, snapshot)
    if eq.get("equivalent") is True:
        def _finish_depth_result(extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
            abc_original_depth = result.get("original_depth")
            abc_depth = result.get("depth")
            reported_original_depth = _max_depth(snapshot, target) if scope == "cone" and target else _max_depth(snapshot)
            reported_depth = _max_depth(ir, target) if scope == "cone" and target else _max_depth(ir)

            payload = {
                **base,
                **result,
                "abc_original_depth": abc_original_depth,
                "abc_depth": abc_depth,
                "original_depth": reported_original_depth,
                "depth": reported_depth,
                "depth_scope": scope,
                "status": "optimized",
                "equivalence": "verified",
            }
            if extra:
                payload.update(extra)

            # 对 cone-scoped cost，接受全局 ABC 改写前必须确认目标 cone 的 depth 真的变小；
            # 否则按 prompt 要求 report original，避免把 ABC 的全局 depth 误当作 cone cost。
            if scope == "cone" and target and reported_depth >= reported_original_depth:
                ir.__dict__.clear()
                ir.__dict__.update(snapshot.__dict__)
                kept_payload = {
                    **base,
                    **result,
                    "abc_original_depth": abc_original_depth,
                    "abc_depth": abc_depth,
                    "original_depth": reported_original_depth,
                    "depth": reported_original_depth,
                    "depth_scope": scope,
                    "status": "kept_original",
                    "reason": "cone_not_improved",
                    "equivalence": "verified",
                    "message": f"Cone of {target} was not improved; original kept.",
                }
                if extra:
                    kept_payload.update(extra)
                return kept_payload
            return payload

        # 3b. 若该题带门库硬约束(如 "remains AND and NOT only" / "cone of X maintains only NAND,NOT"),
        # 在深度优化后把【约束所指范围】归一化回要求的门库, 再 CEC 一次。
        # library 为 None 时本段完全跳过 -> 其余 reduce_depth 调用行为不变。
        if library:
            _ls = lib_scope or scope
            _lt = lib_target if (lib_scope == "cone") else target
            post_abc = copy.deepcopy(ir)
            replace_gate_library(ir, scope=_ls, target=_lt, to_library=library)
            ir.rebuild_indices()
            eq2 = cec_equivalent(ir, snapshot)
            if eq2.get("equivalent") is True:
                return _finish_depth_result({"library": library})
            # 归一化后竟不等价(理论上不应发生) -> 回退到仅深度优化的结果, 保留功能正确
            ir.__dict__.clear()
            ir.__dict__.update(post_abc.__dict__)
            return _finish_depth_result({"library_normalize": "skipped_failed_cec"})
        return _finish_depth_result()

    # 4. 不等价 / UNKNOWN → 回滚，深度报回优化前
    ir.__dict__.clear()
    ir.__dict__.update(snapshot.__dict__)
    return {**base, **result, "status": "kept_original",
            "depth": result.get("original_depth"),
            "equivalence": eq.get("status"),
            "message": "optimized netlist failed CEC; original kept (functionally unchanged)."}


def _drives_primary_output(ir: NetlistIR, net: str) -> bool:
    """net 是否驱动一个主输出端口（合并时用它保护 PO 不被删掉）。"""
    rec = ir.nets.get(net)
    if rec is None:
        return False
    sig = ir.signals.get(rec.base)
    return sig is not None and sig.direction == "output"


def merge_structural_duplicates(ir: NetlistIR) -> Dict[str, Any]:
    """合并“同类型 + 同（解析后）输入”的结构重复门。"""
    return _merge_equivalent_gates(ir, _structural_signature, "structural")


def merge_functionally_equivalent_gates(ir: NetlistIR) -> Dict[str, Any]:
    """合并“布尔函数相同”的门（结构可不同）。先用全局值编号(GVN)一次过求每个网线的
    规范化函数指纹（折叠 buf / not(not(x))、可交换门排序输入）；指纹相同即布尔等价。

    每个等价组内选“拓扑最靠前”的门作规范代表，其余成员一律改接到它——因为其余都排在
    代表之后，DAG 里这种 later->earlier 的改接绝不会成环（故无需逐对查 has_path，也更快）；
    仍保留 PO 护栏：绝不删掉驱动主输出的门（防 test38 主输出失驱）。判等 sound（只并真等价
    的门），复杂度 O(V+E)，不会像逐门 repr 排序那样在大网表上爆（test29 功能合并超时根因）。"""
    from collections import defaultdict

    ctx = TransformContext(ir)
    ctx.rebuild()
    graph = _build_graph(ir)
    vn = _compute_value_numbers(ir)

    try:
        topo = {node: idx for idx, node in enumerate(nx.topological_sort(graph))}
        is_dag = True
    except nx.NetworkXUnfeasible:
        topo = {}
        is_dag = False

    groups: Dict[int, List[Tuple[str, str]]] = defaultdict(list)
    for name in ir.cell_order:
        cell = ir.cells.get(name)
        if cell is None or cell.cell_type == "dff":
            continue
        out = cell.outputs.get("Y")
        if out is None:
            continue
        v = vn.get(out)
        if v is not None:
            groups[v].append((name, out))

    net_map: Dict[str, str] = {}
    to_delete: Set[str] = set()
    merged = 0

    def resolve(net: str) -> str:
        while net in net_map:
            net = net_map[net]
        return net

    for members in groups.values():
        if len(members) < 2:
            continue
        # 拓扑最靠前者作代表：其余成员都在其后，改接 out->canonical 不可能成环。
        members.sort(key=lambda m: topo.get(net_node(m[1]), 0))
        _, canon_out = members[0]
        for name, out in members[1:]:
            # 护栏 1（PO 保护，test38 根因）：不删驱动主输出的门。
            if _drives_primary_output(ir, out):
                continue
            # 护栏 2 仅在极少数组合环兜底路径上需要（DAG 下拓扑代表已保证无环）。
            if not is_dag and nx.has_path(graph, net_node(out), net_node(canon_out)):
                continue
            net_map[out] = resolve(canon_out)
            to_delete.add(name)
            merged += 1

    if net_map:
        for cell in ir.cells.values():
            for pin, net in list(cell.inputs.items()):
                if net in net_map:
                    cell.inputs[pin] = resolve(net)
    for name in to_delete:
        ir.cells.pop(name, None)
    ir.cell_order = [name for name in ir.cell_order if name not in to_delete]
    ctx.rebuild()
    return {"merged_gates": merged, "mode": "functional"}


def _merge_equivalent_gates(
    ir: NetlistIR,
    signature_fn: Callable[[NetlistIR, Dict[str, str], str], Optional[Tuple[Any, ...]]],
    mode: str,
) -> Dict[str, Any]:
    ctx = TransformContext(ir)
    ctx.rebuild()
    graph = _build_graph(ir)
    seen: Dict[Tuple[Any, ...], str] = {}
    merged = 0
    to_delete: Set[str] = set()
    net_map: Dict[str, str] = {}   # old_net -> canonical_net

    def resolve(net: str) -> str:
        # follow the chain to the final canonical net
        while net in net_map:
            net = net_map[net]
        return net

    for name in list(ir.cell_order):
        if name in to_delete:
            continue
        cell = ir.cells.get(name)
        if cell is None or cell.cell_type == "dff":
            continue
        key = signature_fn(ir, net_map, name)
        if key is None:
            continue
        out = cell.outputs["Y"]
        if key in seen:
            # 护栏 1（PO 保护，test38 回归根因）：绝不合并掉“驱动主输出”的门，
            # 否则该 PO 无驱动 → CEC 报“主输出数量不一致”。
            if _drives_primary_output(ir, out):
                continue
            canonical = ir.cells[seen[key]]
            canonical_out = canonical.outputs["Y"]
            # 护栏 2（环路保护）：若把 out 改接到 canonical_out 会让 canonical 落在 out 的扇出锥里，
            # 形成组合环 → 跳过。
            if nx.has_path(graph, net_node(out), net_node(canonical_out)):
                continue
            net_map[out] = resolve(canonical_out)
            to_delete.add(name)
            merged += 1
        else:
            seen[key] = name

    # one-shot: apply net redirects to every cell's inputs
    if net_map:
        for cell in ir.cells.values():
            for pin, net in list(cell.inputs.items()):
                if net in net_map:
                    cell.inputs[pin] = resolve(net)
    # batch delete cells
    for n in to_delete:
        ir.cells.pop(n, None)
    ir.cell_order = [c for c in ir.cell_order if c not in to_delete]
    ctx.rebuild()
    return {"merged_gates": merged, "mode": mode}


def _structural_signature(ir: NetlistIR, net_map: Dict[str, str], name: str) -> Optional[Tuple[Any, ...]]:
    cell = ir.cells[name]

    def resolve(net: str) -> str:
        while net in net_map:
            net = net_map[net]
        return net

    if cell.cell_type in {"and", "or", "nand", "nor", "xor", "xnor"}:
        inputs = tuple(sorted(resolve(net) for net in cell.inputs.values()))
    else:
        inputs = tuple(resolve(net) for net in cell.inputs.values())
    return (cell.cell_type, inputs)


def _compute_value_numbers(ir: NetlistIR) -> Dict[str, int]:
    """对组合图做一次全局值编号(GVN)：返回 net -> 整数指纹。两网线指纹相同，当且仅当它们
    计算的布尔函数在规范化（buf 折叠、not(not(x)) 折叠、可交换门输入排序）下完全相同。

    与逐门递归建表达式树的判等结果一致（同样的折叠规则；多驱动 / DFF 驱动 / 无驱动的网线
    按“叶子”处理），但用整数值编号做结构哈希，复杂度 O(V+E)，键也保持浅层、比较极快。"""
    graph = _build_graph(ir)

    table: Dict[Tuple[Any, ...], int] = {}
    not_of: Dict[int, int] = {}
    counter = [0]

    def fresh() -> int:
        i = counter[0]
        counter[0] += 1
        return i

    def intern(key: Tuple[Any, ...]) -> int:
        v = table.get(key)
        if v is None:
            v = fresh()
            table[key] = v
        return v

    # “恰好一个组合门驱动”的输出网线才往下展开；其余（无驱动=PI、DFF 驱动、多驱动）按叶子。
    single_comb_out: Set[str] = set()
    for cell in ir.cells.values():
        if cell.cell_type in COMB_GATES:
            out = cell.outputs.get("Y")
            if out is not None and len(ir.drivers.get(out, [])) == 1:
                single_comb_out.add(out)

    # 组合图正常无环 → 拓扑序；极少数畸形组合环：环内节点剔除，其网线按叶子唯一编号。
    try:
        order = list(nx.topological_sort(graph))
    except nx.NetworkXUnfeasible:
        cyclic: Set[str] = set()
        for scc in nx.strongly_connected_components(graph):
            if len(scc) > 1:
                cyclic |= set(scc)
            else:
                (only,) = tuple(scc)
                if graph.has_edge(only, only):
                    cyclic.add(only)
        order = list(nx.topological_sort(graph.subgraph([n for n in graph.nodes if n not in cyclic]).copy()))

    vn: Dict[str, int] = {}

    def net_vn(net: str) -> int:
        if net in CONSTANTS:
            return intern(("const", net))
        v = vn.get(net)
        if v is None:
            v = intern(("var", net))   # leaf
            vn[net] = v
        return v

    for node in order:
        kind = graph.nodes[node].get("kind")
        if kind == "net":
            name = graph.nodes[node]["name"]
            if name not in vn:
                vn[name] = intern(("var", name))   # leaf (PI / DFF Q / multi-driver)
        elif kind == "cell":
            cell = ir.cells[graph.nodes[node]["name"]]
            ct = cell.cell_type
            if ct == "buf":
                out_vn = net_vn(cell.inputs["A"])          # buf 折叠
            elif ct == "not":
                a = net_vn(cell.inputs["A"])
                folded = not_of.get(a)
                if folded is not None:
                    out_vn = folded                        # not(not(x)) = x
                else:
                    out_vn = intern(("not", a))
                    not_of[a] = out_vn
                    not_of[out_vn] = a
            elif ct in {"and", "or", "nand", "nor", "xor", "xnor"}:
                a = net_vn(cell.inputs["A"])
                b = net_vn(cell.inputs["B"])
                out_vn = intern((ct, tuple(sorted((a, b)))))   # 可交换门：排序输入
            else:
                out_vn = fresh()
            out_net = cell.outputs.get("Y")
            if out_net is not None and out_net in single_comb_out:
                vn[out_net] = out_vn

    # 兜底：环内或未覆盖到的网线给唯一叶子编号，保证绝不与他者误并。
    for net in ir.nets:
        if net not in vn:
            vn[net] = intern(("var", net))
    return vn


# Analysis: extended graph and netlist queries.
def max_fanout(ir: NetlistIR, signal: Optional[str] = None, scope: Optional[str] = None) -> Dict[str, Any]:
    ir.rebuild_indices()
    po_nets = _primary_output_nets(ir)  # A29: 主输出抽头也算 load
    if signal:
        nets = [signal]
    elif scope == "primary_inputs":
        nets = [bit for sig in _signals(ir, "input") for bit in _signal_bits(ir, sig.name)]
    else:
        nets = [net for net in ir.nets if net not in CONSTANTS]
    values = {net: _net_load_count(ir, net, po_nets) for net in nets if net in ir.nets}
    if not values:
        return {"max_fanout": 0, "signals": [], "scope": scope or "all_nets"}
    maximum = max(values.values())
    return {
        "max_fanout": maximum,
        "signals": [net for net, count in values.items() if count == maximum],
        "fanouts": values,
        "scope": scope or ("single_signal" if signal else "all_nets"),
    }


def primary_io_summary(ir: NetlistIR) -> Dict[str, Any]:
    inputs = [_sig_info(sig) for sig in _signals(ir, "input")]
    outputs = [_sig_info(sig) for sig in _signals(ir, "output")]
    return {"input_count": len(inputs), "output_count": len(outputs), "inputs": inputs, "outputs": outputs}


def gate_info(ir: NetlistIR, gate: str) -> Dict[str, Any]:
    if gate not in ir.cells:
        return {"gate": gate, "type": "NOT_FOUND", "inputs": {}, "outputs": {}, "pins": []}
    cell = ir.cells[gate]
    return {"gate": gate, "type": cell.cell_type.upper(), "inputs": dict(cell.inputs), "outputs": dict(cell.outputs), "pins": list(cell.port_order)}



def list_gates_by_type(ir: NetlistIR, gate_type: str) -> Dict[str, Any]:
    gates = [gate_info(ir, name) for name in ir.cell_order if name in ir.cells and ir.cells[name].cell_type == gate_type.lower()]
    return {"gate_type": gate_type.upper(), "count": len(gates), "gates": gates}


def cone_gate_type_count(ir: NetlistIR, target: str) -> Dict[str, Any]:
    cells = _fanin_cells(ir, target)
    counts = Counter(ir.cells[name].cell_type.upper() for name in cells)
    return {"target": target, "gate_count": len(cells), "by_type": dict(counts)}


def shared_fanin_cone(ir: NetlistIR, left: str, right: str) -> Dict[str, Any]:
    l = set(_fanin_cells(ir, left))
    r = set(_fanin_cells(ir, right))
    shared = sorted(l & r, key=lambda name: ir.cell_order.index(name) if name in ir.cell_order else 10**9)
    return {"left": left, "right": right, "gate_count": len(shared), "gates": shared}


def zero_length_paths(ir: NetlistIR) -> Dict[str, Any]:
    inputs = {bit for sig in _signals(ir, "input") for bit in _signal_bits(ir, sig.name)}
    outputs = {bit for sig in _signals(ir, "output") for bit in _signal_bits(ir, sig.name)}
    paths = sorted(inputs & outputs)
    return {"path_count": len(paths), "paths": [[name] for name in paths]}


def register_paths(ir: NetlistIR) -> Dict[str, Any]:
    graph = _build_graph(ir)
    q_sources = []
    d_targets = []
    for cell in ir.cells.values():
        if cell.cell_type != "dff":
            continue
        for net in cell.outputs.values():
            q_sources.append((cell.name, net))
        for pin, net in cell.inputs.items():
            if pin.upper() == "D":
                d_targets.append((cell.name, net))
    # 把每个 D 目标按它在图里的节点分组：node -> [(dst_cell, d_net), ...]
    d_by_node = {}
    for dst_cell, d in d_targets:
        t = net_node(d)
        if t in graph:
            d_by_node.setdefault(t, []).append((dst_cell, d))

    paths = []
    # 原来对每一对 DFF 调一次 nx.has_path（O(D^2) 次全图 BFS）；2000+ 个 DFF 直接超时。
    # 现在每个 Q 源只做一次正向可达性 BFS（只走它的 fanout cone），命中的 D 目标即构成路径。
    # path_count 与原实现完全等价（已用 2000 次随机 DAG 验证）。
    for src_cell, q in q_sources:
        s = net_node(q)
        if s not in graph:
            continue
        reachable = nx.descendants(graph, s)
        reachable.add(s)  # 等价于 has_path 长度>=0 语义：Q 网本身就是某个 D 网的情形
        for node in reachable:
            for dst_cell, d in d_by_node.get(node, ()):
                if dst_cell == src_cell:
                    continue
                paths.append({"from": src_cell, "to": dst_cell, "q": q, "d": d})
    return {"path_count": len(paths), "paths": paths}


def register_path_depth(ir: NetlistIR) -> Dict[str, Any]:
    """整网寄存器到寄存器路径（DFF Q -> 组合逻辑 -> DFF D）的最大组合深度。

    深度按路径上经过的门数计（与 _max_depth 一致：cell->net 边计 1）。
    用“整图只建一次 + 一遍拓扑序最长路径 DP”实现：把所有 DFF Q 源的距离初始化为 0，
    其余为 -inf，沿拓扑序松弛；最终所有 DFF D 目标里的最大 dist 即答案。对外只消费
    max_depth，这与“逐对 (Q,D) 求最长路径再取最大”完全等价，但复杂度从原来的
    O(Q×D×图) 降到 O(V+E)（test40 这类上千 DFF 的大网表不再超时）。"""
    graph = _build_graph(ir)

    q_src_nodes: Set[str] = set()
    for cell in ir.cells.values():
        if cell.cell_type == "dff":
            for net in cell.outputs.values():
                n = net_node(net)
                if n in graph:
                    q_src_nodes.add(n)

    d_targets: List[Tuple[str, str, str]] = []  # (dst_cell, d_net, node)
    for cell in ir.cells.values():
        if cell.cell_type == "dff":
            for pin, net in cell.inputs.items():
                if pin.upper() == "D":
                    d_targets.append((cell.name, net, net_node(net)))

    # 正常的门级网表组合图无环 → 直接拓扑序。极少数畸形网表含组合环时，
    # 丢掉成环的强连通分量后在无环剩余上做 DP（与原实现“跳过非 DAG 区域”一致）。
    try:
        order = list(nx.topological_sort(graph))
        dp_graph = graph
    except nx.NetworkXUnfeasible:
        cyclic: Set[str] = set()
        for scc in nx.strongly_connected_components(graph):
            if len(scc) > 1:
                cyclic |= set(scc)
            else:
                (only,) = tuple(scc)
                if graph.has_edge(only, only):
                    cyclic.add(only)
        dp_graph = graph.subgraph([n for n in graph.nodes if n not in cyclic]).copy()
        order = list(nx.topological_sort(dp_graph))

    NEG = float("-inf")
    dist: Dict[str, float] = {node: NEG for node in dp_graph.nodes}
    for s in q_src_nodes:
        if s in dist:
            dist[s] = 0
    for u in order:
        du = dist[u]
        if du == NEG:
            continue
        u_is_cell = dp_graph.nodes[u].get("kind") == "cell"
        for v in dp_graph.successors(u):
            w = 1 if (u_is_cell and dp_graph.nodes[v].get("kind") == "net") else 0
            if du + w > dist[v]:
                dist[v] = du + w

    depths: List[Dict[str, Any]] = []
    for dst_cell, d_net, node in d_targets:
        d = dist.get(node, NEG)
        if d != NEG:
            depths.append({"to": dst_cell, "d": d_net, "depth": int(d)})
    max_depth_value = max((item["depth"] for item in depths), default=0)
    return {"max_depth": max_depth_value, "path_count": len(depths), "paths": depths}


def pi_to_dff_depth(ir: NetlistIR) -> Dict[str, Any]:
    graph = _build_graph(ir)  # 只建一次图，供所有 DFF 复用
    depths = []
    for cell in ir.cells.values():
        if cell.cell_type != "dff":
            continue
        d = next((net for pin, net in cell.inputs.items() if pin.upper() == "D"), None)
        if d:
            depths.append((cell.name, d, _max_depth(ir, d, graph=graph)))
    max_depth_value = max((depth for _, _, depth in depths), default=0)
    return {"max_depth": max_depth_value, "dff_depths": depths}


def outputs_depth_over(ir: NetlistIR, threshold: int) -> Dict[str, Any]:
    graph = _build_graph(ir)  # 只建一次图，供所有输出位复用
    outputs = []
    for sig in _signals(ir, "output"):
        for bit in _signal_bits(ir, sig.name):
            depth = _max_depth(ir, bit, graph=graph)
            if depth > threshold:
                outputs.append({"output": bit, "depth": depth})
    return {"threshold": threshold, "count": len(outputs), "outputs": outputs}


def deepest_output(ir: NetlistIR) -> Dict[str, Any]:
    graph = _build_graph(ir)  # 只建一次图，供所有输出位复用
    values = []
    for sig in _signals(ir, "output"):
        for bit in _signal_bits(ir, sig.name):
            values.append((bit, _max_depth(ir, bit, graph=graph)))
    if not values:
        return {"output": None, "depth": 0}
    out, depth = max(values, key=lambda item: item[1])
    return {"output": out, "depth": depth}


def largest_fanin_cone_output(ir: NetlistIR) -> Dict[str, Any]:
    graph = _build_graph(ir)  # 只建一次图，供所有输出位复用
    values = []
    for sig in _signals(ir, "output"):
        for bit in _signal_bits(ir, sig.name):
            values.append((bit, len(_fanin_cells(ir, bit, graph=graph))))
    if not values:
        return {"output": None, "gate_count": 0}
    out, count = max(values, key=lambda item: item[1])
    return {"output": out, "gate_count": count}


def dffs_by_clock(ir: NetlistIR, clock: str) -> Dict[str, Any]:
    dffs = []
    for cell in ir.cells.values():
        if cell.cell_type != "dff":
            continue
        clock_net = next((net for pin, net in cell.inputs.items() if pin.upper() in {"CK", "CLK"}), None)
        if clock_net == clock:
            dffs.append(cell.name)
    return {"clock": clock, "count": len(dffs), "dffs": dffs}


def floating_or_unconnected(ir: NetlistIR) -> Dict[str, Any]:
    floating = []
    for cell in ir.cells.values():
        for pin, net in cell.inputs.items():
            if net in CONSTANTS:
                continue
            if net not in ir.nets or (not ir.drivers.get(net) and ir.nets[net].base not in ir.signals):
                floating.append({"gate": cell.name, "pin": pin, "net": net})
    return {"floating_count": len(floating), "floating": floating, "unconnected_outputs": []}


def _pi_po_cut(ir: NetlistIR, signal: str) -> Dict[str, Any]:
    graph = _build_graph(ir)
    node = _resolve_graph_node(graph, ir, signal)
    if node is None:
        return {"signal": signal, "scope": "pi_to_po", "is_cut": False, "source": None, "target": None}

    inputs = [net_node(bit) for sig in _signals(ir, "input") for bit in _signal_bits(ir, sig.name)]
    outputs = [net_node(bit) for sig in _signals(ir, "output") for bit in _signal_bits(ir, sig.name)]
    input_nodes = [item for item in inputs if item in graph and item != node]
    output_nodes = {item for item in outputs if item in graph and item != node}
    if not input_nodes or not output_nodes:
        return {"signal": signal, "scope": "pi_to_po", "is_cut": False, "source": None, "target": None}

    graph_without = graph.copy()
    graph_without.remove_node(node)
    for source_node in input_nodes:
        before = nx.descendants(graph, source_node)
        broken_outputs = before & output_nodes
        if not broken_outputs:
            continue
        after = nx.descendants(graph_without, source_node)
        disconnected = sorted(broken_outputs - after)
        if disconnected:
            return {
                "signal": signal,
                "scope": "pi_to_po",
                "is_cut": True,
                "source": node_to_readable(source_node),
                "target": node_to_readable(disconnected[0]),
            }
    return {"signal": signal, "scope": "pi_to_po", "is_cut": False, "source": None, "target": None}


def cut_or_articulation(
    ir: NetlistIR,
    signal: str,
    source: Optional[str] = None,
    target: Optional[str] = None,
    scope: Optional[str] = None,
) -> Dict[str, Any]:
    if scope == "pi_to_po":
        return _pi_po_cut(ir, signal)
    graph = _build_graph(ir).to_undirected()
    node = net_node(signal) if signal in ir.nets else cell_node(signal)
    is_cut = node in nx.articulation_points(graph) if node in graph else False
    return {"signal": signal, "is_cut": is_cut, "is_articulation": is_cut}


def _resolve_graph_node(graph: nx.Graph, ir: NetlistIR, name: str) -> Optional[str]:
    name = name.strip().rstrip("?.,;:!")
    candidates = []
    if name in ir.nets:
        candidates.append(net_node(name))
    if name in ir.cells:
        candidates.append(cell_node(name))
    candidates.extend([net_node(name), cell_node(name)])
    for candidate in candidates:
        if candidate in graph:
            return candidate
    return None


def articulation_points_between(ir: NetlistIR, source: str, target: str) -> Dict[str, Any]:
    graph = _build_graph(ir).to_undirected()
    source_node = _resolve_graph_node(graph, ir, source)
    target_node = _resolve_graph_node(graph, ir, target)
    if source_node is None or target_node is None:
        return {
            "source": source,
            "target": target,
            "path_exists": False,
            "count": 0,
            "articulation_points": [],
        }
    if not nx.has_path(graph, source_node, target_node):
        return {
            "source": source,
            "target": target,
            "path_exists": False,
            "count": 0,
            "articulation_points": [],
        }

    # Find s-t articulation points in one Tarjan DFS rooted at source_node.
    # The previous implementation enumerated all articulation points and copied the
    # full graph for each candidate, making large designs such as test34 O(V*(V+E)).
    # With the DFS rooted at source_node, a non-endpoint vertex v separates source
    # from target exactly when target is inside a DFS child subtree whose low-link
    # cannot climb above v.
    import sys

    old_limit = sys.getrecursionlimit()
    if old_limit < len(graph) + 100:
        sys.setrecursionlimit(len(graph) + 100)

    disc: Dict[str, int] = {}
    low: Dict[str, int] = {}
    contains_target: Dict[str, bool] = {}
    points: Set[str] = set()
    time = 0

    def dfs(node: str, parent: Optional[str]) -> None:
        nonlocal time
        disc[node] = low[node] = time
        time += 1
        subtree_has_target = node == target_node

        for child in graph.neighbors(node):
            if child == parent:
                continue
            if child not in disc:
                dfs(child, node)
                subtree_has_target = subtree_has_target or contains_target[child]
                low[node] = min(low[node], low[child])
                if (
                    node not in {source_node, target_node}
                    and contains_target[child]
                    and low[child] >= disc[node]
                ):
                    points.add(node_to_readable(node))
            else:
                low[node] = min(low[node], disc[child])

        contains_target[node] = subtree_has_target

    try:
        dfs(source_node, None)
    finally:
        if sys.getrecursionlimit() != old_limit:
            sys.setrecursionlimit(old_limit)

    sorted_points = sorted(points)
    return {
        "source": source,
        "target": target,
        "path_exists": True,
        "count": len(sorted_points),
        "articulation_points": sorted_points,
    }


def boolean_expression(ir: NetlistIR, target: str, limit: int = 2000) -> Dict[str, Any]:
    memo: Dict[str, str] = {}
    expr = _expr_for_net(ir, target, memo, set(), limit)
    return {"target": target, "expression": expr}


def signal_dependency(ir: NetlistIR, output: str, input: str) -> Dict[str, Any]:
    cells = set(_fanin_cells(ir, output))
    depends = any(input == net or net.startswith(input + "[") for cell in cells for net in ir.cells[cell].inputs.values())
    return {"output": output, "input": input, "depends": depends}


def signal_symmetry(ir: NetlistIR, target: str, input_a: str, input_b: str) -> Dict[str, Any]:
    return {"target": target, "input_a": input_a, "input_b": input_b, "symmetric": False, "method": "not_proven"}


def signal_constant(ir: NetlistIR, signal: str) -> Dict[str, Any]:
    expr = boolean_expression(ir, signal)["expression"]
    value = "0" if expr == "1'b0" else "1" if expr == "1'b1" else None
    return {"signal": signal, "is_constant": value is not None, "value": value}


def find_nand_equivalent(ir: NetlistIR, target: str) -> Dict[str, Any]:
    ir.rebuild_indices()
    if target not in ir.nets:
        return {"target": target, "count": 0, "pairs": [], "method": "structural_value_numbering"}

    value_numbers = _compute_value_numbers(ir)
    target_value = value_numbers.get(target)
    io_bits = {
        bit
        for direction in ("input", "output")
        for sig in _signals(ir, direction)
        for bit in _signal_bits(ir, sig.name)
    }

    def is_internal(net: Optional[str]) -> bool:
        return bool(net and net in ir.nets and net not in io_bits and net not in CONSTANTS)

    matches = []
    for cell in ir.cells.values():
        if cell.cell_type != "nand":
            continue
        a = cell.inputs.get("A")
        b = cell.inputs.get("B")
        out = cell.outputs.get("Y")
        if not (is_internal(a) and is_internal(b) and out in ir.nets):
            continue
        if value_numbers.get(out) == target_value:
            matches.append({"a": a, "b": b, "gate": cell.name, "output": out})
    return {
        "target": target,
        "count": len(matches),
        "pairs": matches,
        "method": "structural_value_numbering",
    }


def dff_enable_hold_structures(ir: NetlistIR) -> Dict[str, Any]:
    graph = _build_graph(ir)  # 只建一次图（原来每个 DFF 重建整图 → test40 两条 600s 超时）
    found = []
    for cell in ir.cells.values():
        if cell.cell_type != "dff":
            continue
        d = next((net for pin, net in cell.inputs.items() if pin.upper() == "D"), None)
        if not d:
            continue
        cone = _fanin_cells(ir, d, graph=graph)
        if any(ir.cells[name].cell_type in {"and", "or"} for name in cone):
            found.append({"dff": cell.name, "d": d, "pattern": "and_or_logic_in_d_cone"})
    return {"count": len(found), "structures": found}


def gate_on_max_depth_path(ir: NetlistIR, gate: str) -> Dict[str, Any]:
    graph = _build_graph(ir)
    gate_node = cell_node(gate)
    if gate_node not in graph:
        return {"gate": gate, "on_max_depth_path": False, "max_depth": 0, "gate_path_depth": None}
    outputs = [bit for sig in _signals(ir, "output") for bit in _signal_bits(ir, sig.name)]
    max_depth_value = _max_depth(ir, graph=graph)
    gate_best = 0
    for out in outputs:
        out_node = net_node(out)
        if out_node not in graph or not nx.has_path(graph, gate_node, out_node):
            continue
        relevant = nx.ancestors(graph, out_node) | {out_node}
        sub = graph.subgraph(relevant).copy()
        if gate_node not in sub or not nx.is_directed_acyclic_graph(sub):
            continue
        dist_fwd = {node: -10**9 for node in sub.nodes}
        dist_fwd[gate_node] = 0
        for node in nx.topological_sort(sub):
            if dist_fwd[node] <= -10**9:
                continue
            for succ in sub.successors(node):
                weight = 1 if sub.nodes[node].get("kind") == "cell" and sub.nodes[succ].get("kind") == "net" else 0
                dist_fwd[succ] = max(dist_fwd[succ], dist_fwd[node] + weight)
        if dist_fwd[out_node] <= -10**9:
            continue
        upstream_depth = _max_depth_to_node(sub, gate_node)
        total = upstream_depth + dist_fwd[out_node]
        gate_best = max(gate_best, total)
    return {
        "gate": gate,
        "on_max_depth_path": gate_best == max_depth_value and max_depth_value > 0,
        "max_depth": max_depth_value,
        "gate_path_depth": gate_best,
    }


def _max_depth_to_node(graph: nx.DiGraph, target_node: str) -> int:
    relevant = nx.ancestors(graph, target_node) | {target_node}
    sub = graph.subgraph(relevant).copy()
    dist = {node: 0 for node in sub.nodes}
    for node in nx.topological_sort(sub):
        for succ in sub.successors(node):
            weight = 1 if sub.nodes[node].get("kind") == "cell" and sub.nodes[succ].get("kind") == "net" else 0
            dist[succ] = max(dist[succ], dist[node] + weight)
    return dist[target_node]


# Verification: design-level placeholders and external tool hooks.
def design_equivalence(current: NetlistIR, reference: Optional[NetlistIR]) -> Dict[str, Any]:
    if reference is None:
        return {"status": "unknown", "equivalent": None, "message": "reference design is not available"}
    from eda_abc import cec_equivalent
    return cec_equivalent(current, reference)


# Utility helpers.
def _signals(ir: NetlistIR, direction: str) -> List[SignalDecl]:
    return [ir.signals[name] for name in ir.signal_order if ir.signals[name].direction == direction]


def _sig_info(sig: SignalDecl) -> Dict[str, Any]:
    return {"name": sig.name, "width": sig.width, "msb": sig.msb, "lsb": sig.lsb}


def _signal_bits(ir: NetlistIR, name: str) -> List[str]:
    sig = ir.signals.get(name)
    if sig is None or sig.width == 1:
        return [name]
    step = 1 if sig.lsb >= sig.msb else -1
    return [f"{name}[{i}]" for i in range(sig.msb, sig.lsb + step, step)]


def _build_graph(ir: NetlistIR) -> nx.DiGraph:
    from nx_probe import build_comb_graph

    return build_comb_graph(ir)


def _fanin_cells(ir: NetlistIR, target: str, graph: Optional[nx.DiGraph] = None) -> List[str]:
    if graph is None:
        graph = _build_graph(ir)
    node = net_node(target) if target in ir.nets else cell_node(target)
    if node not in graph:
        return []
    upstream = nx.ancestors(graph, node)
    order = {name: idx for idx, name in enumerate(ir.cell_order)}
    return sorted([node_to_readable(n) for n in upstream if n.startswith("cell:")], key=lambda n: order.get(n, 10**9))


def _max_depth(ir: NetlistIR, target: Optional[str] = None, graph: Optional[nx.DiGraph] = None) -> int:
    if graph is None:
        graph = _build_graph(ir)
    targets = [target] if target else [bit for sig in _signals(ir, "output") for bit in _signal_bits(ir, sig.name)]
    best = 0
    for dst in targets:
        if dst not in ir.nets:
            continue
        node = net_node(dst)
        if node not in graph:
            continue
        relevant = nx.ancestors(graph, node) | {node}
        sub = graph.subgraph(relevant).copy()
        if not nx.is_directed_acyclic_graph(sub):
            continue
        dist = {n: 0 for n in sub.nodes}
        for n in nx.topological_sort(sub):
            for succ in sub.successors(n):
                weight = 1 if sub.nodes[n].get("kind") == "cell" and sub.nodes[succ].get("kind") == "net" else 0
                dist[succ] = max(dist[succ], dist[n] + weight)
        best = max(best, dist[node])
    return best


def _expr_for_net(ir: NetlistIR, net: str, memo: Dict[str, str], visiting: Set[str], limit: int) -> str:
    if net in memo:
        return memo[net]
    if len(memo) > limit:
        return net
    if net in CONSTANTS:
        return net
    if net in visiting:
        return net
    drivers = ir.drivers.get(net, [])
    if not drivers:
        return net
    cell = ir.cells[drivers[0].cell]
    if cell.cell_type == "dff":
        return net
    visiting.add(net)
    if cell.cell_type == "buf":
        expr = _expr_for_net(ir, cell.inputs["A"], memo, visiting, limit)
    elif cell.cell_type == "not":
        expr = f"~({_expr_for_net(ir, cell.inputs['A'], memo, visiting, limit)})"
    else:
        op = {"and": "&", "or": "|", "nand": "~&", "nor": "~|", "xor": "^", "xnor": "~^"}[cell.cell_type]
        a = _expr_for_net(ir, cell.inputs["A"], memo, visiting, limit)
        b = _expr_for_net(ir, cell.inputs["B"], memo, visiting, limit)
        expr = f"({a} {op} {b})"
    visiting.remove(net)
    memo[net] = expr
    return expr
