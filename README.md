# ICCAD

ICCAD Problem A —— LLM 驱动的门级网表 EDA Agent（v5/v6 integrated）
================================================================

本目录是 `/home/xjw/Merge/iccad_contest_a_v5` 与 `/home/xjw/Merge/iccad_contest_a_v6` 的整合版，目标是保留两个版本的功能：

- 以 v6 的提交/评测包装为基础：保留 `configs/contest.yml`、stdin 单条 prompt 异常护栏、日志目录 fallback。
- 以 v5 的完整功能为主体：保留快速结构化 Verilog 解析、扩展分析工具、LLM review/repair、自动等价验证、last-transform 统计、replay `answers.csv`、稳定 ABC CEC 等。
- 移植 v6 的关键修复：更稳健的 back-to-back inverter collapse，以及 cone depth 已满足时的 no-op early return。

覆盖 test01-test40 的分析 / 变换 / 优化 / 验证四类任务；regex 与 LLM 两套解析，推荐 hybrid 提交。


============================================================
目录结构
============================================================

iccad_contest_a_integrated/
├── src/
│   ├── contest_agent.py     agent 入口：自然语言 -> 工具调用(regex/llm/hybrid)
│   │                        -> 调 EDA 后端 -> 按 #RESPONSE/#END 输出并写 log
│   ├── eda_core.py          EDA 后端核心（分析 + 验证）与工具封装层
│   ├── eda_transform.py     变换/优化后端 + IR->Verilog 序列化器
│   ├── eda_abc.py           ABC 桥接：BLIF 导出、CEC 等价验证、深度优化
│   ├── ir.py                网表 IR 数据结构
│   ├── pyv_extractor.py     Verilog 网表 -> IR 解析；含 v5 快速结构化 parser
│   ├── nx_probe.py          NetworkX 组合图构建（DFF 作时序边界）
│   ├── verify_equiv.py      [自测] 仿真级功能等价检查（独立于 ABC）
│   ├── analysis_check.py    [自测] 分析类回答的独立重算交叉验
│   └── llm_vs_regex.py      [自测] LLM 解析 vs regex 基线 的逐条对照
├── configs/
│   ├── contest.yml                         评测/提交推荐配置（无硬编码路径）
│   ├── deepseek.yml                        DeepSeek（OpenAI 兼容接口）配置模板
│   ├── openai_gpt4o_mini_example.yml       OpenAI gpt-4o-mini 配置模板
│   └── openai_gpt4o_mini_llm_auto_verify.yml  开发用：LLM review + 自动等价验证
├── docs/                    v5 LLM tool routing 文档
├── abc_resources/           ABC 工艺库与 rc 脚本
├── mcp_tools_spec.json      机器可读工具契约（使用 v5 扩展 API surface）
├── TOOLS.md                 工具的人读说明
├── testcase/testcase/testNN/  官方样例（prompt.txt + 网表），用于 replay
└── runs/                    replay 日志 / 自测输出目录（运行时生成）

（src/__pycache__、parsetab.py、runs/* 等为运行自动生成，不是源文件。）


============================================================
运行依赖
============================================================

Python 包：pyverilog、networkx、pyyaml
系统命令：iverilog、yosys、abc/yosys-abc（深度优化与 CEC 等价验证依赖）

Ubuntu / WSL 安装：
  sudo apt update
  sudo apt install -y iverilog yosys
  python3 -m pip install pyverilog networkx pyyaml

说明：verify_equiv.py / analysis_check.py 只用 Python 包（pyverilog + networkx），
不依赖 abc/yosys/iverilog，可在任意环境运行；llm_vs_regex.py 需联网 + API key。


============================================================
环境变量
============================================================

  export ABC_BIN=abc             # 普通 ABC 二进制路径，默认 "abc"
  export ABC_CEC_BIN=yosys-abc   # CEC 优先使用的 ABC，可不设；程序会自动 fallback
  export ABC_TIMEOUT=600         # 单次 abc 调用超时(秒)，默认 120；大用例建议调大

  export ICCAD_DISABLE_FAST_VERILOG=1
                                 # 禁用 v5 快速结构化 Verilog parser，强制走 PyVerilog fallback

  export OPENAI_API_KEY=...      # parser=llm/hybrid 且需要 LLM fallback 时使用


============================================================
推荐运行方式
============================================================

评测/提交推荐使用 v6 保留下来的无硬编码路径配置：

  PYTHONPATH=src python3 src/contest_agent.py -config configs/contest.yml

agent 从 stdin 读取 prompt，输出：

  #RESPONSE N
  ...answer...
  #END N

开发 replay（官方 testcase 存在时）：

  PYTHONPATH=src python3 src/contest_agent.py \
    -config configs/contest.yml \
    --parser regex \
    --suite-root . \
    --replay-suite --start 1 --end 40 \
    --suppress-stdout --log-dir runs/regex

replay 会写：

- `runs/regex/timing.csv`
- `runs/regex/answers.csv`
- 每个 testcase 的 log 文件

开发自动验证配置：

  PYTHONPATH=src python3 src/contest_agent.py \
    -config configs/openai_gpt4o_mini_llm_auto_verify.yml \
    --replay-suite --start 1 --end 40 \
    --suppress-stdout --log-dir runs/auto_verify


============================================================
整合版保留的关键能力
============================================================

- `transformation_constant_propagation(report_only=True)`：用于“report/list/find constant-input gates”类问题，不修改 IR、不触发 auto verify。
- `analysis_last_transform_stats`：回答“刚才的 buffer insertion 添加了多少 BUF”等 last-transform delta 问题。
- `analysis_max_fanout(scope="primary_inputs")`：支持 primary input 范围 fanout 查询。
- `analysis_cut_or_articulation(scope="pi_to_po")` 与 `analysis_articulation_points_between`：保留 v5 的 cut/articulation 扩展。
- `analysis_signal_constant(value=...)`：支持询问信号是否恒为指定 0/1。
- `analysis_find_nand_equivalent`：保留 v5 structural value numbering 语义，不退化为 v6 的 exact NAND output 查找。
- `llm_review` / `auto_verify_transforms`：保留 v5 LLM review/repair 与变换后自动等价验证。
- `collapse_back_to_back_inverters`：采用 v6 修复后的实现，保护 PO 并减少重复全量 rebuild。
- `optimization_reduce_depth(scope="cone", max_depth=...)`：采用 v6 already-optimized early return，避免无意义全设计 ABC 运行。


============================================================
自测命令
============================================================

语法检查：

  python3 -m py_compile src/*.py

import smoke test：

  PYTHONPATH=src python3 - <<'PY'
import contest_agent
import eda_core
import eda_transform
import eda_abc
import pyv_extractor
print("imports ok")
PY

CLI help：

  python3 src/contest_agent.py --help

分析/等价辅助工具：

  PYTHONPATH=src python3 src/verify_equiv.py --help
  PYTHONPATH=src python3 src/analysis_check.py --help

若官方 testcase 与输出齐全，可继续运行：

  PYTHONPATH=src python3 src/verify_equiv.py --bits 1024
  PYTHONPATH=src python3 src/analysis_check.py --path-cap 200000
