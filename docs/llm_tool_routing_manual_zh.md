# LLM 工具选择语义路由手册

本文基于 `testcase/testcase/test01` 到 `test40` 的 487 条子题目整理，用于指导 LLM 将自然语言请求映射到唯一工具调用。核心原则是按完整语义判断，不按孤立关键词机械匹配。

## 总原则

- 先判断请求是“分析/报告当前状态”“查询上一步变化量”“执行变换/优化”“验证等价性”还是“文件/用例控制”。
- 同一句里的 `now`、`currently`、`after` 只能作为上下文词，不能单独决定工具。必须看完整谓语：`are now/currently in the design` 是当前状态；`were added/removed/eliminated/merged by ...` 是上一步变化量。
- 保留所有网名、门名、位选和文件名原样，例如 `n0[1]`、`g0`、`1'b1`。
- `target/output/source/input/signal/gate` 只放网表对象名；`to_library` 只放库类型，如 `nand_not`、`nor_not`、`and_not`、`and_or_not`。
- 请求中出现 `Ensure functionality does not change` 通常表示“当前这条请求的主要动作是一个需要保持功能的变换/优化”。在路由层，不应把这类请求改选成 `verification_design_equivalence`，否则会漏掉真正要执行的变换；在执行安全层，可以开启自动 CEC 等价性校验来检查变换前后是否等价。
- 只有当请求本身明确要求 `verify/prove/check/confirm equivalence`，并且主要动作就是验证时，才把这条请求路由到等价性验证工具。
- `Report/List/Find any gates with constant inputs` 是只报告，不修改网表；`Simplify/propagate/replace/remove` 才修改网表。

## 1. 用例与文件控制

### begin_testcase

- 语义：开始一个新 testcase，重置上下文。
- 触发表达：`beginning of a new testcase`、`case name is testXX`。
- 参数：`case_name=testXX`。

### design_load

- 语义：加载 Verilog 设计。
- 触发表达：`load the design from the file ... located in the directory ...`。
- 参数：`file_name`、`directory`。

### design_write

- 语义：写出当前设计。
- 触发表达：`write the current design to the output file ...`。
- 参数：`output_path`。

## 2. 计数类：当前状态、cone 内数量、上一步变化量

### analysis_count_gates

- 语义：统计当前设计/当前网表/重构后网表中的 gate 数量。
- 触发表达：
  - `count all the gates in this design`
  - `report the total count broken down by gate type`
  - `total gate count`
  - `How many <TYPE> gates are currently in the design?`
  - `How many <TYPE> gates are now in the design after ...?`
  - `Report the total <TYPE> gate count after ...`
  - `How many <TYPE> gates are now in the reconstructed netlist?`
- 关键判断：谓语是 `are ... in the design/netlist` 或 `total count`，问当前状态总量。
- 参数：
  - 全量统计：无参数。
  - 只输出某一门型：`answer_style="<gate>_only"`，如 `nand_only`、`nor_only`、`not_only`。
- 易混排除：
  - `were added/eliminated/removed/merged` 不是当前总量，改用 `analysis_last_transform_stats`。
  - `in the cone of X` 不是全设计总量，见 `analysis_cone_gate_type_count` 或 `analysis_fanin_cone_size`。

### analysis_cone_gate_type_count

- 语义：统计某个 fanin cone 内每种 gate 数量，或 cone 内某一门型数量。
- 触发表达：
  - `Report the number of each gate type in the cone of <target>`
  - `How many <TYPE> gates are now in the restructured cone of output <target>?`
- 参数：
  - `target=<signal/output>`
  - 如只问某一门型，`answer_style="<gate>_only"`。
- 易混排除：
  - `How many gates are in the fanin/logic cone of output X?` 问 cone 总 gate 数，不是分类数量，见 `analysis_fanin_cone_size`。
  - `Compute/List the transitive fanin cone` 是列出 cone 内容，见 `analysis_transitive_fanin_cone`。

### analysis_fanin_cone_size

