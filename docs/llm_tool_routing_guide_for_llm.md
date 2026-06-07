# LLM Routing Guide Actually Injected Into Prompts

This is the English routing guide used by `src/contest_agent.py` in both the first LLM parser prompt and the second LLM reviewer prompt.

Routing guide derived from all 40 testcase prompt groups. Full predicate matters more than single words.

## Critical: Count-vs-Delta

Tools: `analysis_count_gates` vs `analysis_last_transform_stats`.

These two are the most confused pair. Judge by the full predicate and verb tense, never from a single word.

- Current state: `now/currently/is/are` plus present tense -> `analysis_count_gates`.
- Examples: `How many TYPE gates are NOW in the design?`, `How many TYPE gates are currently in the design?`, `total TYPE gate count after the conversion`, `How many TYPE gates are now in the reconstructed netlist?`.
- Time cues: `are now`, `currently`, `is now`, `now in`.
- A reconstructed netlist is still the whole design.
- Last-operation delta: `were` plus past passive plus `by ...` -> `analysis_last_transform_stats`.
- Examples: `How many BUF gates were ADDED by the buffer insertion just performed?`, `How many TYPE gates were ADDED by replacing the X gates?`, `How many TYPE gates were ELIMINATED by constant propagation?`, `How many dangling gates were REMOVED?`, `How many gates were MERGED as structural duplicates?`.
- Time cues: `were added/inserted/removed/eliminated/merged by`, `just performed`, `by the ... just performed`.
- Do not rerun the transformation for last-operation deltas.

Metric mapping:

- Added BUF -> `metric=added_buffers`.
- Added TYPE by replacing XOR/XNOR -> `metric=added_by_type`, plus `gate_type` and `tool=transformation_replace_gate_library`.
- Eliminated by constant propagation -> `metric=eliminated_gates`, plus `tool=transformation_constant_propagation` and `gate_type` when present.
- Dangling removed -> `metric=removed_gates`, plus `tool=transformation_remove_dangling_logic`.
- Redundant/duplicate merged/removed -> `metric=merged_gates`, plus `tool=optimization_merge_equivalent_or_duplicate_gates`.

## Critical: Full Design vs Cone

Tools: `analysis_count_gates` vs `analysis_cone_gate_type_count` vs `analysis_fanin_cone_size`.

- Word `cone`, `fanin cone`, `logic cone`, or `restructured cone` -> scope is a cone, not the whole design.
- `How many TYPE gates are now in the restructured CONE of output X` -> `analysis_cone_gate_type_count(target=X)`.
- `number of each gate type in the CONE of X` -> `analysis_cone_gate_type_count(target=X)`.
- `How many gates are in the fanin/logic CONE of output X` -> `analysis_fanin_cone_size(output=X)`.
- `Compute/list the transitive fanin CONE of output X` -> `analysis_transitive_fanin_cone(output=X)`.
- No word `cone`, and the request talks about `design`, `netlist`, or `reconstructed netlist` -> `analysis_count_gates`.

## File/Control

- `case name is X` -> `begin_testcase`.
- `load the design from file F located in directory D` -> `design_load`.
- `write the current design to output file F` -> `design_write`.

## Critical: Fanout, Signal vs Gate

Tools: `analysis_direct_fanout` vs `analysis_gate_successors`.

Decide by who the driver is.

- Signal/net/input driving gates -> `analysis_direct_fanout(signal=X)`.
- Signal examples: `fanout of primary input X`, `gates driven directly by signal X`, `List every gate that X drives directly`, `gates currently driven by signal X`, `List all gates that now connect to the renamed signal X`.
- Signal-like names: `n0`, `n5`, `n0[1]`, `n1289`, `renamed_sig`.
- Gate instance driving other gates -> `analysis_gate_successors(gate=G)`.
- Gate examples: `gates driven by g0`, `successors of gate g0`, `connected to output of g0`, `Report every gate connected to the output of g0`.
- Gate-like names: `g0`, `g12`.
- Use `answer_style=count_only` only for `number of gates driven by` or `How many` questions.
- `transitive fanout/reachable from input X`, `Determine all gates reachable from X` -> `analysis_transitive_fanout_cone(input=X)`.
- `shared between fanin cones of A and B` -> `analysis_shared_fanin_cone(left=A,right=B)`.
- `maximum fanout of X now` -> `analysis_max_fanout(signal=X)`.
- `Which primary input has the highest fanout` -> `analysis_max_fanout(scope=primary_inputs, answer_style=signals_only)`.

