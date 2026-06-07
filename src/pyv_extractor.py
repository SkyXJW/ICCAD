import re
import shutil
import os
from pathlib import Path
from typing import Optional, Tuple, List

from pyverilog.vparser.parser import parse, VerilogParser
from pyverilog.vparser.ast import (
    ModuleDef,
    Decl,
    Input,
    Output,
    Inout,
    Wire,
    Ioport,
    InstanceList,
    Identifier,
    IntConst,
    Pointer,
    Partselect,
)

from ir import NetlistIR, Cell


PRIMITIVE_GATES = {"and", "or", "nand", "nor", "not", "buf", "xor", "xnor"}
SUPPORTED_STRUCTURAL_CELLS = PRIMITIVE_GATES | {"dff"}


DIRECTIVE_RE = re.compile(r"^\s*`([A-Za-z_]\w*)", re.MULTILINE)


def find_compiler_directives(text: str) -> List[Tuple[int, str]]:
    return [
        (text.count("\n", 0, match.start()) + 1, match.group(1))
        for match in DIRECTIVE_RE.finditer(text)
    ]


def parse_verilog_file(path: str):
    """
    Parse a Verilog file through Pyverilog.

    Pyverilog normally shells out to iverilog for preprocessing. If iverilog is
    unavailable, only plain Verilog without compiler directives is accepted.
    This avoids validating a non-preprocessed design and reporting a false OK.
    """
    if shutil.which("iverilog") is not None:
        return parse([path], debug=False)

    text = Path(path).read_text()
    directives = find_compiler_directives(text)
    if directives:
        detail = ", ".join(f"line {line}: `{name}" for line, name in directives[:8])
        if len(directives) > 8:
            detail += f", ... ({len(directives)} total)"
        raise RuntimeError(
            "iverilog is required to preprocess Verilog compiler directives before "
            f"lossless parsing, but it was not found. Directives in {path}: {detail}"
        )

    return VerilogParser().parse(text, debug=False), ()


def parse_intconst(node) -> int:
    """
    Parse a Pyverilog IntConst into an integer.

    Supported examples:
      3
      4'd3
      4'b1010
      8'hff
      4'sd3
      8'b1010_0011
    """
    if not isinstance(node, IntConst):
        raise ValueError(f"unsupported constant expression for width/index: {node}")

    v = node.value.replace("_", "")

    if "'" not in v:
        return int(v, 10)

    _, rhs = v.split("'", 1)

    # signed form: 4'sd3
    if rhs and rhs[0].lower() == "s":
        rhs = rhs[1:]

    if not rhs:
        raise ValueError(f"unsupported IntConst format: {node.value}")

    base = rhs[0].lower()
    num = rhs[1:]

    if base == "d":
        return int(num, 10)
    if base == "b":
        return int(num, 2)
    if base == "h":
        return int(num, 16)
    if base == "o":
        return int(num, 8)

    raise ValueError(f"unsupported IntConst base in {node.value}")


def get_width_info(decl_node) -> Tuple[int, int, int]:
    """
    Return width, msb, lsb.
    Pyverilog stores width as decl_node.width, usually Width(msb, lsb).
    """
    width_node = getattr(decl_node, "width", None)
    if width_node is None:
        return 1, 0, 0

    msb = parse_intconst(width_node.msb)
    lsb = parse_intconst(width_node.lsb)
    width = abs(msb - lsb) + 1
    return width, msb, lsb


def range_indices(msb: int, lsb: int) -> List[int]:
    """
    Return bit indices in written Verilog order.

    [3:0] -> [3, 2, 1, 0]
    [0:3] -> [0, 1, 2, 3]
    """
    step = 1 if lsb >= msb else -1
    return list(range(msb, lsb + step, step))


def sanitize_name_fragment(s: str) -> str:
    """
    Convert a net name such as out_bus[3] into a safe instance-name fragment.
    """
    s = re.sub(r"[^A-Za-z0-9_]", "_", s)
    if not s or not re.match(r"[A-Za-z_]", s[0]):
        s = "_" + s
    return s