- 语义：统计某输出/信号 fanin cone 中 gate 总数。
- 触发表达：
  - `How many gates are in the fanin cone of primary output <output>?`
  - `How many gates are in the logic cone of output <output>?`
- 参数：`output=<signal/output>`。

### analysis_last_transform_stats

- 语义：读取最近一次匹配变换/优化的统计，不重新执行操作。
- 触发表达：
  - `How many BUF gates were added by the buffer insertion just performed?`
  - `How many <TYPE> gates were added by replacing the XNOR/XOR gates?`
  - `How many <TYPE> gates were eliminated by constant propagation?`
  - `How many dangling gates were removed?`
  - `How many redundant gates were removed?`
  - `How many gates were merged as structural duplicates?`
- 关键判断：谓语是 `were added/inserted/removed/eliminated/merged`，通常带 `by ...`、`just performed` 或依赖上一条操作。
- 参数映射：
  - BUF 插入新增数：`metric="added_buffers"`。
  - 替换 XNOR/XOR 后新增某门型：`metric="added_by_type"`, `gate_type=<TYPE>`, `tool="transformation_replace_gate_library"`。
  - 常量传播消除数：`metric="eliminated_gates"`, `tool="transformation_constant_propagation"`, 可带 `gate_type`。
  - dangling/unused 删除数：`metric="removed_gates"`, `tool="transformation_remove_dangling_logic"`。
  - redundant/structural duplicate 合并/删除数：`metric="merged_gates"`, `tool="optimization_merge_equivalent_or_duplicate_gates"`。
- 易混排除：
  - `are now/currently in the design after ...` 是当前总量，不是上一步新增量。
  - 不要为了回答变化量重新调用对应 transformation。

## 3. 路径与图结构

### analysis_path_exists

- 语义：判断是否存在普通组合路径。
- 触发表达：`Does a combinational path exist from <source> to <target>?`
- 参数：`source`、`target`。
- 易混排除：如果句子含 `avoid/avoiding/does not traverse`，用 `analysis_path_exists_avoiding`。

### analysis_path_exists_avoiding

- 语义：判断从 source 到 target 的路径是否存在，同时避开某节点；也用于判断所有路径是否经过某节点。
- 触发表达：
  - `exists that does not traverse node <avoid>`
  - `exists while avoiding <avoid>`
  - `exists that avoids <avoid>`
  - `Does every path from <source> to <target> pass through <avoid>?`
- 参数：`source`、`target`、`avoid`。
- 关键判断：`every path passes through X` 可转化为“是否存在避开 X 的路径”。

### analysis_enumerate_paths

- 语义：枚举所有组合路径。
- 触发表达：
  - `List every path originating at ... and terminating at ...`
  - `Provide a complete enumeration of paths between ...`
  - `Find all combinational paths from ... to ... and list each path`
- 参数：`source`、`target`、`path_limit=1000000`。

### analysis_zero_length_paths

- 语义：找 PI 到 PO 的零长度直接连线路径。
- 触发表达：`paths of length 0`、`direct wire connections from PI to PO`。

### analysis_cut_or_articulation

- 语义：判断某 wire/signal 是否为任意 PI 到 PO 路径的 cut。
- 触发表达：`wire <signal> is a cut between any primary input and any primary output`。
- 参数：`signal=<wire>`, `scope="pi_to_po"`。
- 易混排除：`Find all articulation points between A and B` 不是 yes/no cut，见下一个工具。

### analysis_articulation_points_between

- 语义：列出两个图节点之间的 articulation points。
- 触发表达：`Find all articulation points in the combinational graph between <source> and <target>`。
- 参数：`source`、`target`。

## 4. Fanin/Fanout/Cone 查询

### analysis_direct_fanout

- 语义：列出某 signal/net 直接驱动的 gate，或报告其直接 fanout。
- 触发表达：
  - `fanout of primary input <signal>; list gates it drives directly`
  - `List all gates currently driven by signal <signal>`
  - `List all gates that now connect to the renamed signal <signal>`
