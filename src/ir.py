from dataclasses import dataclass, field
from collections import defaultdict
from typing import Dict, List, Optional, Literal


CellType = Literal[
    "and", "or", "nand", "nor", "not", "buf", "xor", "xnor", "dff"
]

DeclKind = Literal["input", "output", "inout", "wire"]
PortDirection = Literal["input", "output", "inout", "internal"]
SignalNetType = Literal["wire"]
NetKind = Literal["wire", "constant"]


@dataclass(frozen=True)
class PinRef:
    cell: str
    pin: str


@dataclass(frozen=True)
class DeclRecord:
    # One source-level declaration node seen in the AST.
    kind: DeclKind
    width: int = 1
    msb: int = 0
    lsb: int = 0
    lineno: Optional[int] = None


@dataclass
class SignalDecl:
    # A signal has both a port direction and a net type. In Verilog, an input
    # or output port is still a wire unless another net/variable type says otherwise.
    name: str
    direction: PortDirection = "internal"
    net_type: SignalNetType = "wire"
    width: int = 1
    msb: int = 0
    lsb: int = 0
    decls: List[DeclRecord] = field(default_factory=list)

    @property
    def kind(self) -> str:
        # Backward-compatible summary for older debug code. Do not use this for
        # lossless validation; use direction + net_type + decls instead.
        if self.direction in ("input", "output", "inout"):
            return self.direction
        return self.net_type


@dataclass
class Net:
    # scalar graph node
    # examples: a, a[0], n1, y[3], 1'b0
    name: str
    kind: NetKind
    base: str
    bit_index: Optional[int] = None


@dataclass
class Cell:
    name: str
    cell_type: CellType

    # canonical pins
    # gate: A/B -> Y
    # buf/not: A -> Y
    # dff: CLK/RST_N/D -> Q
    inputs: Dict[str, str] = field(default_factory=dict)
    outputs: Dict[str, str] = field(default_factory=dict)

    port_order: List[str] = field(default_factory=list)
    src: Optional[str] = None


@dataclass
class NetlistIR:
    module_name: str

    # source of truth
    signals: Dict[str, SignalDecl] = field(default_factory=dict)
    nets: Dict[str, Net] = field(default_factory=dict)
    cells: Dict[str, Cell] = field(default_factory=dict)

    # stable write-back order
    port_order: List[str] = field(default_factory=list)
    signal_order: List[str] = field(default_factory=list)
    cell_order: List[str] = field(default_factory=list)

    # derived indices, always rebuild after edits
    drivers: Dict[str, List[PinRef]] = field(default_factory=lambda: defaultdict(list))
    loads: Dict[str, List[PinRef]] = field(default_factory=lambda: defaultdict(list))

    def add_constant_nets(self) -> None:
        for c in ["1'b0", "1'b1"]:
            if c not in self.nets:
                self.nets[c] = Net(name=c, kind="constant", base=c)

    def add_signal(
        self,
        name: str,
        kind: DeclKind,
        width: int = 1,
        msb: int = 0,
        lsb: int = 0,
        lineno: Optional[int] = None,
    ) -> None:
        record = DeclRecord(kind=kind, width=width, msb=msb, lsb=lsb, lineno=lineno)
        direction: PortDirection = kind if kind in ("input", "output", "inout") else "internal"

        if name not in self.signals:
            self.signals[name] = SignalDecl(
                name=name,
                direction=direction,
                net_type="wire",
                width=width,
                msb=msb,
                lsb=lsb,
                decls=[record],
            )
            self.signal_order.append(name)
        else:
            sig = self.signals[name]
            sig.decls.append(record)

            if (sig.width, sig.msb, sig.lsb) != (width, msb, lsb):
                raise ValueError(
                    f"inconsistent declaration width for {name}: "
                    f"existing=({sig.width},{sig.msb},{sig.lsb}) "
                    f"new=({width},{msb},{lsb})"
                )

            if direction != "internal":
                if sig.direction != "internal" and sig.direction != direction:
                    raise ValueError(
                        f"conflicting port direction for {name}: "
                        f"existing={sig.direction} new={direction}"
                    )
                sig.direction = direction

        sig = self.signals[name]
        net_kind: NetKind = sig.net_type

        if sig.width == 1:
            if name not in self.nets:
                self.nets[name] = Net(name=name, kind=net_kind, base=name)
        else:
            lo, hi = min(sig.msb, sig.lsb), max(sig.msb, sig.lsb)
            for i in range(lo, hi + 1):
                bit = f"{name}[{i}]"
                if bit not in self.nets:
                    self.nets[bit] = Net(name=bit, kind=net_kind, base=name, bit_index=i)

    def add_cell(self, cell: Cell) -> None:
        if cell.name in self.cells:
            raise ValueError(f"duplicated cell name: {cell.name}")
        self.cells[cell.name] = cell
        self.cell_order.append(cell.name)

    def rebuild_indices(self) -> None:
        self.drivers.clear()
        self.loads.clear()

        for cell in self.cells.values():
            for pin, net in cell.outputs.items():
                self.drivers[net].append(PinRef(cell.name, pin))
            for pin, net in cell.inputs.items():
                self.loads[net].append(PinRef(cell.name, pin))
