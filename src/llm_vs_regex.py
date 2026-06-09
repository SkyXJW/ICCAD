#!/usr/bin/env python3
"""llm_vs_regex.py — 用 regex 可信基线对照 LLM 解析质量（只比"解析"层，不执行 EDA）。

regex 解析已验证可信（功能 40/40 + 分析 171 条 0 误差）。本脚本对每条 prompt：
  - 用 regex parser 解析 → (tool, args)
  - 用 LLM  parser 解析 → (tool, args)
逐条比对工具与参数（忽略 answer_style / report_last 这类纯渲染提示）。
  AGREE     工具+语义参数一致 → LLM 解析正确
  TOOL_DIFF 工具选错（或选了别的工具）→ 需查/调提示词
  ARG_DIFF  工具对但参数不同（源/目标/门名等）→ 需查
  LLM_ERR   LLM 调用/JSON 失败

只解析、不跑后端，省时省 token；但仍需联网调 LLM，请在有 key 的机器上运行：
  cd ~/iccad_contest_a_v5
  export OPENAI_API_KEY=sk-你的key
  PYTHONPATH=src python3 src/llm_vs_regex.py -config configs/deepseek.yml --only 2 5 14 29 31
  PYTHONPATH=src python3 src/llm_vs_regex.py -config configs/deepseek.yml          # 全部40(调用多)
"""
import argparse
import dataclasses
import sys
from pathlib import Path

from contest_agent import load_agent_config, RequestParser

COSMETIC = {"answer_style", "report_last", "design_id", "path_limit"}  # 纯渲染/恒为默认，不影响行为
LOWER_KEYS = {"gate_type", "from_gate", "to_library"}     # 后端会 .lower()，大小写无关


def norm_logic_value(value):
    text = str(value).strip().lower()
    if text in {"0", "1'b0", "1b0"}:
        return "0"
    if text in {"1", "1'b1", "1b1"}:
        return "1"
    return value


def norm_args(args):
    out = {}
    for k, v in (args or {}).items():
        if k in COSMETIC:
            continue
        if isinstance(v, str):
            v = v.strip().strip('"').strip("'")
        if v in (None, ""):   # 空串/None ≡ 缺省，不计入
            continue
        if k in LOWER_KEYS and isinstance(v, str):
            v = v.lower()
        if k == "value":
            v = norm_logic_value(v)
        if k == "reference" and v == "original":
            continue
        if k == "mode" and v == "structural":
            continue
        if k == "kind" and v == "wire":   # wire 与 signal 等价
            v = "signal"
        out[k] = v
    if out.get("metric") == "added_by_type" and out.get("gate_type") in {"buf", "buffer"}:
        out["metric"] = "added_buffers"
        out.pop("gate_type", None)
    return out