- 参数：`signal=<net/signal>`。
- 易混排除：如果对象是 gate 实例 `g0`，问 `g0 drives` 或 `output of g0`，用 `analysis_gate_successors`。

### analysis_gate_successors

- 语义：列出 gate 实例输出直接驱动的后继 gate，或统计数量。
- 触发表达：
  - `number of gates driven by g0`
  - `immediate successors of gate g0`
  - `every gate connected to the output of g0`
- 参数：`gate=<gate_instance>`；只问数量时可加 `answer_style="count_only"`。

### analysis_transitive_fanin_cone

- 语义：列出某 output/signal 的完整 transitive fanin cone。
- 触发表达：
  - `Compute the transitive fanin cone of output <output>`
  - `Compute the fanin logic cone of output <output> and list all gates that contribute`
- 参数：`output=<signal/output>`。

### analysis_transitive_fanout_cone

- 语义：列出某 input/signal 可达的完整 transitive fanout cone。
- 触发表达：`transitive fanout of primary input <input>`、`List all gates reachable from <input>`。
- 参数：`input=<signal/input>`。

### analysis_shared_fanin_cone

- 语义：找两个 fanin cones 的共享 gate。
- 触发表达：`gates shared between the fanin cones of <left> and <right>`。
- 参数：`left`、`right`。

## 5. 深度、关键路径和 cone 大小

### analysis_max_logic_depth

- 语义：计算组合逻辑最大深度/最长组合路径深度/关键路径深度。
- 触发表达：
  - `maximum logic depth`
  - `longest combinational path depth`
  - `critical path depth`
  - `maximum combinational logic depth in the design now`
  - `maximum combinational depth from any primary input to any primary output`
  - `depth of the cone of <target> now`
- 参数：
  - 全设计：无 `target/source/target` 参数。
  - 某 cone：`target=<output/signal>`。
  - 某 source 到 target：`source=<input>`, `target=<output>`。
- 易混排除：
  - `Which output has the deepest fanin cone?` 是找输出，不是返回最大深度数，见 `analysis_deepest_output`。
  - `Which output has the largest fanin cone?` 是按 cone size，不是 depth，见 `analysis_largest_fanin_cone_output`。
  - `maximum depth on any register-to-register path` 见 `analysis_register_path_depth`。

### analysis_outputs_depth_over

- 语义：统计/列出 depth 超过阈值的输出。
- 触发表达：`How many outputs have a logic depth greater than <N>?`
- 参数：`threshold=N`。
- 易混排除：`For each output with depth greater than N, optimize its cone` 是变换，见 `optimization_reduce_depth`。

### analysis_deepest_output

- 语义：找 fanin logic cone 最深的 output/output bit。
- 触发表达：`Which output bit has the deepest fanin logic cone?`
- 关键判断：`deepest` 表示 depth-based。

### analysis_largest_fanin_cone_output

- 语义：找 fanin cone 最大的输出，按 cone 中 gate 数量/规模。
- 触发表达：`Which output has the largest fanin cone?`
- 关键判断：`largest/biggest` 表示 size-based，不是深度。

### analysis_gate_on_max_depth_path

- 语义：判断某 gate 是否在任意最大深度路径上。
- 触发表达：`whether gate <gate> lies on any maximum-depth path`。

### analysis_pi_to_dff_depth

- 语义：计算任意 primary input 到 DFF D-pin 的最大组合深度。
- 触发表达：`maximum logic depth from any primary input to any DFF D-pin`。

### analysis_register_path_depth

- 语义：计算任意 register-to-register 路径上的最大组合深度。
- 触发表达：`maximum combinational depth on any register-to-register path`。

## 6. Primary IO 与时序结构

### analysis_primary_io_summary

- 语义：统计或列出 primary inputs/outputs 及 bit widths。
- 触发表达：
  - `number of primary inputs and outputs`
  - `How many primary inputs and primary outputs`
  - `list all primary inputs ... with their bit widths`
  - `list all primary outputs ... with their bit widths`
