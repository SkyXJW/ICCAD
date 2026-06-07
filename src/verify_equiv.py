#!/usr/bin/env python3
"""verify_equiv.py — 用随机仿真核对“变换前后功能不变”（与赛题判等口径一致：
主输入/主输出 + 每个 DFF 的 D 端当输出、Q 端当输入，在寄存器处切开做组合等价）。

对每个用例：加载原始 testNN.v 与 agent 写出的 testNN_out.v，给两边“对应”的 PI 位和
DFF-Q 网线灌同一组随机位、各自仿真组合逻辑，比对所有 PO 位和 DFF-D 端是否逐位相同。
不依赖 abc/yosys 的时序处理，纯组合仿真，稳。多组随机向量(默认 1024 位)≈零漏判概率。

对应规则：
- 叶子(自由量)：PI 按端口位名、DFF 的 Q 网线按网线名 —— 这些名字在所有变换里都稳定。
- 观测点：PO 按端口位名；DFF 的 D/RN/SN 端按【DFF 实例名】比对（合并会改 D 的来源网线名，
  但实例名不变）。被删掉的 DFF（如常量传播/去悬空消掉的死寄存器）只在一侧出现 → 跳过并提示；
  若该删除真的影响了功能，必然在其它存活观测点上暴露出来，故仍 sound。

用法：
  cd ~/iccad_contest_a_v5
  PYTHONPATH=src python3 verify_equiv.py                 # 跑 testcase/testcase 下全部用例
  PYTHONPATH=src python3 verify_equiv.py --only 29 33 38 # 只跑指定编号
  PYTHONPATH=src python3 verify_equiv.py --bits 2048     # 加大随机向量位宽
"""
import argparse
import random
import sys
from pathlib import Path

import networkx as nx

import eda_core
from eda_core import design_load
import eda_transform as ET


def _signal_bits(session, name):
    return session.analyzer.signal_bits(name)


def _leaves(ir):
    """组合图里入度为 0 的网线（PI 位 / DFF-Q 网线 / 常量 / 悬空未驱动网线）。"""
    g = ET._build_graph(ir)
    out = set()
    for node in g.nodes:
        if g.nodes[node].get("kind") == "net" and g.in_degree(node) == 0:
            nm = g.nodes[node]["name"]
            if nm not in ("1'b0", "1'b1"):
                out.add(nm)
    return out


def collect(ir, session):
    """返回 (pi_nets, po_nets, dffs)。dffs: instance -> {D,Q,RN,SN}（值为网线或常量）。"""
    pi_nets, po_nets = set(), set()
    for name in ir.signal_order:
        d = ir.signals[name].direction
        if d == "input":
            pi_nets.update(_signal_bits(session, name))
        elif d == "output":
            po_nets.update(_signal_bits(session, name))
    dffs = {}
    for cname, c in ir.cells.items():
        if c.cell_type == "dff":
            dffs[cname] = {
                "D": c.inputs.get("D"),
                "Q": c.outputs.get("Q"),
                "CK": c.inputs.get("CK"),
                "RN": c.inputs.get("RN"),
                "SN": c.inputs.get("SN"),
            }
    return pi_nets, po_nets, dffs


def simulate(ir, K, MASK, shared, free_cache):
    """组合仿真。shared: 跨两网表共享的叶子值(PI 位 / DFF-Q 网线)。
    其它源网线(意料外的叶子)用本表独立随机值(避免两侧偶然相等导致漏判)。"""
    g = ET._build_graph(ir)
    val = {}

    def leaf(net):
        if net == "1'b0":
            return 0
        if net == "1'b1":
            return MASK
        if net in shared:
            return shared[net]
        # 意料外的叶子：本网表内一致、跨网表独立
        if net not in free_cache:
            free_cache[net] = random.getrandbits(K)
        return free_cache[net]

    def gv(net):
        if net in ("1'b0", "1'b1"):
            return 0 if net == "1'b0" else MASK
        if net not in val:
            val[net] = leaf(net)
        return val[net]

    for node in nx.topological_sort(g):
        kind = g.nodes[node].get("kind")
        if kind == "net":
            nm = g.nodes[node]["name"]
            if nm not in val:
                val[nm] = leaf(nm)
        elif kind == "cell":
            c = ir.cells[g.nodes[node]["name"]]
            t = c.cell_type
            if t == "buf":
                r = gv(c.inputs["A"])
            elif t == "not":
                r = (~gv(c.inputs["A"])) & MASK
            elif t == "and":
                r = gv(c.inputs["A"]) & gv(c.inputs["B"])
            elif t == "or":
                r = gv(c.inputs["A"]) | gv(c.inputs["B"])
            elif t == "nand":
                r = (~(gv(c.inputs["A"]) & gv(c.inputs["B"]))) & MASK
            elif t == "nor":
                r = (~(gv(c.inputs["A"]) | gv(c.inputs["B"]))) & MASK
            elif t == "xor":
                r = gv(c.inputs["A"]) ^ gv(c.inputs["B"])
            elif t == "xnor":
                r = (~(gv(c.inputs["A"]) ^ gv(c.inputs["B"]))) & MASK
            else:
                r = 0
            out = c.outputs.get("Y")
            if out is not None:
                val[out] = r
    return val