def intconst_to_bit_nets(node: IntConst) -> List[str]:
    """
    Convert an IntConst into bit-level constant nets in MSB-to-LSB order.

    Examples:
      1'b1    -> ["1'b1"]
      1'b0    -> ["1'b0"]
      4'b1010 -> ["1'b1", "1'b0", "1'b1", "1'b0"]
      4'hA    -> ["1'b1", "1'b0", "1'b1", "1'b0"]

    Unsized 0/1 are normalized to 1'b0 / 1'b1.
    Larger unsized constants are rejected to avoid ambiguous width inference.
    """
    v = node.value.replace("_", "")

    if "'" not in v:
        value = int(v, 10)
        if value == 0:
            return ["1'b0"]
        if value == 1:
            return ["1'b1"]
        raise ValueError(
            f"unsized constant {node.value} is not supported in gate pins; "
            f"use an explicitly sized constant such as 4'd{value}"
        )

    width_text, rhs = v.split("'", 1)

    if not width_text:
        raise ValueError(f"constant width is required: {node.value}")

    width = int(width_text)

    # signed form: 4'sd3
    if rhs and rhs[0].lower() == "s":
        rhs = rhs[1:]

    if not rhs:
        raise ValueError(f"unsupported constant format: {node.value}")

    base = rhs[0].lower()
    digits = rhs[1:]

    if any(ch in digits.lower() for ch in "xz?"):
        raise ValueError(f"X/Z constants are not supported in current IR: {node.value}")

    if base == "b":
        bitstr = digits.zfill(width)[-width:]
    elif base == "h":
        bitstr = bin(int(digits, 16))[2:].zfill(width)[-width:]
    elif base == "d":
        bitstr = bin(int(digits, 10))[2:].zfill(width)[-width:]
    elif base == "o":
        bitstr = bin(int(digits, 8))[2:].zfill(width)[-width:]
    else:
        raise ValueError(f"unsupported constant base in {node.value}")

    return [f"1'b{b}" for b in bitstr]


def signal_bits(ir: NetlistIR, name: str) -> List[str]:
    """
    Expand a declared signal name into bit-level nets.

    input [3:0] a -> ["a[3]", "a[2]", "a[1]", "a[0]"]
    input [0:3] a -> ["a[0]", "a[1]", "a[2]", "a[3]"]
    scalar a      -> ["a"]

    If the signal is undeclared, treat it as scalar. validate_ir() can catch it later.
    """
    sig = ir.signals.get(name)

    if sig is None or sig.width == 1:
        return [name]

    return [f"{name}[{i}]" for i in range_indices(sig.msb, sig.lsb)]


def expr_to_net(expr) -> str:
    """
    Scalar-only conversion.

    Use this for scalar contexts, especially DFF pins.

    For primitive gates, use expr_to_nets(ir, expr), because primitive gates may
    be vector instances such as:
      and U_vec(y[3:0], a[3:0], b[3:0]);
    """
    if isinstance(expr, Identifier):
        return expr.name

    if isinstance(expr, IntConst):
        bits = intconst_to_bit_nets(expr)
        if len(bits) != 1:
            raise ValueError(f"multi-bit constant {expr.value} is not scalar")
        return bits[0]

    if isinstance(expr, Pointer):
        base = expr_to_net(expr.var)
        idx = parse_intconst(expr.ptr)
        return f"{base}[{idx}]"

    if isinstance(expr, Partselect):
        base = expr_to_net(expr.var)
        msb = parse_intconst(expr.msb)
        lsb = parse_intconst(expr.lsb)

        if msb == lsb:
            return f"{base}[{msb}]"

        raise ValueError(
            f"multi-bit part-select {base}[{msb}:{lsb}] is not scalar; "
            f"use expr_to_nets() for vector primitive gates"
        )

    raise ValueError(f"unsupported expression in scalar gate-level context: {expr}")


def expr_to_nets(ir: NetlistIR, expr) -> List[str]:
    """
    Vector-aware expression conversion for primitive gate pins.

    Examples:
      a          -> ["a"] if scalar, or expanded bits if vector
      a[0]       -> ["a[0]"]
      a[3:0]     -> ["a[3]", "a[2]", "a[1]", "a[0]"]
      1'b1       -> ["1'b1"]
      4'b1010    -> ["1'b1", "1'b0", "1'b1", "1'b0"]
    """
    if isinstance(expr, Identifier):
        return signal_bits(ir, expr.name)

    if isinstance(expr, IntConst):
        return intconst_to_bit_nets(expr)

    if isinstance(expr, Pointer):
        base = expr_to_net(expr.var)
        idx = parse_intconst(expr.ptr)
        return [f"{base}[{idx}]"]

    if isinstance(expr, Partselect):
        base = expr_to_net(expr.var)
        msb = parse_intconst(expr.msb)
        lsb = parse_intconst(expr.lsb)
        return [f"{base}[{i}]" for i in range_indices(msb, lsb)]

    raise ValueError(f"unsupported expression in primitive gate pin: {expr}")