- 参数：按输出要求设置 `answer_style="counts"|"inputs"|"outputs"`。

### analysis_register_paths

- 语义：列出 register-to-register 组合路径。
- 触发表达：`List all register-to-register paths ... through combinational logic`。

### analysis_dffs_by_clock

- 语义：列出由某 clock 驱动的 DFF。
- 触发表达：`List all flip-flops driven by clock <clock>`。
- 参数：`clock=<signal>`。

### analysis_dff_enable_hold_structures

- 语义：分析/统计 DFF D 输入逻辑中的 enable/hold 结构。
- 触发表达：
  - `Report the D input logic of the flip-flops ... enable or hold structures`
  - `How many flip-flops were found to have enable or hold structures`

## 7. Boolean 与形式验证

### analysis_boolean_expression

- 语义：推导输出/信号的布尔表达式。
- 触发表达：
  - `Derive the Boolean equation for output <target>`
  - `Write the logic expression for <target>`
  - `What Boolean function does output <target> compute?`
- 参数：`target=<signal/output>`。

### verification_functional_equivalence

- 语义：验证两个内部信号/网的功能等价。
- 触发表达：
  - `signals A and B are functionally equivalent`
  - `A and B produce identical logic values for all inputs`
  - `functional equivalence between internal signals A and B`
- 参数：`left`、`right`。
- 易混排除：这是两个信号之间的等价，不是整个 design 与 original/pre-transform 的等价。

### verification_design_equivalence

- 语义：验证当前设计与 original 或 pre-transform netlist 等价。
- 触发表达：
  - `equivalent to the pre-transformation netlist` -> `reference="pre_transform"`
  - `current design and the original loaded netlist` -> `reference="original"`
  - `netlist as last loaded from disk` -> `reference="original"`
  - `still functionally equivalent to the original` -> `reference="original"`

### analysis_signal_constant

- 语义：判断某输出/信号是否恒为 0 或 1。
- 触发表达：`Is output <signal> always 0/1 regardless of all inputs?`
- 参数：`signal=<signal>`, `value="1'b0"` 或 `"1'b1"`。

### analysis_signal_dependency

- 语义：判断 output 是否依赖某 input。
- 触发表达：`Does output <output> depend on input <input>?`
- 参数：`output`、`input`。

### analysis_signal_symmetry

- 语义：判断某函数对两个输入是否对称。
- 触发表达：`function at <target> is symmetric with respect to inputs <a> and <b>`。
- 参数：`target`、`input_a`、`input_b`。

### analysis_find_nand_equivalent

- 语义：判断网表中是否已有内部信号对 `(a,b)` 使 `NAND(a,b)` 等价于目标。
- 触发表达：`pair of internal signals (a, b) already in the netlist such that NAND(a, b) is equivalent to <target>`。
- 参数：`target=<signal/output>`。

## 8. 基础信息与列表

### analysis_gate_info

- 语义：报告某 gate 实例类型和 pin 连接。
- 触发表达：`What type of gate is <gate>? Report its gate type and pin connections.`
- 参数：`gate=<gate_instance>`。

### analysis_list_gates_by_type

- 语义：列出当前设计中某一门型的所有 gate。
- 触发表达：
  - `List all NAND gates in this design with their input and output signals`
  - `List all XOR gates in this design`
- 参数：`gate_type=<gate_type>`。
- 易混排除：`Report any <gate> gates with constant inputs` 需要按常量输入筛选，不是简单门型列表，见 `transformation_constant_propagation` 且 `report_only=true`。

### analysis_floating_or_unconnected

- 语义：检查/统计 floating inputs、unconnected output ports、floating signals。
- 触发表达：
  - `floating inputs or unconnected output ports`
  - `How many floating signals were found?`
- 易混排除：`found` 是状态查询，不是上一步变化量。

## 9. 变换：fanout、buffer、rename、dangling、constant、library

### transformation_limit_fanout