## Paths

- Simple existence: `Does a combinational path exist from A to B?`, with no avoid clause -> `analysis_path_exists(source=A,target=B)`.
- Avoid/bypass: `does not traverse C`, `while avoiding C`, `avoids C`, `does not pass through C` -> `analysis_path_exists_avoiding(source=A,target=B,avoid=C)`.
- `Does every path from A to B pass through C?` -> `analysis_path_exists_avoiding(source=A,target=B,avoid=C)`. The renderer inverts: `exists=false` means YES, all paths pass through C.
- Enumerate paths only when the object being listed is paths: `List every path`, `complete enumeration`, `find all combinational paths ... list each path`, `Provide a complete enumeration of paths` -> `analysis_enumerate_paths(path_limit=1000000)`.
- Do not use `analysis_enumerate_paths` for `List every gate that X drives directly`; use direct fanout.
- Do not use `analysis_enumerate_paths` for `Report every gate connected to the output of g0`; use gate successors.
- `paths of length 0`, `direct PI to PO wire connections` -> `analysis_zero_length_paths`.
- `List all register-to-register paths` -> `analysis_register_paths`.
- `maximum combinational depth on any register-to-register path` -> `analysis_register_path_depth`.

## Cut / Articulation

- `wire X is a cut between any primary input and any primary output` -> `analysis_cut_or_articulation(signal=X, scope=pi_to_po)`.
- `Find all articulation points between A and B` -> `analysis_articulation_points_between(source=A,target=B)`.
- `Does every path from A to B pass through C?` asks about one specific node C, not all articulation points. Use `analysis_path_exists_avoiding(avoid=C)`.

## Critical: Deepest vs Largest

Tools: `analysis_deepest_output` vs `analysis_largest_fanin_cone_output`.

- `DEEPEST fanin cone` -> `analysis_deepest_output`, judged by logic depth / gate levels.
- `LARGEST fanin cone` or `biggest fanin cone` -> `analysis_largest_fanin_cone_output`, judged by size / number of gates.
- `gate G lies on any maximum-depth path` -> `analysis_gate_on_max_depth_path(gate=G)`.

## Depth

- `maximum logic depth`, `maximum combinational depth`, `longest combinational path depth`, `critical path depth` -> `analysis_max_logic_depth`.
- Use `target=X` only for a cone.
- Use `source=A,target=B` for from-to depth.
- Use no arguments for full-design max depth.
- `maximum depth from any PI to any DFF D-pin` -> `analysis_pi_to_dff_depth`.
- `How many outputs have logic depth greater than N` -> `analysis_outputs_depth_over(threshold=N)`.
- `What is the depth of the cone of X now?` -> `analysis_max_logic_depth(target=X)`.

## Primary IO / Registers

- Counts or lists of primary inputs/outputs with bit widths -> `analysis_primary_io_summary(answer_style=counts/inputs/outputs)`.
- `flip-flops driven by clock C` -> `analysis_dffs_by_clock(clock=C)`.
- `D input logic ... enable or hold structures`, or count of such flip-flops -> `analysis_dff_enable_hold_structures`.

## Boolean / Formal

- `Boolean equation/function/logic expression for X`, `Derive the Boolean equation`, `Write the logic expression` -> `analysis_boolean_expression(target=X)`.
- `signals A and B functionally equivalent`, `identical logic values for all inputs`, `Check functional equivalence between internal signals A and B` -> `verification_functional_equivalence(left=A,right=B)`.
- Key distinction: two signals being compared -> `verification_functional_equivalence`; whole designs being compared -> `verification_design_equivalence`.
- `current design/netlist equivalent to original/last loaded/from disk` -> `verification_design_equivalence(reference=original)`.
- `equivalent to pre-transformation netlist`, `Prove that the transformed design is equivalent to the pre-transformation netlist` -> `verification_design_equivalence(reference=pre_transform)`.
- `output X always 0/1`, `Is output X always 0 regardless of all inputs?` -> `analysis_signal_constant(signal=X,value=1'b0 or 1'b1)`.
- Constant values must be Verilog format `1'b0` or `1'b1`, never `0` or `1`.
- `Does output X depend on input Y?` -> `analysis_signal_dependency(output=X,input=Y)`.
- `function at T symmetric with respect to inputs A and B` -> `analysis_signal_symmetry(target=T,input_a=A,input_b=B)`.
- `pair of internal signals (a,b) such that NAND(a,b) is equivalent to T` -> `analysis_find_nand_equivalent(target=T)`.

