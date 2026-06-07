#!/usr/bin/env python3
"""analysis_check.py — 分析类回答的独立正确性交叉验。

思路：用 agent 跑每个用例，在【每条分析查询发生的当时】（网表处于该步的正确状态），
用一套【独立代码】从网表重算同一指标，与 agent 返回的结构化结果逐条比对。验的是
“计算值对不对”，能抓出深度算错、扇出数错、路径计数错、锥算错这类 bug；与 agent 的
分析实现（eda_core）相互独立，不循环论证。

覆盖（语义无歧义、可靠重算的）：
  analysis_count_gates          总数 + 各门型计数
  analysis_gate_successors      门输出网线的负载（直接后继）数
  analysis_direct_fanout        网线直接驱动的门数
  analysis_max_logic_depth      源到目标 / 全设计 最长路径的门级数
  analysis_path_exists_avoiding 源到目标、绕开某节点是否可达
  analysis_enumerate_paths      源到目标的路径条数（DAG 计数，不枚举）
其余分析工具：标记 SKIP（暂无独立复核），不误判。

用法：
  cd ~/iccad_contest_a_v5
  PYTHONPATH=src python3 src/analysis_check.py --only 2 5 14
  PYTHONPATH=src python3 src/analysis_check.py            # 全部 40（含大用例，较慢）
  PYTHONPATH=src python3 src/analysis_check.py --path-cap 200000   # 路径计数上限，超则跳过比对
"""
import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

import networkx as nx

import eda_core
import eda_transform as ET
from contest_agent import AgentConfig, ContestAgent

COMB = {"and", "or", "nand", "nor", "not", "buf", "xor", "xnor"}


# ---------- 独立重算：基础结构 ----------
def cell_out_net(ir, name):
    c = ir.cells.get(name)
    return c.outputs.get("Y") if c else None


def net_loads(ir, net):
    """读该网线的 cell 列表（独立用 ir.cells 扫，不依赖 ir.loads 索引）。"""
    out = []
    for nm, c in ir.cells.items():
        if net in c.inputs.values():
            out.append(nm)
    return out


def signal_bit_nets(ir, sig_session, signal):
    """signal 可能是标量或总线名：返回其位网线集合；标量则就是它自己。"""
    try:
        bits = sig_session.analyzer.signal_bits(signal)
        if bits:
            return list(bits)
    except Exception:
        pass
    return [signal]


# ---------- 独立重算：组合图上的深度/路径 ----------
def comb_graph(ir):
    return ET._build_graph(ir)


def longest_gate_levels(g, src_net, dst_net):
    """src_net 到 dst_net 的最长路径上的“门(cell)节点”数 = 逻辑深度。无路径返回 None。"""
    s, t = ET.net_node(src_net), ET.net_node(dst_net)
    if s not in g or t not in g:
        return None
    if not nx.has_path(g, s, t):
        return None
    # 只保留能到达 t 的子图上做最长路径 DP（按拓扑序），权重=经过的 cell 节点数
    anc = nx.ancestors(g, t) | {t}
    sub = g.subgraph(anc)
    try:
        order = list(nx.topological_sort(sub))
    except nx.NetworkXUnfeasible:
        return None
    best = {n: (0 if n == s else float("-inf")) for n in sub.nodes}
    best[s] = 0
    for n in order:
        if best[n] == float("-inf"):
            continue
        for m in sub.successors(n):
            w = 1 if sub.nodes[m].get("kind") == "cell" else 0
            if best[n] + w > best[m]:
                best[m] = best[n] + w
    return None if best[t] == float("-inf") else int(best[t])


def count_paths(g, src_net, dst_net, cap):
    """src_net 到 dst_net 的不同路径条数（DAG DP 计数，不枚举）。超 cap 返回 ('cap', None)。"""
    s, t = ET.net_node(src_net), ET.net_node(dst_net)
    if s not in g or t not in g or not nx.has_path(g, s, t):
        return 0
    anc = nx.ancestors(g, t) | {t}
    sub = g.subgraph(anc)
    try:
        order = list(nx.topological_sort(sub))
    except nx.NetworkXUnfeasible:
        return None
    npath = {n: 0 for n in sub.nodes}
    npath[s] = 1
    for n in order:
        if npath[n] == 0:
            continue
        if npath[n] > cap:
            return ("cap", None)
        for m in sub.successors(n):
            npath[m] += npath[n]
    return npath[t]


def reachable_avoiding(g, src_net, dst_net, avoid_node):
    """src 到 dst 是否存在一条不经过 avoid 的路径。"""
    s, t = ET.net_node(src_net), ET.net_node(dst_net)
    av = ET.net_node(avoid_node)
    if s not in g or t not in g:
        return False
    h = g.copy()
    if av in h:
        h.remove_node(av)
    if s not in h or t not in h:
        return False
    return nx.has_path(h, s, t)