def fit_port_width(
    bits: List[str],
    width: int,
    *,
    allow_broadcast: bool,
    inst_name: str,
    pin_name: str,
) -> List[str]:
    """
    Normalize a port bit list to target width.

    Output pins must exactly match target width.
    Input pins may be scalar-broadcast to vector width.

    Example:
      and U(y[3:0], a[3:0], 1'b1)
    broadcasts 1'b1 to all four input bits.
    """
    if len(bits) == width:
        return bits

    if allow_broadcast and len(bits) == 1:
        return bits * width

    raise ValueError(
        f"width mismatch at {inst_name}.{pin_name}: "
        f"got width {len(bits)}, expected {width}, bits={bits}"
    )


def expanded_cell_name(inst_name: str, out_net: str, width: int) -> str:
    """
    Generate a stable scalar cell name for expanded vector primitive gates.

    Scalar instance:
      U1 -> U1

    Vector instance:
      U_vec(y[3:0], ...)
      -> U_vec__y_3, U_vec__y_2, ...
    """
    if width == 1:
        return inst_name

    return f"{inst_name}__{sanitize_name_fragment(out_net)}"


def add_decl_to_ir(ir: NetlistIR, decl_node) -> None:
    """
    Handle Input / Output / Wire declarations.
    """
    if isinstance(decl_node, Input):
        kind = "input"
    elif isinstance(decl_node, Output):
        kind = "output"
    elif isinstance(decl_node, Inout):
        kind = "inout"
    elif isinstance(decl_node, Wire):
        kind = "wire"
    else:
        return

    width, msb, lsb = get_width_info(decl_node)
    ir.add_signal(
        decl_node.name,
        kind,
        width=width,
        msb=msb,
        lsb=lsb,
        lineno=getattr(decl_node, "lineno", None),
    )


def handle_ioport(ir: NetlistIR, ioport: Ioport) -> None:
    """
    Handle ANSI-style module ports:
      module top(input a, output y);
    """
    first = ioport.first
    add_decl_to_ir(ir, first)

    if isinstance(first, (Input, Output)):
        if first.name not in ir.port_order:
            ir.port_order.append(first.name)


def handle_decl(ir: NetlistIR, decl: Decl) -> None:
    """
    Handle non-ANSI declarations:
      input a, b;
      output y;
      wire n1, n2;
    """
    for item in decl.list:
        add_decl_to_ir(ir, item)

        if isinstance(item, (Input, Output)):
            if item.name not in ir.port_order:
                ir.port_order.append(item.name)