## Basic Lists / Checks

- `gate type and pin connections of G`, `What type of gate is G? Report its gate type and pin connections` -> `analysis_gate_info(gate=G)`.
- `List all TYPE gates in this design`, `List all TYPE gates with their input and output signals`, `List all XOR/NAND gates in this design` -> `analysis_list_gates_by_type(gate_type=TYPE)`.
- Key distinction: listing by gate type only -> `analysis_list_gates_by_type`; filtering by constant inputs -> `transformation_constant_propagation(report_only=true)`.
- `floating inputs or unconnected output ports`, `floating signals`, `Check if there are any floating inputs or unconnected output ports` -> `analysis_floating_or_unconnected`.

## Critical: Constant Propagation vs List Gates

Tools: `transformation_constant_propagation` vs `analysis_list_gates_by_type`.

- Filter is `constant inputs` (0/1), not just gate type -> `transformation_constant_propagation`.
- `Report any NAND gates with constant inputs (0 or 1)` -> `transformation_constant_propagation(gate_type=nand, report_only=true)`.
- `Report any AND gates with a constant 0 input` -> `transformation_constant_propagation(gate_type=and, constant=1'b0, report_only=true)`.
- `Report any OR gates with a constant 1 input` -> `transformation_constant_propagation(gate_type=or, constant=1'b1, report_only=true)`.
- `List all gates with one or more inputs tied to 1'b1` -> `transformation_constant_propagation(constant=1'b1, report_only=true)`.
- `List all XOR gates in this design` -> `analysis_list_gates_by_type(gate_type=xor)`.
- Report/find/list/identify without simplify/propagate/remove -> `report_only=true`.
- Simplify/propagate/remove reported gates -> `transformation_constant_propagation` with no `report_only`, or `report_only=false`.
- `Replace all 2-input NAND gates that have one input tied to constant 1 with inverters` -> `transformation_constant_propagation(gate_type=nand, constant=1'b1)`.
- This NAND-to-inverter request is constant propagation, not library replacement.
- If the request says `constant 0 input` or `constant 1 input`, the `constant` argument is mandatory: `constant=1'b0` or `constant=1'b1`.
- If it says `constant inputs (0 or 1)` without specifying one value, omit `constant`.

## Library Replacement

- `entire design/netlist using only NAND and NOT`, `Remap the entire design to use only NAND and NOT` -> `transformation_replace_gate_library(scope=design,to_library=nand_not)`.
- `using only NOR and NOT`, `NOR-only` -> `to_library=nor_not`.
- `using only AND and NOT` -> `to_library=and_not`.
- `AND, OR, and NOT` -> `to_library=and_or_not`.
- Cone replacement: `Replace OR gates in the cone of X with NAND and NOT` -> `scope=cone`, `target=X`, `from_gate=or`, `to_library=nand_not`.
- Cone replacement: `Convert the logic cone of X to use only NOR and NOT` -> `scope=cone`, `target=X`, `to_library=nor_not`.
- Cone replacement: `restructure the logic cone of output X using only NAND and NOT` -> `scope=cone`, `target=X`, `to_library=nand_not`.
- `Decompose all XOR gates in the fanin cone of X into AND, OR, and NOT` -> `scope=cone`, `target=X`, `from_gate=xor`, `to_library=and_or_not`.
- `Convert every XNOR gate in this design to an equivalent NOR-only circuit` -> `scope=design`, `from_gate=xnor`, `to_library=nor_not`.
- `Convert every XOR gate to an equivalent 4-NAND circuit`, `replace all XOR gates with equivalent NAND-only` -> `scope=design`, `from_gate=xor`, `to_library=nand_not`.
- Parameter rule: `from_gate` must be a primitive: `or`, `xor`, `xnor`. Never use `or2`, `xor2`, or a target signal.
- Parameter rule: `to_library` must be `nand_not`, `nor_not`, `and_not`, or `and_or_not`. Never put library names in `target`.
- Parameter rule: `target` is only for `scope=cone`, and is the output/signal name.