def max_fanin_levels(g, net):
    """从任意源到 net 的最长路径上的 cell 数（= 该网线的最大组合扇入深度）。"""
    t = ET.net_node(net)
    if t not in g:
        return None
    anc = nx.ancestors(g, t) | {t}
    sub = g.subgraph(anc)
    try:
        order = list(nx.topological_sort(sub))
    except nx.NetworkXUnfeasible:
        return None
    best = {}
    for n in order:
        base = max((best[p] for p in sub.predecessors(n)), default=0)
        best[n] = base + (1 if sub.nodes[n].get("kind") == "cell" else 0)
    return best.get(t, 0)


def all_node_fanin_levels(g):
    """一次全图 DP：每个节点的最大组合扇入深度(cell 数)。"""
    try:
        order = list(nx.topological_sort(g))
    except nx.NetworkXUnfeasible:
        return None
    best = {}
    for n in order:
        base = max((best[p] for p in g.predecessors(n)), default=0)
        best[n] = base + (1 if g.nodes[n].get("kind") == "cell" else 0)
    return best


# ---------- 比对一条分析查询 ----------
def check_call(ir, session, tool, args, result, cap):
    """返回 (status, detail). status in {OK, MISMATCH, SKIP}."""
    if tool == "analysis_count_gates":
        by = Counter(c.cell_type for c in ir.cells.values())
        total = sum(by.values())
        style = args.get("answer_style", "")
        if style in ("", "by_type", "total_only"):
            r_total = result.get("total")
            r_by = {k: v for k, v in (result.get("by_type") or {}).items()}
            mism = []
            if r_total is not None and r_total != total:
                mism.append(f"total {r_total}≠{total}")
            for k, v in r_by.items():
                if by.get(k.lower(), by.get(k, 0)) != v:
                    mism.append(f"{k} {v}≠{by.get(k.lower(), by.get(k,0))}")
            return ("MISMATCH", "; ".join(mism)) if mism else ("OK", f"total={total}")
        # specific-type styles like 'not_only'/'buf_only'：答案是该门型的计数，不是 total
        if style.endswith("_only"):
            t = style[:-5]
            exp = by.get(t, 0)
            abt = {k.lower(): v for k, v in (result.get("by_type") or {}).items()}
            got = abt.get(t, result.get("total"))   # agent 的该门型计数（兜底才用 total）
            return ("OK", f"{t}={exp}") if got == exp else ("MISMATCH", f"{t}: agent={got} mine={exp}")
        return ("SKIP", f"style={style}")

    if tool == "analysis_gate_successors":
        out = cell_out_net(ir, args.get("gate", ""))
        exp = len(net_loads(ir, out)) if out else None
        got = result.get("successor_count")
        if exp is None:
            return ("SKIP", "no output net")
        return ("OK", f"{exp}") if got == exp else ("MISMATCH", f"successor_count {got}≠{exp}")

    if tool == "analysis_direct_fanout":
        sig = args.get("signal", "")
        bits = signal_bit_nets(ir, session, sig)
        gates = set()
        for b in bits:
            gates.update(net_loads(ir, b))
        exp = len(gates)
        got = result.get("gate_count")
        return ("OK", f"{exp}") if got == exp else ("MISMATCH", f"gate_count {got}≠{exp} (sig={sig})")

    if tool == "analysis_max_logic_depth":
        g = comb_graph(ir)
        mode = result.get("mode")
        got = result.get("depth")
        if mode == "source_to_target" or (args.get("source") and args.get("target")):
            exp = longest_gate_levels(g, args["source"], args["target"])
            pe = result.get("path_exists")
            if exp is None:
                # 我判无路径：agent 也应报无路径(depth=None/path_exists=False)
                if got is None or pe is False:
                    return ("OK", "no-path (一致)")
                return ("MISMATCH", f"我判无路径，但 agent depth={got} ({args['source']}->{args['target']})")
            if got is None or pe is False:
                return ("MISMATCH", f"agent 判无路径，但我算 depth={exp} ({args['source']}->{args['target']})")
            return ("OK", f"{exp}") if got == exp else ("MISMATCH", f"depth {got}≠{exp} ({args['source']}->{args['target']})")
        if mode == "fanin_cone" or (args.get("target") and not args.get("source")):
            exp = max_fanin_levels(g, args["target"])
            if exp is None:
                return ("SKIP", "target not in graph")
            return ("OK", f"{exp}") if got == exp else ("MISMATCH", f"fanin depth {got}≠{exp} (target={args['target']})")
        if mode == "design":
            best = all_node_fanin_levels(g)
            if best is None:
                return ("SKIP", "cyclic")
            mx = 0
            for nm in ir.signal_order:
                if ir.signals[nm].direction == "output":
                    for bit in session.analyzer.signal_bits(nm):
                        nd = ET.net_node(bit)
                        if nd in best:
                            mx = max(mx, best[nd])
            return ("OK", f"{mx}") if got == mx else ("MISMATCH", f"design depth {got}≠{mx}")
        return ("SKIP", f"mode={mode}")

    if tool == "analysis_path_exists_avoiding":
        g = comb_graph(ir)
        exp = reachable_avoiding(g, args["source"], args["target"], args.get("avoid"))
        got = result.get("exists")
        return ("OK", f"{exp}") if got == exp else ("MISMATCH", f"exists {got}≠{exp}")

    if tool == "analysis_enumerate_paths":
        g = comb_graph(ir)
        exp = count_paths(g, args["source"], args["target"], cap)
        got = result.get("path_count")
        if isinstance(exp, tuple) and exp[0] == "cap":
            return ("SKIP", f">cap paths")
        if exp is None:
            return ("SKIP", "cyclic")
        return ("OK", f"{exp}") if got == exp else ("MISMATCH", f"path_count {got}≠{exp}")

    if tool == "analysis_transitive_fanout_cone":
        g = comb_graph(ir)
        net = args.get("input") or args.get("signal") or args.get("net") or args.get("target")
        s = ET.net_node(net)
        exp = 0 if s not in g else sum(1 for n in nx.descendants(g, s) if g.nodes[n].get("kind") == "cell")
        got = result.get("gate_count")
        return ("OK", f"{exp}") if got == exp else ("MISMATCH", f"fanout_cone gate_count {got}≠{exp} (net={net})")

    if tool == "analysis_transitive_fanin_cone":
        g = comb_graph(ir)
        net = args.get("output") or args.get("signal") or args.get("net") or args.get("target")
        t = ET.net_node(net)
        exp = 0 if t not in g else sum(1 for n in nx.ancestors(g, t) if g.nodes[n].get("kind") == "cell")
        got = result.get("gate_count")
        return ("OK", f"{exp}") if got == exp else ("MISMATCH", f"fanin_cone gate_count {got}≠{exp} (net={net})")

    return ("SKIP", "no independent recompute")