def compare(call_r, call_l):
    if call_r.tool != call_l.tool:
        return "TOOL_DIFF", f"regex={call_r.tool} | llm={call_l.tool}"
    ar, al = norm_args(call_r.arguments), norm_args(call_l.arguments)
    # kind=auto 会自动判定，等价于任意显式 kind → 比对时忽略 kind
    if ar.get("kind") == "auto" or al.get("kind") == "auto":
        ar.pop("kind", None); al.pop("kind", None)
    if ar == al:
        return "AGREE", call_r.tool
    added, dropped, conflict = [], [], []
    for k in set(ar) | set(al):
        rv, lv = ar.get(k), al.get(k)
        if rv == lv:
            continue
        if rv is None:                       # regex 没填、LLM 填了 → 多半是显式默认
            added.append(f"{k}={lv!r}")
        elif lv is None:                     # regex 有、LLM 漏了 → 可能丢信息
            dropped.append(f"{k}={rv!r}")
        else:                                # 两边都填且不同 → 冲突
            conflict.append(f"{k}: regex={rv!r} llm={lv!r}")
    if conflict or dropped:
        parts = []
        if conflict:
            parts.append("冲突 " + "; ".join(conflict))
        if dropped:
            parts.append("LLM漏填 " + "; ".join(dropped))
        if added:
            parts.append("(另LLM多填默认 " + "; ".join(added) + ")")
        return "ARG_DIFF", f"{call_r.tool}: " + " | ".join(parts)
    return "BENIGN", f"{call_r.tool}: LLM多填默认 " + "; ".join(added)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    ap = argparse.ArgumentParser()
    ap.add_argument("-config", "--config", type=Path, required=True)
    ap.add_argument("--suite-root", default=".")
    ap.add_argument("--only", nargs="*", type=int, default=None)
    ap.add_argument("--show-agree", action="store_true", help="也打印一致项")
    args = ap.parse_args()

    base = load_agent_config(args.config)
    base = dataclasses.replace(base, suite_root=Path(args.suite_root))
    cfg_r = dataclasses.replace(base, parser="regex")
    cfg_l = dataclasses.replace(base, parser="llm")
    if not cfg_l.api_key:
        env_name = "ANTHROPIC_API_KEY" if cfg_l.provider == "anthropic" else "OPENAI_API_KEY"
        print(f"✗ 没有检测到 {cfg_l.provider} API key。请先 export {env_name}=...")
        return 2
    pr = RequestParser(cfg_r)
    pl = RequestParser(cfg_l)
    print(f"  provider={base.provider} model={base.model} base_url={base.base_url}")

    root = Path(args.suite_root) / "testcase" / "testcase"
    cases = sorted([d for d in root.glob("test*") if d.is_dir()],
                   key=lambda p: int("".join(c for c in p.name if c.isdigit()) or 0))
    if args.only:
        keep = {f"test{n:02d}" for n in args.only} | {f"test{n}" for n in args.only}
        cases = [c for c in cases if c.name in keep]

    tot = {"AGREE": 0, "BENIGN": 0, "TOOL_DIFF": 0, "ARG_DIFF": 0, "LLM_ERR": 0}
    problems = []
    benign_notes = []
    for c in cases:
        lines = [x for x in (c / "prompt.txt").read_text().splitlines() if x.strip()]
        agree = benign = tdiff = adiff = err = 0
        for i, line in enumerate(lines, 1):
            try:
                call_r = pr.parse(line)
            except Exception as e:  # regex 基线理论上不该失败
                problems.append(f"{c.name} #{i} REGEX_ERR {type(e).__name__}: {line[:60]}")
                continue
            try:
                call_l = pl.parse(line)
            except Exception as e:  # noqa
                err += 1; tot["LLM_ERR"] += 1
                problems.append(f"{c.name} #{i} LLM_ERR {type(e).__name__}: {e} | {line[:60]}")
                continue
            st, detail = compare(call_r, call_l)
            tot[st] += 1
            if st == "AGREE":
                agree += 1
                if args.show_agree:
                    print(f"    {c.name} #{i} AGREE {detail}")
            elif st == "BENIGN":
                benign += 1
                benign_notes.append(f"{c.name} #{i}  {detail}")
            elif st == "TOOL_DIFF":
                tdiff += 1
                problems.append(f"{c.name} #{i} TOOL_DIFF  «{line[:70]}»\n        {detail}")
            else:
                adiff += 1
                problems.append(f"{c.name} #{i} ARG_DIFF  «{line[:70]}»\n        {detail}")
        mark = "✓" if (tdiff + adiff + err) == 0 else "✗"
        print(f"  {c.name:8s} {mark}  agree={agree} benign={benign} tool_diff={tdiff} arg_diff={adiff} llm_err={err}", flush=True)

    print("-" * 64)
    n = sum(tot.values())
    real = tot["TOOL_DIFF"] + tot["ARG_DIFF"] + tot["LLM_ERR"]
    print(f"  共 {n} 条 prompt：一致={tot['AGREE']}  无害默认填充={tot['BENIGN']}  "
          f"| 真差异: 工具不同={tot['TOOL_DIFF']} 参数冲突/漏填={tot['ARG_DIFF']} LLM出错={tot['LLM_ERR']}")
    if real == 0:
        print("  ✓ LLM 解析与可信基线在行为上逐条一致（差异均为无害默认填充）")
    if problems:
        print("\n  需检查（真差异）：")
        for p in problems:
            print("   - " + p)
    if benign_notes and args.show_agree:
        print("\n  无害默认填充明细：")
        for b in benign_notes:
            print("   · " + b)
    return 0


if __name__ == "__main__":
    sys.exit(main())
