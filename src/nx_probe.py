from __future__ import annotations

import networkx as nx

from ir import NetlistIR


COMB_GATES = {"and", "or", "nand", "nor", "not", "buf", "xor", "xnor"}


def net_node(name: str) -> str:
    return f"net:{name}"


def cell_node(name: str) -> str:
    return f"cell:{name}"


def node_to_readable(node: str) -> str:
    if node.startswith("net:"):
        return node[4:]
    if node.startswith("cell:"):
        return node[5:]
    return node


def build_comb_graph(ir: NetlistIR) -> nx.DiGraph:
    """Build a bit-level combinational graph from NetlistIR.

    Nodes are split into:
    - net:<name> for scalar nets/bits
    - cell:<name> for combinational gate instances

    Edges are input net -> gate -> output net. DFF cells are intentionally not
    inserted, so DFFs act as sequential boundaries for combinational analysis.
    """
    graph = nx.DiGraph()

    for net_name in ir.nets:
        graph.add_node(net_node(net_name), kind="net", name=net_name)

    for cell_name in ir.cell_order:
        cell = ir.cells[cell_name]
        if cell.cell_type not in COMB_GATES:
            continue

        cnode = cell_node(cell.name)
        graph.add_node(cnode, kind="cell", name=cell.name, cell_type=cell.cell_type)

        for pin, net in cell.inputs.items():
            nnode = net_node(net)
            if nnode not in graph:
                graph.add_node(nnode, kind="net", name=net)
            graph.add_edge(nnode, cnode, pin=pin)

        for pin, net in cell.outputs.items():
            nnode = net_node(net)
            if nnode not in graph:
                graph.add_node(nnode, kind="net", name=net)
            graph.add_edge(cnode, nnode, pin=pin)

    return graph