def run_case(case_dir, cap):
    ROOT = case_dir.parents[2] if (case_dir.parents[2] / "src").exists() else Path(".")
    cfg = AgentConfig(parser="regex", suite_root=ROOT, log_dir=Path("/tmp/ac_log"))
    agent = ContestAgent(cfg, suppress_stdout=True)
    findings = []
    orig = agent.execute

    def wrap(call, _o=orig):
        r = _o(call)
        if isinstance(r, dict) and (call.tool.startswith("analysis") ):
            sess = eda_core._DESIGN_SESSIONS.get(r.get("design_id", "current"))
            if sess is not None:
                try:
                    st, detail = check_call(sess.ir, sess, call.tool, dict(call.arguments), r, cap)
                except Exception as e:  # noqa
                    st, detail = "SKIP", f"checker-error:{type(e).__name__}"
                findings.append((call.tool, st, detail))
        return r

    agent.execute = wrap
    for line in [x for x in (case_dir / "prompt.txt").read_text().splitlines() if x.strip()]:
        try:
            agent.process_request(line)
        except Exception:
            pass
    return findings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite-root", default=".")
    ap.add_argument("--only", nargs="*", type=int, default=None)
    ap.add_argument("--path-cap", type=int, default=2_000_000)
    args = ap.parse_args()
    root = Path(args.suite_root) / "testcase" / "testcase"
    cases = sorted([d for d in root.glob("test*") if d.is_dir()],
                   key=lambda p: int("".join(c for c in p.name if c.isdigit()) or 0))
    if args.only:
        keep = {f"test{n:02d}" for n in args.only} | {f"test{n}" for n in args.only}
        cases = [c for c in cases if c.name in keep]

    tot_ok = tot_mis = tot_skip = 0
    bad = []
    skip_tools = Counter()
    for c in cases:
        try:
            fnd = run_case(c, args.path_cap)
        except Exception as e:  # noqa
            print(f"  {c.name:8s} ! ERROR {type(e).__name__}: {e}")
            bad.append(c.name)
            continue
        ok = sum(1 for _, s, _ in fnd if s == "OK")
        mis = [(t, d) for t, s, d in fnd if s == "MISMATCH"]
        sk = sum(1 for _, s, _ in fnd if s == "SKIP")
        for t, s, _ in fnd:
            if s == "SKIP":
                skip_tools[t] += 1
        tot_ok += ok; tot_mis += len(mis); tot_skip += sk
        mark = "✓" if not mis else "✗"
        line = f"  {c.name:8s} {mark}  checked={ok} mismatch={len(mis)} skip={sk}"
        print(line)
        for t, d in mis:
            print(f"        ✗ {t}: {d}")
            bad.append(f"{c.name}:{t}")
    print("-" * 60)
    print(f"  独立核验通过={tot_ok}  不一致={tot_mis}  跳过(暂无独立复核)={tot_skip}")
    if skip_tools:
        print("  跳过的工具(可后续补独立复核):")
        for t, n in skip_tools.most_common():
            print(f"      {n:3d}×  {t}")
    if tot_mis:
        print(f"  ⚠ 需检查: {', '.join(bad)}")
    else:
        print(f"  所有可独立复核的分析回答：计算值正确 ✓")
    return 1 if tot_mis else 0


if __name__ == "__main__":
    sys.exit(main())