- 语义：插入 buffer 以限制 fanout，每个 driver 不超过阈值。
- 触发表达：
  - `Insert buffers wherever needed so that no gate drives more than <N> loads`
  - `fanout optimization across the netlist with maximum fanout <N>`
  - `insert buffers on the clock/reset signal <signal> to reduce its fanout`
- 参数：
  - 全设计：`max_fanout=N`。
  - 特定信号：`signal=<signal>`, `max_fanout=N`。
- 易混排除：`insert a BUF gate on signal X so each load is driven through a dedicated buffer` 是 dedicated buffer，见下一个工具。

### transformation_insert_dedicated_buffers

- 语义：对某 signal 每个 load 插入一个专用 BUF。
- 触发表达：`insert a BUF gate on signal <signal> so that each load ... dedicated buffer`。
- 参数：`signal=<signal>`。

### transformation_remove_dangling_logic

- 语义：删除不影响 primary outputs/顺序边界的 dangling/unused logic。
- 触发表达：
  - `Trim unused wires and gates`
  - `Remove all dangling gates that do not contribute to any primary output`
  - `Eliminate unused logic gates`
  - `Sweep out dangling gates`
  - `Prune the netlist of unused gates`
  - `Remove floating nodes that do not affect outputs`
  - `Check if there are any dangling gates ... Remove them if found`
- 易混排除：`How many dangling gates were removed?` 是统计上一步结果，用 `analysis_last_transform_stats`。

### transformation_rename_identifier

- 语义：重命名 gate/wire/signal 并更新引用。
- 触发表达：
  - `Rename gate <old> to <new>`
  - `Change the identifier of wire <old> to <new>`
  - `Update the name of signal <old> to <new>`
- 参数：`old_name`、`new_name`、`kind="gate"` 或 `"signal"`。

### transformation_reconnect_gate_input

- 语义：把某 gate 的某 input pin 接到另一信号。
- 触发表达：`reconnect input pin <pin> of gate <gate> to internal signal <signal>`。
- 参数：`gate`、`pin`、`signal`。

### transformation_collapse_back_to_back_inverters

- 语义：把 NOT 后接 NOT 的反相器对折叠为直连。
- 触发表达：`back-to-back inverter pairs`、`NOT followed by NOT`、`collapse them into a wire/direct wire connections`。

### transformation_constant_propagation

- 语义：报告或简化带常量输入的 gate。
- 触发表达与参数：
  - `Report/List any <TYPE> gates with constant inputs` -> `gate_type=<type>`, `report_only=true`。
  - `Report any <TYPE> gates with a constant 0/1 input` -> 加 `constant="1'b0"` 或 `"1'b1"`, `report_only=true`。
  - `List all gates with one or more inputs tied to 1'b1` -> `constant="1'b1"`, `report_only=true`。
  - `Simplify the reported <TYPE> gates by propagating their constant inputs` -> `gate_type=<type>`，不加 `report_only`。
  - `replace all 2-input NAND gates that have one input tied to constant 1 with inverters` -> `gate_type="nand"`, `constant="1'b1"`。
- 易混排除：
  - 只报告 constant-input gates 不应修改网表。
  - NAND constant-1 -> inverter 属于 constant propagation，不是 `transformation_replace_gate_library`。

### transformation_replace_gate_library

- 语义：把整个设计或某 cone 重新映射到指定等价门库，或只替换某门型。
- 触发表达与参数：
  - `entire netlist/design using only AND and NOT gates` -> `scope="design"`, `to_library="and_not"`。
  - `Remap entire design to use only NAND and NOT gates` -> `scope="design"`, `to_library="nand_not"`。
  - `logic cone of/output <target> using only NAND and NOT gates` -> `scope="cone"`, `target=<target>`, `to_library="nand_not"`。
  - `logic cone of/output <target> using only NOR and NOT gates` -> `scope="cone"`, `target=<target>`, `to_library="nor_not"`。
  - `Replace all 2-input OR gates in the cone of <target> ... NAND and NOT` -> `scope="cone"`, `target=<target>`, `from_gate="or"`, `to_library="nand_not"`。
  - `Decompose all XOR gates in the fanin cone of <target> into AND, OR, and NOT` -> `scope="cone"`, `target=<target>`, `from_gate="xor"`, `to_library="and_or_not"`。
  - `Convert/Rewrite/replace every/all XNOR gate ... NOR-only/NOR and NOT` -> `scope="design"`, `from_gate="xnor"`, `to_library="nor_not"`。
  - `Convert/replace every/all XOR gate ... 4-NAND/NAND-only` -> `scope="design"`, `from_gate="xor"`, `to_library="nand_not"`。