def make_primitive_cells(ir: NetlistIR, inst_list: InstanceList, inst) -> List[Cell]:
    """
    Convert a primitive gate instance into one or more scalar Cells.

    Scalar example:
      and U1(n1, a, b)
        -> Cell U1

    Vector example:
      and U_vec(y[3:0], a[3:0], b[3:0])
        -> Cell U_vec__y_3
        -> Cell U_vec__y_2
        -> Cell U_vec__y_1
        -> Cell U_vec__y_0
    """
    gate_type = inst_list.module
    inst_name = inst.name

    port_bits = [expr_to_nets(ir, p.argname) for p in inst.portlist]
    cells: List[Cell] = []

    line_info = f"line {getattr(inst, 'lineno', '?')}"

    if gate_type in {"buf", "not"}:
        if len(port_bits) != 2:
            raise ValueError(
                f"{gate_type} {inst_name} expects 2 ports: output, input; got {port_bits}"
            )

        y_bits, a_bits = port_bits
        width = max(len(y_bits), len(a_bits))

        y_bits = fit_port_width(
            y_bits,
            width,
            allow_broadcast=False,
            inst_name=inst_name,
            pin_name="Y",
        )
        a_bits = fit_port_width(
            a_bits,
            width,
            allow_broadcast=True,
            inst_name=inst_name,
            pin_name="A",
        )

        for i in range(width):
            out_net = y_bits[i]
            cell_name = expanded_cell_name(inst_name, out_net, width)

            cells.append(
                Cell(
                    name=cell_name,
                    cell_type=gate_type,
                    inputs={"A": a_bits[i]},
                    outputs={"Y": out_net},
                    port_order=["Y", "A"],
                    src=line_info if width == 1 else f"{line_info}, expanded from {inst_name}",
                )
            )

        return cells

    else:
        if len(port_bits) != 3:
            raise ValueError(
                f"{gate_type} {inst_name} expects 3 ports: output, input1, input2; got {port_bits}"
            )

        y_bits, a_bits, b_bits = port_bits
        width = max(len(y_bits), len(a_bits), len(b_bits))

        y_bits = fit_port_width(
            y_bits,
            width,
            allow_broadcast=False,
            inst_name=inst_name,
            pin_name="Y",
        )
        a_bits = fit_port_width(
            a_bits,
            width,
            allow_broadcast=True,
            inst_name=inst_name,
            pin_name="A",
        )
        b_bits = fit_port_width(
            b_bits,
            width,
            allow_broadcast=True,
            inst_name=inst_name,
            pin_name="B",
        )

        for i in range(width):
            out_net = y_bits[i]
            cell_name = expanded_cell_name(inst_name, out_net, width)

            cells.append(
                Cell(
                    name=cell_name,
                    cell_type=gate_type,
                    inputs={"A": a_bits[i], "B": b_bits[i]},
                    outputs={"Y": out_net},
                    port_order=["Y", "A", "B"],
                    src=line_info if width == 1 else f"{line_info}, expanded from {inst_name}",
                )
            )

        return cells


def make_dff_cell(inst) -> Cell:
    """
    Convert a DFF instance.

    Named-port instances preserve the source pin names and order exactly, e.g.:
      dff g0(.RN(rst_n), .SN(1'b1), .CK(clk), .D(d), .Q(q));
    becomes inputs RN/SN/CK/D and output Q.

    Positional instances have no source pin names, so this uses the contest
    conventions for four- and five-port DFFs.
    """
    inst_name = inst.name

    # named-port style
    if any(p.portname is not None for p in inst.portlist):
        inputs = {}
        outputs = {}
        port_order = []

        for p in inst.portlist:
            pname = p.portname
            if pname is None:
                raise ValueError(f"dff {inst_name} mixes named and positional ports")

            net = expr_to_net(p.argname)
            port_order.append(pname)

            if pname.upper() in {"Q", "QN"}:
                outputs[pname] = net
            else:
                inputs[pname] = net

        if not outputs:
            raise ValueError(f"dff {inst_name} has no Q/QN output port: {port_order}")

        return Cell(
            name=inst_name,
            cell_type="dff",
            inputs=inputs,
            outputs=outputs,
            port_order=port_order,
            src=f"line {getattr(inst, 'lineno', '?')}",
        )

    # positional style
    ports = [expr_to_net(p.argname) for p in inst.portlist]

    if len(ports) == 4:
        return Cell(
            name=inst_name,
            cell_type="dff",
            inputs={
                "CLK": ports[0],
                "RST_N": ports[1],
                "D": ports[2],
            },
            outputs={
                "Q": ports[3],
            },
            port_order=["CLK", "RST_N", "D", "Q"],
            src=f"line {getattr(inst, 'lineno', '?')}",
        )

    if len(ports) == 5:
        return Cell(
            name=inst_name,
            cell_type="dff",
            inputs={
                "RN": ports[0],
                "SN": ports[1],
                "CK": ports[2],
                "D": ports[3],
            },
            outputs={
                "Q": ports[4],
            },
            port_order=["RN", "SN", "CK", "D", "Q"],
            src=f"line {getattr(inst, 'lineno', '?')}",
        )

    raise ValueError(
        f"dff {inst_name} expects 4 or 5 positional ports, or named ports; got {ports}"
    )