def obs_val(val, net, MASK):
    if net is None:
        return None
    if net == "1'b0":
        return 0
    if net == "1'b1":
        return MASK
    return val.get(net)


def check_case(case_dir, K):
    MASK = (1 << K) - 1
    src = case_dir / f"{case_dir.name}.v"
    out = case_dir / f"{case_dir.name}_out.v"
    if not src.exists() or not out.exists():
        return ("SKIP", f"missing {'input' if not src.exists() else 'output'} .v")

    eda_core._DESIGN_SESSIONS.clear()
    design_load(str(src), design_id="a")
    sa = eda_core._DESIGN_SESSIONS["a"]
    ia = sa.ir
    design_load(str(out), design_id="b")
    sb = eda_core._DESIGN_SESSIONS["b"]
    ib = sb.ir

    pa, poa, da = collect(ia, sa)
    pb, pob, db = collect(ib, sb)

    # 端口一致性（PI/PO 数量与名字应一致）
    notes = []
    if pa != pb:
        notes.append(f"PI mismatch (+{len(pb-pa)}/-{len(pa-pb)})")
    if poa != pob:
        notes.append(f"PO mismatch (+{len(pob-poa)}/-{len(poa-pob)})")

    # 共享叶子：两网表里都为叶子(入度0)的网线，灌同一随机值。
    # 这天然涵盖 PI 位、DFF-Q 网线、以及两边都悬空未驱动的网线（如未赋值的 PO 位 n3[0]）。
    shared = {}
    for net in (_leaves(ia) & _leaves(ib)):
        shared[net] = random.getrandbits(K)

    va = simulate(ia, K, MASK, shared, {})
    vb = simulate(ib, K, MASK, shared, {})

    mism = []
    # PO 观测点
    for net in (poa & pob):
        if obs_val(va, net, MASK) != obs_val(vb, net, MASK):
            mism.append(("PO", net))
    # DFF 观测点（按实例名）
    common_dff = set(da) & set(db)
    for inst in common_dff:
        for pin in ("D", "CK", "RN", "SN"):
            na, nb = da[inst][pin], db[inst][pin]
            if na is None and nb is None:
                continue
            if obs_val(va, na, MASK) != obs_val(vb, nb, MASK):
                mism.append((f"DFF.{pin}", inst))
    removed = set(da) - set(db)
    added = set(db) - set(da)
    if removed:
        notes.append(f"{len(removed)} DFF removed")
    if added:
        notes.append(f"{len(added)} DFF added")

    status = "PASS" if not mism else "FAIL"
    detail = ""
    if mism:
        kinds = {}
        for k, _ in mism:
            kinds[k] = kinds.get(k, 0) + 1
        detail = "mismatch: " + ", ".join(f"{k}×{n}" for k, n in sorted(kinds.items()))
        detail += f"  e.g. {mism[:3]}"
    elif notes:
        detail = "; ".join(notes)
    return (status, detail)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite-root", default=".")
    ap.add_argument("--only", nargs="*", type=int, default=None, help="只跑这些编号")
    ap.add_argument("--bits", type=int, default=1024, help="随机向量位宽")
    ap.add_argument("--seed", type=int, default=12345)
    args = ap.parse_args()
    random.seed(args.seed)

    root = Path(args.suite_root) / "testcase" / "testcase"
    if not root.exists():
        root = Path(args.suite_root)
    cases = sorted([d for d in root.glob("test*") if d.is_dir()],
                   key=lambda p: int("".join(ch for ch in p.name if ch.isdigit()) or 0))
    if args.only:
        keep = {f"test{n:02d}" for n in args.only} | {f"test{n}" for n in args.only}
        cases = [c for c in cases if c.name in keep]

    npass = nfail = nskip = 0
    fails = []
    for c in cases:
        try:
            status, detail = check_case(c, args.bits)
        except Exception as e:  # noqa
            status, detail = "ERROR", f"{type(e).__name__}: {e}"
        mark = {"PASS": "✓", "FAIL": "✗ FUNC CHANGED", "SKIP": "–", "ERROR": "! ERROR"}[status]
        line = f"  {c.name:8s} {mark}"
        if detail:
            line += f"   ({detail})"
        print(line)
        if status == "PASS":
            npass += 1
        elif status == "FAIL":
            nfail += 1; fails.append(c.name)
        elif status == "ERROR":
            nfail += 1; fails.append(c.name + "(err)")
        else:
            nskip += 1
    print("-" * 60)
    print(f"  PASS={npass}  FAIL={nfail}  SKIP={nskip}")
    if fails:
        print(f"  ⚠ 需检查: {', '.join(fails)}")
    else:
        print(f"  全部存在输出的用例：功能等价 ✓")
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