- 易混排除：
  - `target` 永远是信号/输出名，不是 `nand_not` 或 `nor_not`。
  - `from_gate` 是原始门型，如 `or/xor/xnor`，不要写 `or2/xor2`。
  - 转换后的当前门数问题用 `analysis_count_gates` 或 `analysis_cone_gate_type_count`，不是重新调用替换工具。

## 10. 优化

### optimization_reduce_depth

- 语义：通过重构降低组合逻辑深度。
- 触发表达：
  - `Reduce the critical path depth through restructuring`
  - `Optimize the logic to minimize maximum path depth`
  - `Perform depth optimization`
  - `Optimize the logic depth of the design`
  - `Try to optimize/restructure <target> to at most <N> levels deep`
  - `targeting depth <N> or less`
  - `For each output with depth greater than <N>, optimize its cone`
- 参数：
  - 全设计优化：无参数或 `scope="design"`。
  - 某 cone/信号：`target=<signal/output>`, `max_depth=N`, `scope="cone"`。
  - 对所有超阈值输出逐个优化：`max_depth=N`, `scope="outputs_over_depth"`。
- 易混排除：`How many outputs have depth greater than N?` 只是查询，用 `analysis_outputs_depth_over`。

### optimization_merge_equivalent_or_duplicate_gates

- 语义：合并等价/重复 gate。
- 触发表达：
  - `functionally equivalent (produce the same function)` -> `mode="functional"`。
  - `same Boolean function on the same inputs (structural duplicates)` -> `mode="structural"`。
  - `redundant gates ... can be removed without changing functionality` -> `mode="structural"`。
- 易混排除：
  - `How many redundant gates were removed?` 或 `How many gates were merged...` 是统计上一步结果，用 `analysis_last_transform_stats`。

## 11. 最容易错的判别对

- 当前总量 vs 上一步变化量：
  - `are now/currently in the design/netlist`、`total gate count after ...` -> `analysis_count_gates`。
  - `were added/removed/eliminated/merged by ...`、`just performed` -> `analysis_last_transform_stats`。
- 全设计门数 vs cone 门数：
  - `in this design/netlist` -> `analysis_count_gates`。
  - `in the cone/restructured cone of X` -> `analysis_cone_gate_type_count` 或 `analysis_fanin_cone_size`。
- report/list constant-input gates vs simplify constant propagation：
  - `Report/List/Find any ... with constant inputs` -> `transformation_constant_propagation(report_only=true)`。
  - `Simplify/propagate/replace ...` -> `transformation_constant_propagation(report_only=false)`。
- depth 数值 vs deepest output vs largest cone：
  - `maximum logic/combinational depth` -> `analysis_max_logic_depth`。
  - `which output has deepest fanin cone` -> `analysis_deepest_output`。
  - `which output has largest fanin cone` -> `analysis_largest_fanin_cone_output`。
- signal fanout vs gate successor：
  - `signal/input nX drives` -> `analysis_direct_fanout`。
  - `gate g0 drives/output of g0` -> `analysis_gate_successors`。
- whole-design equivalence vs signal equivalence：
  - `current design/netlist vs original/pre-transformation` -> `verification_design_equivalence`。
  - `signals A and B equivalent/identical values` -> `verification_functional_equivalence`。
- cut vs articulation list：
  - `wire X is a cut between any PI and any PO` -> `analysis_cut_or_articulation(scope="pi_to_po")`。
  - `Find all articulation points between A and B` -> `analysis_articulation_points_between`。