def handle_instance_list(ir: NetlistIR, inst_list: InstanceList) -> None:
    module_type = inst_list.module

    for inst in inst_list.instances:
        if module_type in PRIMITIVE_GATES:
            cells = make_primitive_cells(ir, inst_list, inst)
            for cell in cells:
                ir.add_cell(cell)

        elif module_type == "dff":
            cell = make_dff_cell(inst)
            ir.add_cell(cell)

        else:
            raise ValueError(f"unsupported module/cell type in contest netlist: {module_type}")


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//.*", "", text)


def _split_commas(text: str) -> List[str]:
    out: List[str] = []
    start = 0
    depth = 0
    for i, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
        elif ch == "," and depth == 0:
            item = text[start:i].strip()
            if item:
                out.append(item)
            start = i + 1
    item = text[start:].strip()
    if item:
        out.append(item)
    return out


def _parse_width_prefix(text: str) -> Tuple[int, int, int, str]:
    text = text.strip()
    match = re.match(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]\s*(.*)$", text, flags=re.S)
    if not match:
        return 1, 0, 0, text
    msb, lsb = int(match.group(1)), int(match.group(2))
    return abs(msb - lsb) + 1, msb, lsb, match.group(3).strip()


def _parse_const_bits_text(expr: str) -> List[str]:
    value = expr.replace("_", "").strip()
    if "'" not in value:
        if value == "0":
            return ["1'b0"]
        if value == "1":
            return ["1'b1"]
        raise ValueError(f"unsupported unsized constant in gate pin: {expr}")

    width_text, rhs = value.split("'", 1)
    if not width_text:
        raise ValueError(f"constant width is required: {expr}")
    width = int(width_text)
    if rhs and rhs[0].lower() == "s":
        rhs = rhs[1:]
    if not rhs:
        raise ValueError(f"unsupported constant format: {expr}")
    base = rhs[0].lower()
    digits = rhs[1:]
    if any(ch in digits.lower() for ch in "xz?"):
        raise ValueError(f"X/Z constants are not supported in current IR: {expr}")
    if base == "b":
        bitstr = digits.zfill(width)[-width:]
    elif base == "h":
        bitstr = bin(int(digits, 16))[2:].zfill(width)[-width:]
    elif base == "d":
        bitstr = bin(int(digits, 10))[2:].zfill(width)[-width:]
    elif base == "o":
        bitstr = bin(int(digits, 8))[2:].zfill(width)[-width:]
    else:
        raise ValueError(f"unsupported constant base in {expr}")
    return [f"1'b{bit}" for bit in bitstr]


def _expr_to_nets_fast(ir: NetlistIR, expr: str) -> List[str]:
    expr = expr.strip()
    if re.fullmatch(r"\d+(?:'s?[bBdDhHoO][0-9a-fA-F_xXzZ?]+)?", expr):
        return _parse_const_bits_text(expr)

    part = re.fullmatch(r"([A-Za-z_][\w$]*)\s*\[\s*(\d+)\s*:\s*(\d+)\s*\]", expr)
    if part:
        base = part.group(1)
        msb, lsb = int(part.group(2)), int(part.group(3))
        return [f"{base}[{i}]" for i in range_indices(msb, lsb)]

    bit = re.fullmatch(r"([A-Za-z_][\w$]*)\s*\[\s*(\d+)\s*\]", expr)
    if bit:
        return [f"{bit.group(1)}[{int(bit.group(2))}]"]

    ident = re.fullmatch(r"[A-Za-z_][\w$]*", expr)
    if ident:
        return signal_bits(ir, expr)

    raise ValueError(f"unsupported expression in structural fast parser: {expr}")


def _expr_to_net_fast(expr: str) -> str:
    bits = _parse_const_bits_text(expr) if re.fullmatch(r"\d+(?:'s?[bBdDhHoO][0-9a-fA-F_xXzZ?]+)?", expr.strip()) else None
    if bits is not None:
        if len(bits) != 1:
            raise ValueError(f"multi-bit constant {expr} is not scalar")
        return bits[0]
    bit = re.fullmatch(r"([A-Za-z_][\w$]*)\s*\[\s*(\d+)\s*\]", expr.strip())
    if bit:
        return f"{bit.group(1)}[{int(bit.group(2))}]"
    if re.fullmatch(r"[A-Za-z_][\w$]*", expr.strip()):
        return expr.strip()
    raise ValueError(f"unsupported scalar expression in structural fast parser: {expr}")