## Critical: Redundant vs Dangling

Tools: merge vs remove.

- Redundant/duplicate gates: `functionally equivalent`, `produce the same function`, `same Boolean function on the same inputs`, `structural duplicates`, `redundant gates ... removable without changing functionality` -> `optimization_merge_equivalent_or_duplicate_gates`.
- Use `mode=functional` for `functionally equivalent` or `produce the same function`.
- Use `mode=structural` for `same Boolean function on the same inputs` or `structural duplicates`.
- Dangling/unused/disconnected gates: trim/prune/sweep/delete/remove dangling/unused/floating gates/nets/nodes that `do not contribute to outputs` or are `not connected to any primary output` -> `transformation_remove_dangling_logic`.
- Dangling synonyms: `Trim unused wires and gates`, `Eliminate unused logic gates`, `Delete all gates that do not contribute to any primary output`, `Sweep out dangling gates`, `Prune the netlist of unused gates`, `Remove floating nodes that do not affect outputs`, `Remove all dangling gates and nets not connected to any primary output`.

## Fanout Transformations

- `Insert buffers wherever needed so that no gate drives more than N loads`, `fanout optimization across netlist maximum fanout N`, `Perform fanout optimization with maximum fanout N` -> `transformation_limit_fanout(max_fanout=N)`.
- If a clock/reset/signal is named, such as `on the clock signal n0`, include `signal=S`.
- `insert a BUF on signal S so each load is driven through a dedicated buffer` -> `transformation_insert_dedicated_buffers(signal=S)`.

## Other Transformations

- Rename/change/update identifier/name of gate/wire/signal OLD to NEW -> `transformation_rename_identifier(old_name=OLD,new_name=NEW,kind=gate or signal)`.
- `Rename gate g0 to renamed_gate` -> `kind=gate`.
- `Rename wire n74 to renamed_wire` -> `kind=signal`.
- `Change the identifier of gate G to NEW` -> `kind=gate`.
- `Update the name of signal X to NEW throughout the netlist` -> `kind=signal`.
- `Reconnect input pin P of gate G to internal signal S` -> `transformation_reconnect_gate_input(gate=G,pin=P,signal=S)`.
- `back-to-back inverter pairs`, `NOT followed by NOT`, `Find all pairs of back-to-back inverters and collapse them` -> `transformation_collapse_back_to_back_inverters`.

## Depth Optimization

- `Reduce the critical path depth through restructuring`, `Optimize the logic to minimize maximum path depth`, `Perform depth optimization on the combinational logic`, `Optimize the logic depth of the design` -> `optimization_reduce_depth(scope=design)`.
- Named signal with target depth: `Try to restructure n8 with a target depth of 4`, `Try to optimize n15 to at most 4 levels deep`, `Attempt to reduce the depth of the cone of n8 to 4` -> `optimization_reduce_depth(scope=cone,target=X,max_depth=N)`.
- `For each output with depth greater than N, optimize its cone` -> `optimization_reduce_depth(scope=outputs_over_depth,max_depth=N)`.
- Any request to optimize/reduce/restructure/balance logic depth is a transformation, `optimization_reduce_depth`, not an analysis.
- Analysis is only for asking `What is`, `Compute`, or `Determine` the depth value.

## Functional-Preservation Clauses

- `Ensure functionality does not change`, `Make sure nothing changes functionally`, `preserving functional equivalence`, `Ensure the design functionality does not change` are constraints on a transformation/optimization request.
- They do not change the route to a verification tool.
- Choose verification tools, `verification_functional_equivalence` or `verification_design_equivalence`, only when the main requested action is verify/prove/check/confirm the equivalence itself.