def _add_structural_decl(ir: NetlistIR, stmt: str, lineno: int) -> None:
    match = re.match(r"(input|output|inout|wire)\b\s*(.*)$", stmt.strip(), flags=re.S)
    if not match:
        raise ValueError(f"unsupported declaration: {stmt[:80]}")
    kind = match.group(1)
    rest = re.sub(r"\b(?:wire|reg|logic)\b", " ", match.group(2)).strip()
    width, msb, lsb, rest = _parse_width_prefix(rest)
    for raw_name in _split_commas(rest):
        name = raw_name.strip()
        name = re.sub(r"\s*=.*$", "", name).strip()
        if not re.fullmatch(r"[A-Za-z_][\w$]*", name):
            raise ValueError(f"unsupported declared name: {raw_name}")
        ir.add_signal(name, kind, width=width, msb=msb, lsb=lsb, lineno=lineno)


def _add_structural_gate(ir: NetlistIR, gate_type: str, inst_name: str, args_text: str, lineno: int) -> None:
    args = _split_commas(args_text)
    port_bits = [_expr_to_nets_fast(ir, arg) for arg in args]
    line_info = f"line {lineno}"

    if gate_type in {"buf", "not"}:
        if len(port_bits) != 2:
            raise ValueError(f"{gate_type} {inst_name} expects 2 ports")
        y_bits, a_bits = port_bits
        width = max(len(y_bits), len(a_bits))
        y_bits = fit_port_width(y_bits, width, allow_broadcast=False, inst_name=inst_name, pin_name="Y")
        a_bits = fit_port_width(a_bits, width, allow_broadcast=True, inst_name=inst_name, pin_name="A")
        for i, out_net in enumerate(y_bits):
            ir.add_cell(
                Cell(
                    name=expanded_cell_name(inst_name, out_net, width),
                    cell_type=gate_type,
                    inputs={"A": a_bits[i]},
                    outputs={"Y": out_net},
                    port_order=["Y", "A"],
                    src=line_info if width == 1 else f"{line_info}, expanded from {inst_name}",
                )
            )
        return

    if len(port_bits) != 3:
        raise ValueError(f"{gate_type} {inst_name} expects 3 ports")
    y_bits, a_bits, b_bits = port_bits
    width = max(len(y_bits), len(a_bits), len(b_bits))
    y_bits = fit_port_width(y_bits, width, allow_broadcast=False, inst_name=inst_name, pin_name="Y")
    a_bits = fit_port_width(a_bits, width, allow_broadcast=True, inst_name=inst_name, pin_name="A")
    b_bits = fit_port_width(b_bits, width, allow_broadcast=True, inst_name=inst_name, pin_name="B")
    for i, out_net in enumerate(y_bits):
        ir.add_cell(
            Cell(
                name=expanded_cell_name(inst_name, out_net, width),
                cell_type=gate_type,
                inputs={"A": a_bits[i], "B": b_bits[i]},
                outputs={"Y": out_net},
                port_order=["Y", "A", "B"],
                src=line_info if width == 1 else f"{line_info}, expanded from {inst_name}",
            )
        )


def _add_structural_dff(ir: NetlistIR, inst_name: str, args_text: str, lineno: int) -> None:
    args = _split_commas(args_text)
    if args and args[0].lstrip().startswith("."):
        inputs = {}
        outputs = {}
        port_order = []
        for arg in args:
            match = re.fullmatch(r"\.([A-Za-z_][\w$]*)\s*\(\s*(.*?)\s*\)", arg.strip(), flags=re.S)
            if not match:
                raise ValueError(f"unsupported named dff port: {arg}")
            pname, expr = match.group(1), match.group(2)
            net = _expr_to_net_fast(expr)
            port_order.append(pname)
            if pname.upper() in {"Q", "QN"}:
                outputs[pname] = net
            else:
                inputs[pname] = net
        if not outputs:
            raise ValueError(f"dff {inst_name} has no Q/QN output port")
        ir.add_cell(Cell(inst_name, "dff", inputs=inputs, outputs=outputs, port_order=port_order, src=f"line {lineno}"))
        return

    ports = [_expr_to_net_fast(arg) for arg in args]
    if len(ports) == 4:
        ir.add_cell(
            Cell(
                inst_name,
                "dff",
                inputs={"CLK": ports[0], "RST_N": ports[1], "D": ports[2]},
                outputs={"Q": ports[3]},
                port_order=["CLK", "RST_N", "D", "Q"],
                src=f"line {lineno}",
            )
        )
        return
    if len(ports) == 5:
        ir.add_cell(
            Cell(
                inst_name,
                "dff",
                inputs={"RN": ports[0], "SN": ports[1], "CK": ports[2], "D": ports[3]},
                outputs={"Q": ports[4]},
                port_order=["RN", "SN", "CK", "D", "Q"],
                src=f"line {lineno}",
            )
        )
        return
    raise ValueError(f"dff {inst_name} expects 4 or 5 positional ports, or named ports")


def parse_structural_verilog_to_ir(path: str, top: Optional[str] = None) -> NetlistIR:
    text = Path(path).read_text(errors="replace")
    if find_compiler_directives(text):
        raise ValueError("compiler directives require full Verilog preprocessing")
    text = _strip_comments(text)
    modules = list(re.finditer(r"\bmodule\s+([A-Za-z_][\w$]*)\s*\((.*?)\)\s*;(.*?)\bendmodule\b", text, flags=re.S))
    if not modules:
        raise ValueError("no module definition found")

    chosen = None
    for match in modules:
        name = match.group(1)
        if name == "dff":
            continue
        if top is None or name == top:
            chosen = match
            break
    if chosen is None:
        raise ValueError(f"cannot find top module: {top}")

    ir = NetlistIR(module_name=chosen.group(1))
    ir.add_constant_nets()
    for name in _split_commas(chosen.group(2)):
        if not re.fullmatch(r"[A-Za-z_][\w$]*", name):
            raise ValueError(f"unsupported port name: {name}")
        if name not in ir.port_order:
            ir.port_order.append(name)

    body = chosen.group(3)
    current_line = text.count("\n", 0, chosen.start(3)) + 1
    for raw_stmt in body.split(";"):
        stmt = raw_stmt.strip()
        lineno = current_line
        current_line += raw_stmt.count("\n")
        if not stmt:
            continue
        head = stmt.split(None, 1)[0]
        if head in {"input", "output", "inout", "wire"}:
            _add_structural_decl(ir, stmt, lineno)
            continue
        match = re.fullmatch(r"([A-Za-z_][\w$]*)\s+([A-Za-z_][\w$]*)\s*\((.*)\)", stmt, flags=re.S)
        if not match:
            raise ValueError(f"unsupported statement in structural fast parser: {stmt[:100]}")
        cell_type, inst_name, args_text = match.group(1), match.group(2), match.group(3)
        if cell_type not in SUPPORTED_STRUCTURAL_CELLS:
            raise ValueError(f"unsupported cell type in structural fast parser: {cell_type}")
        if cell_type == "dff":
            _add_structural_dff(ir, inst_name, args_text, lineno)
        else:
            _add_structural_gate(ir, cell_type, inst_name, args_text, lineno)

    ir.rebuild_indices()
    return ir


def parse_verilog_to_ir(path: str, top: Optional[str] = None) -> NetlistIR:
    if os.environ.get("ICCAD_DISABLE_FAST_VERILOG") != "1":
        try:
            return parse_structural_verilog_to_ir(path, top=top)
        except Exception:
            pass

    ast, _ = parse_verilog_file(path)

    chosen_module = None
    for definition in ast.description.definitions:
        if not isinstance(definition, ModuleDef):
            continue
        if definition.name == "dff":
            continue
        if top is None or definition.name == top:
            chosen_module = definition
            break

    if chosen_module is None:
        raise ValueError(f"cannot find top module: {top}")

    ir = NetlistIR(module_name=chosen_module.name)
    ir.add_constant_nets()

    # ANSI-style port declarations
    if chosen_module.portlist is not None:
        for p in chosen_module.portlist.ports:
            if isinstance(p, Ioport):
                handle_ioport(ir, p)
            else:
                # Non-ANSI port list names appear here; declarations come later.
                name = getattr(p, "name", None)
                if name and name not in ir.port_order:
                    ir.port_order.append(name)

    # Body items
    for item in chosen_module.items:
        if isinstance(item, Decl):
            handle_decl(ir, item)

        elif isinstance(item, InstanceList):
            handle_instance_list(ir, item)

    ir.rebuild_indices()
    return ir
