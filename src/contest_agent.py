from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import signal
import contextlib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    import yaml
except Exception:  # pragma: no cover - only used when PyYAML is absent
    yaml = None

from eda_core import (
    GATE_COUNT_ORDER,
    analysis_boolean_expression,
    analysis_cone_gate_type_count,
    analysis_count_gates,
    analysis_cut_or_articulation,
    analysis_articulation_points_between,
    analysis_deepest_output,
    analysis_dff_enable_hold_structures,
    analysis_dffs_by_clock,
    analysis_direct_fanout,
    analysis_enumerate_paths,
    analysis_fanin_cone_size,
    analysis_find_nand_equivalent,
    analysis_floating_or_unconnected,
    analysis_gate_info,
    analysis_gate_on_max_depth_path,
    analysis_gate_successors,
    analysis_largest_fanin_cone_output,
    analysis_list_gates_by_type,
    analysis_max_fanout,
    analysis_max_logic_depth,
    analysis_outputs_depth_over,
    analysis_path_exists,
    analysis_path_exists_avoiding,
    analysis_pi_to_dff_depth,
    analysis_primary_io_summary,
    analysis_register_path_depth,
    analysis_register_paths,
    analysis_shared_fanin_cone,
    analysis_signal_constant,
    analysis_signal_dependency,
    analysis_signal_symmetry,
    analysis_transitive_fanin_cone,
    analysis_transitive_fanout_cone,
    analysis_zero_length_paths,
    design_load,
    design_write,
    optimization_merge_equivalent_or_duplicate_gates,
    optimization_reduce_depth,
    transformation_collapse_back_to_back_inverters,
    transformation_constant_propagation,
    transformation_insert_dedicated_buffers,
    transformation_limit_fanout,
    transformation_reconnect_gate_input,
    transformation_remove_dangling_logic,
    transformation_rename_identifier,
    transformation_replace_gate_library,
    verification_design_equivalence,
    verification_functional_equivalence,
)


TOOL_REQUIRED_ARGS: Dict[str, List[str]] = {
    "begin_testcase": ["case_name"],
    "design_load": ["file_name", "directory"],
    "design_write": ["output_path"],
    "analysis_count_gates": [],
    "analysis_last_transform_stats": ["metric"],
    "analysis_fanin_cone_size": ["output"],
    "analysis_transitive_fanin_cone": ["output"],
    "analysis_direct_fanout": ["signal"],
    "analysis_transitive_fanout_cone": ["input"],
    "analysis_path_exists": ["source", "target"],
    "analysis_path_exists_avoiding": ["source", "target", "avoid"],
    "analysis_enumerate_paths": ["source", "target"],
    "analysis_max_logic_depth": [],
    "analysis_gate_successors": ["gate"],
    "verification_functional_equivalence": ["left", "right"],
    "transformation_limit_fanout": [],
    "transformation_insert_dedicated_buffers": ["signal"],
    "transformation_remove_dangling_logic": [],
    "transformation_rename_identifier": ["old_name", "new_name"],
    "transformation_reconnect_gate_input": ["gate", "pin", "signal"],
    "transformation_collapse_back_to_back_inverters": [],
    "transformation_constant_propagation": [],
    "transformation_replace_gate_library": [],
    "optimization_reduce_depth": [],
    "optimization_merge_equivalent_or_duplicate_gates": [],
    "analysis_max_fanout": [],
    "analysis_primary_io_summary": [],
    "analysis_gate_info": ["gate"],
    "analysis_gate_on_max_depth_path": ["gate"],
    "analysis_list_gates_by_type": ["gate_type"],
    "analysis_cone_gate_type_count": ["target"],
    "analysis_shared_fanin_cone": ["left", "right"],
    "analysis_zero_length_paths": [],
    "analysis_register_paths": [],
    "analysis_register_path_depth": [],
    "analysis_pi_to_dff_depth": [],
    "analysis_outputs_depth_over": ["threshold"],
    "analysis_deepest_output": [],
    "analysis_largest_fanin_cone_output": [],
    "analysis_dffs_by_clock": ["clock"],
    "analysis_floating_or_unconnected": [],
    "analysis_cut_or_articulation": ["signal"],
    "analysis_articulation_points_between": ["source", "target"],
    "analysis_boolean_expression": ["target"],
    "analysis_signal_dependency": ["output", "input"],
    "analysis_signal_symmetry": ["target", "input_a", "input_b"],
    "analysis_signal_constant": ["signal"],
    "analysis_find_nand_equivalent": ["target"],
    "analysis_dff_enable_hold_structures": [],
    "verification_design_equivalence": [],
}

ALLOWED_TOOLS = set(TOOL_REQUIRED_ARGS)


@dataclass
class ToolCall:
    tool: str
    arguments: Dict[str, Any]
    source: str


@dataclass
class AgentConfig:
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    parser: str = "hybrid"
    suite_root: Path = Path.cwd()
    log_dir: Path = Path.cwd()
    path_limit: int = 1_000_000
    yosys_timeout: int = 240
    miter_dir: Optional[Path] = None
    temperature: float = 0.0
    max_output_tokens: int = 4096
    answer_artifact_threshold: int = 65_536
    llm_review: bool = False
    auto_verify_transforms: bool = False


def _strip_quotes(value: str) -> str:
    return value.strip().strip('"').strip("'").rstrip(".,?")


def _load_mapping(path: Optional[Path]) -> Dict[str, Any]:
    if path is None:
        return {}
    if not path.exists():
        raise FileNotFoundError(path)
    text = path.read_text(errors="replace")
    if not text.strip():
        return {}
    if path.suffix.lower() == ".json":
        return json.loads(text)
    if yaml is not None:
        loaded = yaml.safe_load(text)
        return loaded or {}

    # Minimal fallback for simple "key: value" config files.
    result: Dict[str, Any] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = _strip_quotes(value.strip())
    return result


def _first_value(data: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return default


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def load_agent_config(config_path: Optional[Path]) -> AgentConfig:
    raw = _load_mapping(config_path)
    llm = raw.get("llm") if isinstance(raw.get("llm"), dict) else {}
    merged = {**raw, **llm}

    provider = str(_first_value(merged, "provider", "llm_provider", default="openai")).strip().lower()
    generation = merged.get("generation") if isinstance(merged.get("generation"), dict) else {}
    provider_block = merged.get(provider) if isinstance(merged.get(provider), dict) else {}

    default_model = "gpt-4o-mini" if provider == "openai" else "claude-haiku-4-5"
    model_value = _first_value(provider_block, "model", "model_name", default=None)
    if model_value is None:
        model_value = _first_value(merged, "model", "model_name", default=default_model)
    model = str(model_value)

    api_key = _first_value(provider_block, "api_key", default=None)
    if api_key is None:
        provider_api_key = "anthropic_api_key" if provider == "anthropic" else "openai_api_key"
        api_key = _first_value(merged, provider_api_key, default=None)
    if api_key is None:
        api_key = _first_value(merged, "api_key", default=None)
    if api_key is None:
        env_key = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
        api_key = os.environ.get(env_key)

    base_url = _first_value(provider_block, "base_url", "api_base", default=None)
    if base_url is None:
        base_url = _first_value(merged, "base_url", "api_base", default=None)

    temperature = float(
        _first_value(
            generation,
            "temperature",
            default=_first_value(merged, "temperature", default=0.0),
        )
    )
    max_output_tokens = int(
        _first_value(
            generation,
            "max_output_tokens",
            default=_first_value(merged, "max_output_tokens", "max_tokens", default=4096),
        )
    )
    answer_artifact_threshold = int(_first_value(merged, "answer_artifact_threshold", default=65_536))

    suite_root = Path(_first_value(merged, "suite_root", "project_root", default=Path.cwd()))
    log_dir = Path(_first_value(merged, "log_dir", "output_dir", default=Path.cwd()))
    miter_dir_value = _first_value(merged, "miter_dir", default=None)

    return AgentConfig(
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        parser=str(_first_value(merged, "parser", default="hybrid")).lower(),
        suite_root=suite_root,
        log_dir=log_dir,
        path_limit=int(_first_value(merged, "path_limit", default=1_000_000)),
        yosys_timeout=int(_first_value(merged, "yosys_timeout", default=240)),
        miter_dir=Path(miter_dir_value) if miter_dir_value else None,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        answer_artifact_threshold=answer_artifact_threshold,
        llm_review=_as_bool(_first_value(merged, "llm_review", "llm_self_check", "llm_judge", default=False)),
        auto_verify_transforms=_as_bool(_first_value(merged, "auto_verify_transforms", default=False)),
    )


def _json_post(url: str, headers: Dict[str, str], body: Dict[str, Any], timeout: int = 60) -> Dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    last_error: Optional[BaseException] = None
    for attempt in range(6):
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(f"LLM HTTP error {exc.code}: {detail}")
            if exc.code != 429 or attempt == 5:
                raise last_error from exc
        except urllib.error.URLError as exc:
            last_error = exc
            if attempt == 5:
                raise
        time.sleep(min(30.0, 2.0 * (2 ** attempt)))
    raise RuntimeError(f"LLM request failed after retries: {last_error}")


def _extract_json_object(text: str) -> Dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def _resource_root() -> Path:
    """Return the project/resource root in source and PyInstaller-frozen runs."""
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root)
    return Path(__file__).resolve().parents[1]


def _default_tool_spec_path(suite_root: Path) -> Path:
    candidate = suite_root / "mcp_tools_spec.json"
    if candidate.exists():
        return candidate
    return _resource_root() / "mcp_tools_spec.json"


def load_tool_contract(suite_root: Path) -> Dict[str, Any]:
    path = _default_tool_spec_path(suite_root)
    if not path.exists():
        raise FileNotFoundError(f"cannot find tool contract: {path}")
    return json.loads(path.read_text(errors="replace"))


_TOOL_DESC_OVERRIDES = {
    "analysis_register_paths": "COUNT register-to-register combinational paths (from a DFF output to a DFF data input). Returns a path COUNT, never a depth — do not use for depth questions.",
    "analysis_cone_gate_type_count": "Report the per-gate-type count within the fanin cone of `target` (an output/signal); the renderer outputs only the requested gate type's count. Use for 'how many <TYPE> gates are in the cone of X'.",
    "analysis_pi_to_dff_depth": "Compute the maximum combinational depth from primary inputs to DFF data (D) inputs.",
    "analysis_outputs_depth_over": "List the outputs whose combinational logic depth exceeds the integer `threshold`.",
    "analysis_largest_fanin_cone_output": "Find the output whose fanin cone contains the most gates; report that output and its cone size.",
    "analysis_find_nand_equivalent": "Find the NAND gate(s) whose output net is `target`; report how many and their inputs.",
    "analysis_shared_fanin_cone": "List the gates shared by the fanin cones of two signals `left` and `right`.",
    "analysis_zero_length_paths": "Count zero-length combinational paths (a primary input wired directly to a primary output with no gate in between).",
}


_LLM_ROUTING_GUIDE = """
Routing guide derived from all 40 testcase prompt groups. Full predicate matters more than single words.

=== CRITICAL: Count-vs-Delta (analysis_count_gates vs analysis_last_transform_stats) ===
These two are the most confused pair. Judge by the FULL predicate and verb tense, never from a single word.
- CURRENT STATE (now/currently/is/are + present tense) -> analysis_count_gates: "How many TYPE gates are NOW in the design?", "How many TYPE gates are currently in the design?", "total TYPE gate count after the conversion", "How many TYPE gates are now in the reconstructed netlist?". Time cues: "are now", "currently", "is now", "now in". A reconstructed netlist is still the whole design.
- LAST-OP DELTA (were + past passive + by ...) -> analysis_last_transform_stats: "How many BUF gates were ADDED by the buffer insertion just performed?", "How many TYPE gates were ADDED by replacing the X gates?", "How many TYPE gates were ELIMINATED by constant propagation?", "How many dangling gates were REMOVED?", "How many gates were MERGED as structural duplicates?". Time cues: "were added/inserted/removed/eliminated/merged by", "just performed", "by the ... just performed". Do NOT rerun the transformation.
- Map metric: added BUF -> metric=added_buffers; added TYPE by replacing XOR/XNOR -> metric=added_by_type plus gate_type and tool=transformation_replace_gate_library; eliminated by constant propagation -> metric=eliminated_gates plus tool=transformation_constant_propagation and gate_type when present; dangling removed -> metric=removed_gates plus tool=transformation_remove_dangling_logic; redundant/duplicate merged/removed -> metric=merged_gates plus tool=optimization_merge_equivalent_or_duplicate_gates.

=== CRITICAL: Full-design vs Cone (analysis_count_gates vs analysis_cone_gate_type_count vs analysis_fanin_cone_size) ===
- WORD "cone" (or "fanin cone", "logic cone", "restructured cone") -> scope is a cone, NOT the whole design.
- "How many TYPE gates are now in the restructured CONE of output X" -> analysis_cone_gate_type_count(target=X). "number of each gate type in the CONE of X" -> analysis_cone_gate_type_count(target=X).
- "How many gates are in the fanin/logic CONE of output X" -> analysis_fanin_cone_size(output=X) (size only, no type breakdown).
- "Compute/list the transitive fanin CONE of output X" -> analysis_transitive_fanin_cone(output=X) (list gates, not just count).
- NO word "cone", talks about "design/netlist/reconstructed netlist" -> analysis_count_gates (full design).

=== File/Control ===
"case name is X" -> begin_testcase. "load the design from file F located in directory D" -> design_load. "write the current design to output file F" -> design_write.

=== CRITICAL: Fanout — Signal vs Gate (analysis_direct_fanout vs analysis_gate_successors) ===
Decide by WHO is the driver:
- Signal/net/input driving gates: "fanout of primary input X", "gates driven directly by signal X", "List every gate that X drives directly", "gates currently driven by signal X", "List all gates that now connect to the renamed signal X" -> analysis_direct_fanout(signal=X). The driver name looks like n0, n5, n0[1], n1289, renamed_sig — these are signals/nets/inputs.
- Gate instance driving other gates: "gates driven by g0", "successors of gate g0", "connected to output of g0", "Report every gate connected to the output of g0" -> analysis_gate_successors(gate=g0). The driver name looks like g0, g12 — these are gate instances. Use answer_style=count_only for "number of gates driven by"/"How many" questions.
- "transitive fanout/reachable from input X", "Determine all gates reachable from X" -> analysis_transitive_fanout_cone(input=X).
- "shared between fanin cones of A and B" -> analysis_shared_fanin_cone(left=A,right=B).
- "maximum fanout of X now" -> analysis_max_fanout(signal=X). "Which primary input has the highest fanout" -> analysis_max_fanout(scope='primary_inputs', answer_style='signals_only').

=== Paths ===
- Simple existence: "Does a combinational path exist from A to B?" (no avoid clause) -> analysis_path_exists(source=A,target=B).
- Avoid/bypass: "does not traverse C"/"while avoiding C"/"avoids C"/"does not pass through C" -> analysis_path_exists_avoiding(source=A,target=B,avoid=C).
- "Does every path from A to B pass through C?" -> analysis_path_exists_avoiding(source=A,target=B,avoid=C). The renderer inverts: exists=false means YES (all paths pass through C).
- ENUMERATE PATHS only when the object being listed IS paths: "List every path", "complete enumeration", "find all combinational paths ... list each path", "Provide a complete enumeration of paths". Do NOT use for "List every gate that X drives directly" (that's direct_fanout) or "Report every gate connected to the output of g0" (that's gate_successors).
- "paths of length 0", "direct PI to PO wire connections" -> analysis_zero_length_paths.
- "List all register-to-register paths" -> analysis_register_paths (list paths). "maximum combinational depth on any register-to-register path" -> analysis_register_path_depth (depth value, not a list).

=== Cut / Articulation ===
- "wire X is a cut between any primary input and any primary output" -> analysis_cut_or_articulation(signal=X,scope=pi_to_po).
- "Find all articulation points between A and B" (finding ALL articulation points between two named nodes) -> analysis_articulation_points_between(source=A,target=B).
- IMPORTANT contrast: "Does every path from A to B pass through C?" -> analysis_path_exists_avoiding(avoid=C) — this asks about ONE specific node C, NOT all articulation points.

=== CRITICAL: Deepest vs Largest (analysis_deepest_output vs analysis_largest_fanin_cone_output) ===
- "DEEPEST fanin cone" -> analysis_deepest_output (judged by logic DEPTH / gate levels).
- "LARGEST fanin cone" / "biggest fanin cone" -> analysis_largest_fanin_cone_output (judged by SIZE / number of gates).
- "gate G lies on any maximum-depth path" -> analysis_gate_on_max_depth_path(gate=G).

=== Depth (analysis_max_logic_depth and variants) ===
- "maximum logic depth", "maximum combinational depth", "longest combinational path depth", "critical path depth" -> analysis_max_logic_depth. With target=X only for a cone; with source=A,target=B for from-to depth; no arguments for full-design max depth.
- "maximum depth from any PI to any DFF D-pin" -> analysis_pi_to_dff_depth.
- "How many outputs have logic depth greater than N" -> analysis_outputs_depth_over(threshold=N).
- "What is the depth of the cone of X now?" -> analysis_max_logic_depth(target=X).

=== Primary IO / Registers ===
- Counts or lists of primary inputs/outputs with bit widths -> analysis_primary_io_summary(answer_style counts/inputs/outputs).
- "flip-flops driven by clock C" -> analysis_dffs_by_clock(clock=C).
- "D input logic ... enable or hold structures" or count of such flip-flops -> analysis_dff_enable_hold_structures.

=== Boolean / Formal ===
- "Boolean equation/function/logic expression for X", "Derive the Boolean equation", "Write the logic expression" -> analysis_boolean_expression(target=X).
- "signals A and B functionally equivalent", "identical logic values for all inputs", "Check functional equivalence between internal signals A and B" -> verification_functional_equivalence(left=A,right=B). KEY: two SIGNALS being compared, not designs.
- "current design/netlist equivalent to original/last loaded/from disk" -> verification_design_equivalence(reference=original). "equivalent to pre-transformation netlist" -> reference=pre_transform. "Prove that the transformed design is equivalent to the pre-transformation netlist" -> verification_design_equivalence. KEY: whole DESIGN being compared.
- "output X always 0/1", "Is output X always 0 regardless of all inputs?" -> analysis_signal_constant(signal=X,value=1'b0 or 1'b1). Constant must be Verilog format "1'b0"/"1'b1", never "0"/"1".
- "Does output X depend on input Y?" -> analysis_signal_dependency(output=X,input=Y).
- "function at T symmetric with respect to inputs A and B" -> analysis_signal_symmetry(target=T,input_a=A,input_b=B).
- "pair of internal signals (a,b) such that NAND(a,b) is equivalent to T" -> analysis_find_nand_equivalent(target=T).

=== Basic Lists / Checks ===
- "gate type and pin connections of G", "What type of gate is G? Report its gate type and pin connections" -> analysis_gate_info(gate=G).
- "List all TYPE gates in this design", "List all TYPE gates with their input and output signals", "List all XOR/NAND gates in this design" -> analysis_list_gates_by_type(gate_type=TYPE). KEY: filtering by gate TYPE only, NOT by constant inputs.
- "floating inputs or unconnected output ports", "floating signals", "Check if there are any floating inputs or unconnected output ports" -> analysis_floating_or_unconnected.

=== CRITICAL: Constant Propagation vs List Gates (transformation_constant_propagation vs analysis_list_gates_by_type) ===
- Filter is "constant inputs" (0/1) NOT gate type -> transformation_constant_propagation.
- "Report any NAND gates with constant inputs (0 or 1)" -> transformation_constant_propagation(gate_type=nand, report_only=true).
- "Report any AND gates with a constant 0 input" -> transformation_constant_propagation(gate_type=and, constant="1'b0", report_only=true).
- "Report any OR gates with a constant 1 input" -> transformation_constant_propagation(gate_type=or, constant="1'b1", report_only=true).
- "List all gates with one or more inputs tied to 1'b1" -> transformation_constant_propagation(constant="1'b1", report_only=true).
- "List all XOR gates in this design" -> analysis_list_gates_by_type(gate_type=xor). KEY: just listing by type, NO constant-input filter.
- REPORT/FIND/LIST/IDENTIFY without simplify/propagate/remove -> report_only=true. SIMPLIFY/PROPAGATE/REMOVE reported gates -> transformation_constant_propagation (no report_only or report_only=false).
- "Replace all 2-input NAND gates that have one input tied to constant 1 with inverters" -> transformation_constant_propagation(gate_type=nand, constant="1'b1"). This is constant propagation, NOT library replacement.
- If the request says "constant 0 input" or "constant 1 input", the constant argument is mandatory: constant="1'b0" or constant="1'b1". If it says "constant inputs (0 or 1)" without specifying, omit constant.

=== Library Replacement (transformation_replace_gate_library) ===
- "entire design/netlist using only NAND and NOT", "Remap the entire design to use only NAND and NOT" -> scope=design, to_library=nand_not.
- "using only NOR and NOT"/"NOR-only" -> to_library=nor_not. "using only AND and NOT" -> to_library=and_not. "AND, OR, and NOT" -> to_library=and_or_not.
- For CONE replacement: "Replace OR gates in the cone of X with NAND and NOT" -> scope=cone, target=X, from_gate=or, to_library=nand_not. "Convert the logic cone of X to use only NOR and NOT" -> scope=cone, target=X, to_library=nor_not. "restructure the logic cone of output X using only NAND and NOT" -> scope=cone, target=X, to_library=nand_not.
- "Decompose all XOR gates in the fanin cone of X into AND, OR, and NOT" -> scope=cone, target=X, from_gate=xor, to_library=and_or_not.
- "Convert every XNOR gate in this design to an equivalent NOR-only circuit" -> scope=design, from_gate=xnor, to_library=nor_not.
- "Convert every XOR gate to an equivalent 4-NAND circuit", "replace all XOR gates with equivalent NAND-only" -> scope=design, from_gate=xor, to_library=nand_not.
- PARAMETER RULES: from_gate must be primitive: or/xor/xnor (never or2/xor2, never a target signal). to_library must be nand_not/nor_not/and_not/and_or_not (never put in target). target is ONLY for scope=cone and is the output signal name.

=== CRITICAL: Redundant (merge) vs Dangling (remove) ===
- REDUNDANT/DUPLICATE gates: "functionally equivalent", "produce the same function", "same Boolean function on the same inputs", "structural duplicates", "redundant gates ... removable without changing functionality" -> optimization_merge_equivalent_or_duplicate_gates. mode='functional' for "functionally equivalent/produce the same function"; mode='structural' for "same Boolean function on the same inputs/structural duplicates".
- DANGLING/UNUSED/DISCONNECTED gates: trim/prune/sweep/delete/remove dangling/unused/floating gates/nets/nodes that "do not contribute to outputs"/"not connected to any primary output" -> transformation_remove_dangling_logic. Synonyms: "Trim unused wires and gates", "Eliminate unused logic gates", "Delete all gates that do not contribute to any primary output", "Sweep out dangling gates", "Prune the netlist of unused gates", "Remove floating nodes that do not affect outputs", "Remove all dangling gates and nets not connected to any primary output".

=== Fanout Transformations ===
- "Insert buffers wherever needed so that no gate drives more than N loads", "fanout optimization across netlist maximum fanout N", "Perform fanout optimization with maximum fanout N" -> transformation_limit_fanout(max_fanout=N). If a clock/reset/signal is named (e.g. "on the clock signal n0"), include signal=S.
- "insert a BUF on signal S so each load is driven through a dedicated buffer" -> transformation_insert_dedicated_buffers(signal=S).

=== Other Transformations ===
- Rename/change/update identifier/name of gate/wire/signal OLD to NEW -> transformation_rename_identifier(old_name=OLD,new_name=NEW,kind=(gate or signal)). "Rename gate g0 to renamed_gate" -> kind=gate. "Rename wire n74 to renamed_wire" -> kind=signal. "Change the identifier of gate G to NEW" -> kind=gate. "Update the name of signal X to NEW throughout the netlist" -> kind=signal.
- "Reconnect input pin P of gate G to internal signal S" -> transformation_reconnect_gate_input(gate=G,pin=P,signal=S).
- "back-to-back inverter pairs", "NOT followed by NOT", "Find all pairs of back-to-back inverters and collapse them" -> transformation_collapse_back_to_back_inverters.

=== Depth Optimization (optimization_reduce_depth) ===
- "Reduce the critical path depth through restructuring", "Optimize the logic to minimize maximum path depth", "Perform depth optimization on the combinational logic", "Optimize the logic depth of the design" -> optimization_reduce_depth(scope=design).
- Named signal with target depth: "Try to restructure n8 with a target depth of 4", "Try to optimize n15 to at most 4 levels deep", "Attempt to reduce the depth of the cone of n8 to 4" -> optimization_reduce_depth(scope=cone,target=X,max_depth=N).
- "For each output with depth greater than N, optimize its cone" -> optimization_reduce_depth(scope=outputs_over_depth,max_depth=N).
- KEY: Any request to optimize/reduce/restructure/balance logic depth is a TRANSFORMATION (optimization_reduce_depth), not an analysis. Analysis only when asking "What is / Compute / Determine" the depth value.

=== Functional-Preservation Clauses ===
"Ensure functionality does not change", "Make sure nothing changes functionally", "preserving functional equivalence", "Ensure the design functionality does not change" are CONSTRAINTS on a transformation/optimization request. They do NOT change the route to a verification tool. Choose verification tools (verification_functional_equivalence, verification_design_equivalence) ONLY when the main requested action is verify/prove/check/confirm the equivalence itself.
"""


def build_llm_system_prompt(tool_contract: Dict[str, Any]) -> str:
    protocol_tools = [
        {
            "name": "begin_testcase",
            "description": "Initialize a testcase and start writing responses to <case_name>.log.",
            "when_to_use": "Use for requests like 'This is the beginning of a new testcase. The case name is test03.'",
            "parameters": {
                "type": "object",
                "properties": {"case_name": {"type": "string"}},
                "required": ["case_name"],
            },
        },
        {
            "name": "design_load",
            "description": "Load a testcase design. For LLM parsing, return file_name and directory exactly from the request; the agent resolves the full path.",
            "when_to_use": "Use for requests asking to load/read a design from a file in a directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_name": {"type": "string"},
                    "directory": {"type": "string"},
                },
                "required": ["file_name", "directory"],
            },
        },
    ]
    spec_tools = [tool for tool in tool_contract.get("tools", []) if tool.get("name") != "design_load"]
    llm_tools = protocol_tools + spec_tools
    compact = []
    for tool in llm_tools:
        name = tool.get("name")
        if name in _TOOL_DESC_OVERRIDES:
            desc, wtu = _TOOL_DESC_OVERRIDES[name], ""
        else:
            desc, wtu = tool.get("description", ""), tool.get("when_to_use", "")
        compact.append(
            {
                "name": name,
                "description": desc,
                "when_to_use": wtu,
                "parameters": tool.get("parameters", {}),
            }
        )

    return (
        "You are the request parser for an ICCAD gate-level netlist EDA agent. "
        "Translate each natural-language request into exactly one JSON tool call. "
        "Return only JSON with this shape: {\"tool\": \"...\", \"arguments\": {...}}. "
        "Do not answer the EDA question yourself; the deterministic EDA backend will compute the answer. "
        "Preserve every net, signal, bus bit, gate, and file name exactly as written, including names such as n0[1]. "
        "Constant logic values must be written in Verilog form \"1'b0\" or \"1'b1\" (never \"0\" or \"1\") wherever a tool takes a constant value, including the constant argument of transformation_constant_propagation and the value argument of analysis_signal_constant. "
        + _LLM_ROUTING_GUIDE
        +
        "Available tool contract follows as JSON:\n"
        + json.dumps(
            {
                "common_notes": tool_contract.get("common_notes", []),
                "tools": compact,
            },
            ensure_ascii=False,
            sort_keys=False,
        )
    )


def build_llm_review_prompt(tool_contract: Dict[str, Any]) -> str:
    spec_tools = [tool for tool in tool_contract.get("tools", []) if tool.get("name") != "design_load"]
    compact = []
    for tool in spec_tools:
        name = tool.get("name")
        if name in _TOOL_DESC_OVERRIDES:
            desc, wtu = _TOOL_DESC_OVERRIDES[name], ""
        else:
            desc, wtu = tool.get("description", ""), tool.get("when_to_use", "")
        compact.append(
            {
                "name": name,
                "description": desc,
                "when_to_use": wtu,
                "parameters": tool.get("parameters", {}),
            }
        )
    compact.extend(
        [
            {
                "name": "begin_testcase",
                "description": "Start a testcase and reset parser/execution context.",
                "parameters": {
                    "type": "object",
                    "properties": {"case_name": {"type": "string"}},
                    "required": ["case_name"],
                },
            },
            {
                "name": "design_load",
                "description": "Load the testcase netlist.",
                "parameters": {
                    "type": "object",
                    "properties": {"file_name": {"type": "string"}, "directory": {"type": "string"}},
                    "required": ["file_name", "directory"],
                },
            },
        ]
    )
    return (
        "You are a strict semantic reviewer for an ICCAD gate-level netlist EDA request parser. "
        "You receive the original request and one candidate JSON tool call. "
        "Decide whether the candidate exactly preserves the user's requested operation or analysis. "
        "If it is correct, return the same tool and arguments. If it is wrong, return a corrected single tool call. "
        "Return only JSON with this shape: {\"valid\": true/false, \"tool\": \"...\", \"arguments\": {...}, \"reason\": \"short reason\"}. "
        "Do not solve the EDA task yourself. Do not invent signal names, gate names, or constants. "
        "Preserve every net, signal, bus bit, gate, and file name exactly as written, including bus bits such as n0[1]. "
        + _LLM_ROUTING_GUIDE
        +
        "Available tool contract follows as JSON:\n"
        + json.dumps(
            {
                "common_notes": tool_contract.get("common_notes", []),
                "tools": compact,
            },
            ensure_ascii=False,
            sort_keys=False,
        )
    )


class RequestParser:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.tool_contract = load_tool_contract(config.suite_root)
        self.system_prompt = build_llm_system_prompt(self.tool_contract)
        self.review_prompt = build_llm_review_prompt(self.tool_contract)

    def parse(self, request: str) -> ToolCall:
        mode = self.config.parser
        if mode not in {"regex", "llm", "hybrid"}:
            mode = "hybrid"

        if mode in {"regex", "hybrid"}:
            try:
                call = self._parse_with_regex(request)
                self._validate(call)
                return call
            except Exception:
                if mode == "regex":
                    raise

        try:
            call = self._parse_with_llm(request)
            self._validate(call)
            return call
        except json.JSONDecodeError:
            # 大模型偶发返回非法 JSON：退回 regex 再试一次，而不是直接失败。
            call = self._parse_with_regex(request)
            self._validate(call)
            return call

    def _validate(self, call: ToolCall) -> None:
        if call.tool not in ALLOWED_TOOLS:
            raise ValueError(f"unknown tool from {call.source}: {call.tool}")
        missing = [name for name in TOOL_REQUIRED_ARGS[call.tool] if name not in call.arguments]
        if missing:
            raise ValueError(f"missing arguments for {call.tool}: {missing}")

    def _parse_with_llm(self, request: str) -> ToolCall:
        if not self.config.api_key:
            raise RuntimeError("LLM API key is not configured")

        system = self.system_prompt
        user = f"Request: {request}"

        if self.config.provider == "anthropic":
            payload = self._call_anthropic(system, user)
        else:
            payload = self._call_openai(system, user)

        tool = str(payload.get("tool", ""))
        args = payload.get("arguments", {})
        if not isinstance(args, dict):
            raise ValueError("LLM arguments must be an object")
        call = ToolCall(tool=tool, arguments=args, source="llm")
        if self.config.llm_review:
            call = self._review_llm_call(request, call)
        return self._repair_llm_call(request, call)

    def _review_llm_call(self, request: str, call: ToolCall) -> ToolCall:
        user = json.dumps(
            {
                "request": request,
                "candidate": {"tool": call.tool, "arguments": call.arguments},
            },
            ensure_ascii=False,
        )
        try:
            if self.config.provider == "anthropic":
                payload = self._call_anthropic(self.review_prompt, user)
            else:
                payload = self._call_openai(self.review_prompt, user)
        except Exception:
            return call

        tool = str(payload.get("tool", call.tool))
        args = payload.get("arguments", call.arguments)
        if not isinstance(args, dict):
            return call
        return ToolCall(tool=tool, arguments=args, source=call.source)

    def _repair_llm_call(self, request: str, call: ToolCall) -> ToolCall:
        text = request.strip()
        lower = text.lower()
        args = dict(call.arguments)

        def repaired(tool: str, new_args: Dict[str, Any]) -> ToolCall:
            return ToolCall(tool, new_args, call.source)

        def gate_style(gate: str) -> Dict[str, Any]:
            return {"answer_style": gate.lower() + "_only"}

        match = re.search(r"Report the total (AND|OR|NOT|NAND|NOR|XOR|XNOR|BUF|DFF) gate count", text, flags=re.I)
        if match:
            return repaired("analysis_count_gates", gate_style(match.group(1)))
        match = re.search(r"How many (AND|OR|NOT|NAND|NOR|XOR|XNOR|BUF|DFF) gates are now in the design after", text, flags=re.I)
        if match:
            return repaired("analysis_count_gates", gate_style(match.group(1)))
        match = re.search(r"How many (AND|OR|NOT|NAND|NOR|XOR|XNOR|BUF|DFF) gates are (?:currently|now) in the design", text, flags=re.I)
        if match:
            return repaired("analysis_count_gates", gate_style(match.group(1)))
        match = re.search(r"How many (AND|OR|NOT|NAND|NOR|XOR|XNOR|BUF|DFF) gates are (?:currently|now) in the (?:reconstructed )?netlist", text, flags=re.I)
        if match:
            return repaired("analysis_count_gates", gate_style(match.group(1)))

        if "how many buf gates were added" in lower:
            return repaired("analysis_last_transform_stats", {"metric": "added_buffers"})
        match = re.search(r"How many (AND|OR|NOT|NAND|NOR|XOR|XNOR|BUF|DFF) gates were added by replacing the (XNOR|XOR) gates\?", text, flags=re.I)
        if match:
            return repaired(
                "analysis_last_transform_stats",
                {"metric": "added_by_type", "gate_type": match.group(1).upper(), "tool": "transformation_replace_gate_library"},
            )
        match = re.search(r"How many (?:(AND|OR|NAND|NOR|XOR|XNOR|BUF|DFF) )?gates were eliminated by constant(?:-[01])? propagation\?", text, flags=re.I)
        if match:
            new_args: Dict[str, Any] = {"metric": "eliminated_gates", "tool": "transformation_constant_propagation"}
            if match.group(1):
                new_args["gate_type"] = match.group(1).lower()
            return repaired("analysis_last_transform_stats", new_args)
        if "how many dangling gates were removed" in lower:
            return repaired("analysis_last_transform_stats", {"metric": "removed_gates", "tool": "transformation_remove_dangling_logic"})
        if "how many redundant gates were removed" in lower or re.search(r"How many gates were merged as structural duplicates\?", text, flags=re.I):
            return repaired("analysis_last_transform_stats", {"metric": "merged_gates", "tool": "optimization_merge_equivalent_or_duplicate_gates"})

        match = re.search(r"Replace all 2-input OR gates in the cone of (\S+) with equivalent logic (?:built only from|using only) NAND and NOT", text, flags=re.I)
        if match:
            return repaired("transformation_replace_gate_library", {"scope": "cone", "target": _strip_quotes(match.group(1)), "from_gate": "or", "to_library": "nand_not"})
        match = re.search(r"(?:logic cone of|cone of output) (\S+) (?:to use|using) only (NAND|NOR) and NOT", text, flags=re.I)
        if match:
            return repaired("transformation_replace_gate_library", {"scope": "cone", "target": _strip_quotes(match.group(1)), "to_library": match.group(2).lower() + "_not"})
        if "decompose all xor gates" in lower and "and, or, and not" in lower:
            target = re.search(r"fanin cone of (\S+)", text, flags=re.I)
            return repaired(
                "transformation_replace_gate_library",
                {"scope": "cone", "target": _strip_quotes(target.group(1)) if target else None, "from_gate": "xor", "to_library": "and_or_not"},
            )
        if "nand gates that have one input tied to constant 1" in lower:
            return repaired("transformation_constant_propagation", {"gate_type": "nand", "constant": "1'b1"})
        match = re.search(r"(?:Report|List|Find|Identify) any (AND|OR|NAND|NOR|XOR|XNOR|BUF|DFF|NOT) gates with (?:a )?constant ([01]) input", text, flags=re.I)
        if match:
            return repaired(
                "transformation_constant_propagation",
                {"gate_type": match.group(1).lower(), "constant": "1'b" + match.group(2), "report_only": True},
            )
        if "xnor" in lower and "nor" in lower and ("replace" in lower or "convert" in lower or "rewrite" in lower):
            args["to_library"] = "nor_not"
            if args.get("target") in {"nor", "nor_only", "nor_not"}:
                args.pop("target", None)
            if args.get("from_gate") not in (None, "", "xnor"):
                args.pop("from_gate", None)
            args.setdefault("scope", "design")
            args["from_gate"] = "xnor"
            return repaired("transformation_replace_gate_library", args)

        if "for each output with depth greater than" in lower and "optimize its cone" in lower:
            match = re.search(r"depth greater than (\d+)", text, flags=re.I)
            return repaired("optimization_reduce_depth", {"max_depth": int(match.group(1)) if match else 4, "scope": "outputs_over_depth"})
        if "functionally equivalent" in lower and "merge" in lower:
            return repaired("optimization_merge_equivalent_or_duplicate_gates", {"mode": "functional"})
        if "structural duplicates" in lower and ("merge" in lower or "same boolean function" in lower):
            return repaired("optimization_merge_equivalent_or_duplicate_gates", {"mode": "structural"})

        if "pre-transformation" in lower or "pre transformation" in lower or "pre-transform" in lower:
            return repaired("verification_design_equivalence", {"reference": "pre_transform"})
        if "current netlist is functionally equivalent" in lower or "current design and the original loaded netlist" in lower or "design is still functionally equivalent" in lower:
            return repaired("verification_design_equivalence", {"reference": "original"})

        match = re.search(r"fanout of primary input (\S+).*drives directly", text, flags=re.I)
        if match:
            return repaired("analysis_direct_fanout", {"signal": _strip_quotes(match.group(1))})

        if call.tool == "transformation_replace_gate_library":
            library_aliases = {"nand_only": "nand_not", "nor_only": "nor_not", "and_only": "and_not"}
            cone_target = re.search(r"(?:in the )?cone of (\S+)", text, flags=re.I)
            if cone_target and str(args.get("target", "")).lower() in {"nand_not", "nor_not", "and_not", "and_or_not", "nand_only", "nor_only", "and_only"}:
                args["scope"] = "cone"
                args["target"] = _strip_quotes(cone_target.group(1))
            library_match = re.search(r"(?:using|built) only (NAND|NOR|AND) and NOT", text, flags=re.I)
            if library_match:
                args["to_library"] = library_match.group(1).lower() + "_not"
            if isinstance(args.get("to_library"), str):
                args["to_library"] = library_aliases.get(str(args["to_library"]).lower(), args["to_library"])
            if isinstance(args.get("from_gate"), str) and str(args["from_gate"]).lower() in {"or2", "xor2", "xnor2", "nand2", "nor2"}:
                args["from_gate"] = str(args["from_gate"]).lower().removesuffix("2")
            if args != call.arguments:
                return repaired(call.tool, args)
        return call

    def _call_openai(self, system: str, user: str) -> Dict[str, Any]:
        base_url = self.config.base_url or "https://api.openai.com/v1"
        url = base_url.rstrip("/")
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        body = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_output_tokens,
            "response_format": {"type": "json_object"},
        }
        data = _json_post(
            url,
            {
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            body,
        )
        content = data["choices"][0]["message"]["content"]
        return _extract_json_object(content)

    def _call_anthropic(self, system: str, user: str) -> Dict[str, Any]:
        base_url = self.config.base_url or "https://api.anthropic.com/v1/messages"
        url = base_url.rstrip("/")
        if not url.endswith("/messages"):
            url += "/messages"
        body = {
            "model": self.config.model,
            "max_tokens": self.config.max_output_tokens,
            "temperature": self.config.temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        data = _json_post(
            url,
            {
                "x-api-key": str(self.config.api_key),
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            body,
        )
        content_blocks = data.get("content", [])
        text = "".join(block.get("text", "") for block in content_blocks if block.get("type") == "text")
        return _extract_json_object(text)

    def _parse_with_regex(self, request: str) -> ToolCall:
        text = request.strip()
        lower = text.lower()

        match = re.search(r"case name is\s+([A-Za-z0-9_.-]+)", text, flags=re.I)
        if match:
            return ToolCall("begin_testcase", {"case_name": _strip_quotes(match.group(1))}, "regex")

        match = re.search(r"load the design from the file\s+(\S+)\s+located in the directory\s+(\S+)", text, flags=re.I)
        if match:
            return ToolCall(
                "design_load",
                {"file_name": _strip_quotes(match.group(1)), "directory": _strip_quotes(match.group(2))},
                "regex",
            )

        match = re.search(r"write the current design to the output file\s+(\S+)", text, flags=re.I)
        if match:
            return ToolCall("design_write", {"output_path": _strip_quotes(match.group(1))}, "regex")

        if "count all the gates" in text.lower():
            return ToolCall("analysis_count_gates", {}, "regex")

        if text == "Compute the total gate count of the design.":
            return ToolCall("analysis_count_gates", {"answer_style": "total_only"}, "regex")

        match = re.search(r"How many gates are in the (?:fanin |logic )cone of (?:primary )?output (\S+)\?", text)
        if match:
            return ToolCall("analysis_fanin_cone_size", {"output": _strip_quotes(match.group(1))}, "regex")

        match = re.search(r"Compute the maximum logic depth of the fanin cone of output (\S+)\.", text)
        if match:
            return ToolCall("analysis_max_logic_depth", {"target": _strip_quotes(match.group(1))}, "regex")

        match = re.search(r"List all gates currently driven by signal (\S+)\.?", text, flags=re.I)
        if match:
            return ToolCall("analysis_direct_fanout", {"signal": _strip_quotes(match.group(1))}, "regex")
        match = re.search(r"What is the fanout of primary input (\S+)\? List (?:all|every) gates? that \1 drives directly\.", text)
        if match:
            return ToolCall("analysis_direct_fanout", {"signal": _strip_quotes(match.group(1))}, "regex")

        match = re.search(r"Does every path from input (\S+) to output (\S+) pass through gate (\S+)\?", text, flags=re.I)
        if match:
            src, dst, gate = match.groups()
            return ToolCall("analysis_path_exists_avoiding", {"source": src, "target": dst, "avoid": _strip_quotes(gate), "answer_style": "all_paths_through"}, "regex")
        match = re.search(r"Does a combinational path exist from primary input (\S+) to primary output (\S+)\?", text, flags=re.I)
        if match:
            return ToolCall("analysis_path_exists", {"source": _strip_quotes(match.group(1)), "target": _strip_quotes(match.group(2))}, "regex")
        match = re.search(r"Does a combinational path from (\S+) to (\S+) exist that avoids (\S+)\?", text, flags=re.I)
        if match:
            src, dst, avoid = match.groups()
            return ToolCall("analysis_path_exists_avoiding", {"source": src, "target": dst, "avoid": _strip_quotes(avoid)}, "regex")

        match = re.search(r"Determine whether a combinational path from (\S+) to (\S+) exists that does not traverse node (\S+)\.", text)
        if match:
            src, dst, avoid = match.groups()
            return ToolCall("analysis_path_exists_avoiding", {"source": src, "target": dst, "avoid": _strip_quotes(avoid)}, "regex")

        match = re.search(r"Verify whether a path connecting input (\S+) to output (\S+) exists while avoiding (\S+)\.", text)
        if match:
            src, dst, avoid = match.groups()
            return ToolCall("analysis_path_exists_avoiding", {"source": src, "target": dst, "avoid": _strip_quotes(avoid)}, "regex")

        match = re.search(r"Find all combinational paths from primary input (\S+) to primary output (\S+) and list each path", text, flags=re.I)
        if match:
            src, dst = match.groups()
            return ToolCall("analysis_enumerate_paths", {"source": src, "target": _strip_quotes(dst), "path_limit": 1_000_000}, "regex")
        match = re.search(r"List every path originating at primary input (\S+) and terminating at primary output (\S+)\.", text)
        if match:
            src, dst = match.groups()
            return ToolCall("analysis_enumerate_paths", {"source": src, "target": _strip_quotes(dst), "path_limit": 1_000_000}, "regex")

        match = re.search(r"Provide a complete enumeration of paths between (\S+) and (\S+)\.", text)
        if match:
            src, dst = match.groups()
            return ToolCall("analysis_enumerate_paths", {"source": src, "target": _strip_quotes(dst), "path_limit": 1_000_000}, "regex")

        match = re.search(r"Compute the maximum logic depth from input (\S+) to output (\S+)\.", text)
        if match:
            src, dst = match.groups()
            return ToolCall("analysis_max_logic_depth", {"source": src, "target": _strip_quotes(dst)}, "regex")

        match = re.search(r"Determine the longest combinational path depth from (\S+) to (\S+)\.", text)
        if match:
            src, dst = match.groups()
            return ToolCall("analysis_max_logic_depth", {"source": src, "target": _strip_quotes(dst)}, "regex")

        match = re.search(r"Calculate the critical path depth between (\S+) and (\S+)\.", text)
        if match:
            src, dst = match.groups()
            return ToolCall("analysis_max_logic_depth", {"source": src, "target": _strip_quotes(dst)}, "regex")

        match = re.search(r"Determine the number of gates driven by (\S+)\.", text)
        if match:
            return ToolCall("analysis_gate_successors", {"gate": _strip_quotes(match.group(1)), "answer_style": "count_only"}, "regex")

        match = re.search(r"Enumerate the immediate successors of gate (\S+)\.", text)
        if match:
            return ToolCall("analysis_gate_successors", {"gate": _strip_quotes(match.group(1))}, "regex")

        match = re.search(r"Compute the transitive fanin cone of output (\S+)\.", text)
        if match:
            return ToolCall("analysis_transitive_fanin_cone", {"output": _strip_quotes(match.group(1))}, "regex")

        match = re.search(r"What is the transitive fanout of primary input (\S+)\?", text, flags=re.I)
        if match:
            return ToolCall("analysis_transitive_fanout_cone", {"input": _strip_quotes(match.group(1))}, "regex")
        match = re.search(r"Compute the transitive fanout cone of input (\S+)\.", text)
        if match:
            return ToolCall("analysis_transitive_fanout_cone", {"input": _strip_quotes(match.group(1))}, "regex")

        match = re.search(r"Determine whether signals (\S+) and (\S+) are functionally equivalent\.", text)
        if match:
            left, right = match.groups()
            return ToolCall("verification_functional_equivalence", {"left": left, "right": _strip_quotes(right)}, "regex")

        match = re.search(r"Verify that (\S+) and (\S+) produce identical logic values for all inputs\.", text)
        if match:
            left, right = match.groups()
            return ToolCall("verification_functional_equivalence", {"left": left, "right": _strip_quotes(right)}, "regex")

        match = re.search(r"Check whether internal signals (\S+) and (\S+) are functionally equivalent for all input combinations", text, flags=re.I)
        if match:
            left, right = match.groups()
            return ToolCall("verification_functional_equivalence", {"left": left, "right": _strip_quotes(right)}, "regex")

        match = re.search(r"Check functional equivalence between internal signals (\S+) and (\S+)\.", text)
        if match:
            left, right = match.groups()
            return ToolCall("verification_functional_equivalence", {"left": left, "right": _strip_quotes(right)}, "regex")

        if "determine all gates reachable from" in lower:
            match = re.search(r"reachable from (\S+)", text, flags=re.I)
            if match:
                return ToolCall("analysis_transitive_fanout_cone", {"input": _strip_quotes(match.group(1))}, "regex")

        # Transformation / Optimization parser rules for official test21-test40 prompts.
        if "insert buffers" in lower and "no gate" in lower and "more than 4 loads" in lower:
            return ToolCall("transformation_limit_fanout", {"max_fanout": 4}, "regex")
        match = re.search(r"Perform fanout optimization across the netlist with maximum fanout (\d+)", text, flags=re.I)
        if match:
            return ToolCall("transformation_limit_fanout", {"max_fanout": int(match.group(1))}, "regex")
        match = re.search(r"insert buffers on the (?:clock|reset) signal (\S+) to reduce its fanout (?:so no single driver has more than|to at most) (\d+) loads", text, flags=re.I)
        if match:
            return ToolCall("transformation_limit_fanout", {"signal": _strip_quotes(match.group(1)), "max_fanout": int(match.group(2))}, "regex")
        match = re.search(r"insert a BUF gate on signal (\S+) so that each load of \1 is driven through a dedicated buffer", text, flags=re.I)
        if match:
            return ToolCall("transformation_insert_dedicated_buffers", {"signal": _strip_quotes(match.group(1))}, "regex")
        if "how many buf gates were added" in lower:
            return ToolCall("analysis_last_transform_stats", {"metric": "added_buffers"}, "regex")
        if "reduce the critical path depth" in lower or "reduce critical path depth" in lower or "perform depth optimization" in lower or "optimize the logic depth" in lower or "minimize maximum path depth" in lower:
            return ToolCall("optimization_reduce_depth", {}, "regex")
        if "for each output with depth greater than" in lower and "optimize its cone" in lower:
            match = re.search(r"depth greater than (\d+)", text, flags=re.I)
            return ToolCall("optimization_reduce_depth", {"max_depth": int(match.group(1)) if match else 4, "scope": "outputs_over_depth"}, "regex")
        match = re.search(r"What is the maximum combinational depth on any register-to-register path in this design\?", text, flags=re.I)
        if match:
            return ToolCall("analysis_register_path_depth", {}, "regex")
        match = re.search(r"Attempt to reduce the depth of the cone of (\S+) to (\d+)", text, flags=re.I)
        if match:
            return ToolCall("optimization_reduce_depth", {"target": _strip_quotes(match.group(1)), "max_depth": int(match.group(2)), "scope": "cone"}, "regex")
        match = re.search(r"What is the depth of the cone of (\S+) now\?", text, flags=re.I)
        if match:
            return ToolCall("analysis_max_logic_depth", {"target": _strip_quotes(match.group(1))}, "regex")
        match = re.search(r"Try to (?:optimize|restructure) (\S+) (?:to at most|with a target depth of) (\d+)(?: levels deep)?", text, flags=re.I)
        if match:
            return ToolCall("optimization_reduce_depth", {"target": _strip_quotes(match.group(1)), "max_depth": int(match.group(2)), "scope": "cone"}, "regex")
        match = re.search(r"logic cone of output (\S+) targeting depth (\d+) or less", text, flags=re.I)
        if match:
            return ToolCall("optimization_reduce_depth", {"target": _strip_quotes(match.group(1)), "max_depth": int(match.group(2)), "scope": "cone"}, "regex")
        match = re.search(r"How many gates were merged as structural duplicates\?", text, flags=re.I)
        if match:
            return ToolCall("analysis_last_transform_stats", {"metric": "merged_gates", "tool": "optimization_merge_equivalent_or_duplicate_gates"}, "regex")
        if "structural duplicates" in lower and ("merge" in lower or "same boolean function" in lower):
            return ToolCall("optimization_merge_equivalent_or_duplicate_gates", {"mode": "structural"}, "regex")
        if "functionally equivalent" in lower and "merge" in lower:
            return ToolCall("optimization_merge_equivalent_or_duplicate_gates", {"mode": "functional"}, "regex")
        if "how many dangling gates were removed" in lower:
            return ToolCall("analysis_last_transform_stats", {"metric": "removed_gates", "tool": "transformation_remove_dangling_logic"}, "regex")
        if any(phrase in lower for phrase in ["trim unused", "eliminate unused", "dangling gates", "floating nodes", "prune the netlist", "sweep out dangling", "do not contribute to any primary output", "not connected to any primary output"]):
            if lower.startswith("check"):
                return ToolCall("analysis_floating_or_unconnected", {}, "regex") if "floating" in lower else ToolCall("transformation_remove_dangling_logic", {}, "regex")
            return ToolCall("transformation_remove_dangling_logic", {}, "regex")
        match = re.search(r"(?:Rename gate|Change the identifier of gate) (\S+) to (\S+)", text, flags=re.I)
        if match:
            return ToolCall("transformation_rename_identifier", {"old_name": _strip_quotes(match.group(1)), "new_name": _strip_quotes(match.group(2)), "kind": "gate"}, "regex")
        match = re.search(r"(?:wire|signal) (\S+) to (renamed_\w+)", text, flags=re.I)
        if ("rename" in lower or "identifier" in lower or "update the name" in lower) and match:
            return ToolCall("transformation_rename_identifier", {"old_name": _strip_quotes(match.group(1)), "new_name": _strip_quotes(match.group(2)), "kind": "signal"}, "regex")
        match = re.search(r"reconnect input pin (\S+) of gate (\S+) to internal signal (\S+)", text, flags=re.I)
        if match:
            pin, gate, signal = match.groups()
            return ToolCall("transformation_reconnect_gate_input", {"gate": gate, "pin": pin, "signal": _strip_quotes(signal)}, "regex")
        if "back-to-back inverter" in lower or "not followed by not" in lower:
            return ToolCall("transformation_collapse_back_to_back_inverters", {}, "regex")
        match = re.search(r"Report any (AND|OR|NAND|NOR) gates? with (?:a )?constant ([01]) input", text, flags=re.I)
        if match:
            return ToolCall("transformation_constant_propagation", {"gate_type": match.group(1).lower(), "constant": f"1'b{match.group(2)}", "report_only": True}, "regex")
        match = re.search(r"Report any (NAND|NOR) gates? with constant inputs", text, flags=re.I)
        if match:
            return ToolCall("transformation_constant_propagation", {"gate_type": match.group(1).lower(), "report_only": True}, "regex")
        match = re.search(r"List all gates with one or more inputs tied to 1'b1\.?", text, flags=re.I)
        if match:
            return ToolCall("transformation_constant_propagation", {"gate_type": None, "constant": "1'b1", "report_only": True}, "regex")
        match = re.search(r"How many (?:(AND|OR|NAND|NOR|XOR|XNOR|BUF|DFF) )?gates were eliminated by constant(?:-[01])? propagation\?", text, flags=re.I)
        if match:
            args = {"metric": "eliminated_gates", "tool": "transformation_constant_propagation"}
            if match.group(1):
                args["gate_type"] = match.group(1).lower()
            return ToolCall("analysis_last_transform_stats", args, "regex")
        if "constant propagation" in lower or "constant-0 propagation" in lower or "constant-1 propagation" in lower or "propagating their constant" in lower:
            gtm = re.search(r"\b(nand|nor|xnor|xor|and|or)\s+gates?\b", lower)
            gate_type = gtm.group(1) if gtm else None
            return ToolCall("transformation_constant_propagation", {"gate_type": gate_type}, "regex")
        if "nand gates that have one input tied to constant 1" in lower:
            return ToolCall("transformation_constant_propagation", {"gate_type": "nand", "constant": "1'b1"}, "regex")
        if "reconstruct the entire netlist using only and and not" in lower:
            return ToolCall("transformation_replace_gate_library", {"scope": "design", "to_library": "and_not"}, "regex")
        if "remap the entire design to use only nand and not" in lower:
            return ToolCall("transformation_replace_gate_library", {"scope": "design", "to_library": "nand_not"}, "regex")
        match = re.search(r"(?:logic cone of|cone of output) (\S+) (?:to use|using) only (NAND|NOR) and NOT", text, flags=re.I)
        if match:
            return ToolCall("transformation_replace_gate_library", {"scope": "cone", "target": _strip_quotes(match.group(1)), "to_library": match.group(2).lower() + "_not"}, "regex")
        match = re.search(r"Replace all 2-input OR gates in the cone of (\S+) with equivalent logic built only from NAND and NOT", text, flags=re.I)
        if match:
            return ToolCall("transformation_replace_gate_library", {"scope": "cone", "target": _strip_quotes(match.group(1)), "from_gate": "or", "to_library": "nand_not"}, "regex")
        if "decompose all xor gates" in lower and "and, or, and not" in lower:
            target = re.search(r"fanin cone of (\S+)", text, flags=re.I)
            return ToolCall("transformation_replace_gate_library", {"scope": "cone", "target": _strip_quotes(target.group(1)) if target else None, "from_gate": "xor", "to_library": "and_or_not"}, "regex")
        if "xor" in lower and "nand" in lower and ("replace" in lower or "convert" in lower):
            return ToolCall("transformation_replace_gate_library", {"scope": "design", "from_gate": "xor", "to_library": "nand_not"}, "regex")
        if "xnor" in lower and "nor" in lower and ("replace" in lower or "convert" in lower or "rewrite" in lower):
            return ToolCall("transformation_replace_gate_library", {"scope": "design", "from_gate": "xnor", "to_library": "nor_not"}, "regex")
        match = re.search(r"How many redundant gates were removed\?", text, flags=re.I)
        if match:
            return ToolCall("analysis_last_transform_stats", {"metric": "merged_gates", "tool": "optimization_merge_equivalent_or_duplicate_gates"}, "regex")
        if "redundant gates" in lower and ("remove" in lower or "removed" in lower or "eliminate" in lower):
            return ToolCall("optimization_merge_equivalent_or_duplicate_gates", {"mode": "structural"}, "regex")
        match = re.search(r"Compute the fanin logic cone of output (\S+) and list all gates that contribute to this output\.?", text, flags=re.I)
        if match:
            return ToolCall("analysis_transitive_fanin_cone", {"output": _strip_quotes(match.group(1))}, "regex")
        match = re.search(r"Compute the fanin logic cone of output (\S+)\.?$", text.strip(), flags=re.I)
        if match:
            return ToolCall("analysis_transitive_fanin_cone", {"output": _strip_quotes(match.group(1))}, "regex")
        if "merge" in lower and ("structural duplicates" in lower or "functionally equivalent" in lower or "same boolean function" in lower):
            return ToolCall("optimization_merge_equivalent_or_duplicate_gates", {"mode": "structural"}, "regex")

        match = re.search(r"List all gates that now connect to the renamed signal (\S+)", text, flags=re.I)
        if match:
            return ToolCall("analysis_direct_fanout", {"signal": _strip_quotes(match.group(1))}, "regex")

        # Extended analysis parser rules.
        if "pre-transformation" in lower or "pre transformation" in lower or "pre-transform" in lower:
            return ToolCall("verification_design_equivalence", {"reference": "pre_transform"}, "regex")
        if "current netlist is functionally equivalent" in lower or "current design and the original loaded netlist" in lower or "transformed design is equivalent" in lower or "design is still functionally equivalent" in lower:
            return ToolCall("verification_design_equivalence", {"reference": "original"}, "regex")
        match = re.search(r"What is the maximum fanout of (\S+) now", text, flags=re.I)
        if match:
            return ToolCall("analysis_max_fanout", {"signal": _strip_quotes(match.group(1))}, "regex")
        if "which primary input has the highest fanout" in lower:
            return ToolCall("analysis_max_fanout", {"scope": "primary_inputs", "answer_style": "signals_only"}, "regex")
        match = re.search(r"Determine whether gate (\S+) lies on any maximum-depth path", text, flags=re.I)
        if match:
            return ToolCall("analysis_gate_on_max_depth_path", {"gate": _strip_quotes(match.group(1))}, "regex")
        if "number of primary inputs and outputs" in lower or "how many primary inputs and primary outputs" in lower:
            return ToolCall("analysis_primary_io_summary", {"answer_style": "counts"}, "regex")
        if "primary inputs" in lower and "bit widths" in lower:
            return ToolCall("analysis_primary_io_summary", {"answer_style": "inputs"}, "regex")
        if "primary outputs" in lower and "bit widths" in lower:
            return ToolCall("analysis_primary_io_summary", {"answer_style": "outputs"}, "regex")
        match = re.search(r"What type of gate is (\S+)\?", text, flags=re.I)
        if match:
            return ToolCall("analysis_gate_info", {"gate": _strip_quotes(match.group(1))}, "regex")
        match = re.search(r"Report every gate connected to the output of (\S+)", text, flags=re.I)
        if match:
            return ToolCall("analysis_gate_successors", {"gate": _strip_quotes(match.group(1))}, "regex")
        match = re.search(r"Report the total (AND|OR|NOT|NAND|NOR|XOR|XNOR|BUF|DFF) gate count", text, flags=re.I)
        if match:
            return ToolCall("analysis_count_gates", {"answer_style": match.group(1).lower() + "_only"}, "regex")
        match = re.search(r"How many (AND|OR|NOT|NAND|NOR|XOR|XNOR|BUF|DFF) gates were added by replacing the XNOR gates\?", text, flags=re.I)
        if match:
            return ToolCall("analysis_last_transform_stats", {"metric": "added_by_type", "gate_type": match.group(1).upper(), "tool": "transformation_replace_gate_library"}, "regex")
        match = re.search(r"How many (AND|OR|NOT|NAND|NOR|XOR|XNOR|BUF|DFF) gates were added by replacing the XOR gates\?", text, flags=re.I)
        if match:
            return ToolCall("analysis_last_transform_stats", {"metric": "added_by_type", "gate_type": match.group(1).upper(), "tool": "transformation_replace_gate_library"}, "regex")
        match = re.search(r"How many (AND|OR|NOT|NAND|NOR|XOR|XNOR|BUF|DFF) gates are now in the reconstructed netlist\?", text, flags=re.I)
        if match:
            return ToolCall("analysis_count_gates", {"answer_style": match.group(1).lower() + "_only"}, "regex")
        match = re.search(r"How many (AND|OR|NOT|NAND|NOR|XOR|XNOR|BUF|DFF) gates are now in the restructured cone of output (\S+)\?", text, flags=re.I)
        if match:
            return ToolCall("analysis_cone_gate_type_count", {"target": _strip_quotes(match.group(2)), "answer_style": match.group(1).lower() + "_only"}, "regex")
        match = re.search(r"How many (AND|OR|NOT|NAND|NOR|XOR|XNOR|BUF|DFF) gates are (?:currently|now) in the design", text, flags=re.I)
        if match:
            return ToolCall("analysis_count_gates", {"answer_style": match.group(1).lower() + "_only"}, "regex")
        match = re.search(r"List all (NAND|XOR) gates", text, flags=re.I)
        if match:
            return ToolCall("analysis_list_gates_by_type", {"gate_type": match.group(1).lower()}, "regex")
        match = re.search(r"number of each gate type in the cone of (\S+)", text, flags=re.I)
        if match:
            return ToolCall("analysis_cone_gate_type_count", {"target": _strip_quotes(match.group(1))}, "regex")
        match = re.search(r"gates shared between the fanin cones of (\S+) and (\S+)", text, flags=re.I)
        if match:
            return ToolCall("analysis_shared_fanin_cone", {"left": match.group(1), "right": _strip_quotes(match.group(2))}, "regex")
        if "paths of length 0" in lower:
            return ToolCall("analysis_zero_length_paths", {}, "regex")
        if "register-to-register paths" in lower:
            return ToolCall("analysis_register_paths", {}, "regex")
        if "primary input to any dff d-pin" in lower:
            return ToolCall("analysis_pi_to_dff_depth", {}, "regex")
        match = re.search(r"How many outputs have a logic depth greater than (\d+)", text, flags=re.I)
        if match:
            return ToolCall("analysis_outputs_depth_over", {"threshold": int(match.group(1))}, "regex")
        if "maximum combinational depth from any primary input to any primary output" in lower or "maximum combinational logic depth in the design" in lower:
            return ToolCall("analysis_max_logic_depth", {}, "regex")
        if "which output bit has the deepest" in lower:
            return ToolCall("analysis_deepest_output", {}, "regex")
        if "which output has the largest fanin cone" in lower:
            return ToolCall("analysis_largest_fanin_cone_output", {}, "regex")
        match = re.search(r"List all flip-flops driven by clock (\S+)", text, flags=re.I)
        if match:
            return ToolCall("analysis_dffs_by_clock", {"clock": _strip_quotes(match.group(1))}, "regex")
        match = re.search(r"How many floating signals were found\?", text, flags=re.I)
        if match:
            return ToolCall("analysis_floating_or_unconnected", {}, "regex")
        if "floating inputs" in lower or "unconnected output ports" in lower:
            return ToolCall("analysis_floating_or_unconnected", {}, "regex")
        match = re.search(r"(?:wire|internal signal) (\S+) is a cut between any primary input and any primary output", text, flags=re.I)
        if match:
            return ToolCall("analysis_cut_or_articulation", {"signal": _strip_quotes(match.group(1)), "scope": "pi_to_po"}, "regex")
        match = re.search(r"(?:wire|internal signal) (\S+) is a cut", text, flags=re.I)
        if match:
            return ToolCall("analysis_cut_or_articulation", {"signal": _strip_quotes(match.group(1))}, "regex")
        match = re.search(r"articulation points(?: in the combinational graph)? between (\S+) and (\S+)", text, flags=re.I)
        if match:
            return ToolCall("analysis_articulation_points_between", {"source": _strip_quotes(match.group(1)), "target": _strip_quotes(match.group(2))}, "regex")
        match = re.search(r"(?:Boolean equation for output|Boolean function does output|logic expression for) (\S+)", text, flags=re.I)
        if match:
            return ToolCall("analysis_boolean_expression", {"target": _strip_quotes(match.group(1))}, "regex")
        match = re.search(r"Does output (\S+) depend on input (\S+)\?", text, flags=re.I)
        if match:
            return ToolCall("analysis_signal_dependency", {"output": match.group(1), "input": _strip_quotes(match.group(2))}, "regex")
        match = re.search(r"function at (\S+) is symmetric with respect to inputs (\S+) and (\S+)", text, flags=re.I)
        if match:
            return ToolCall("analysis_signal_symmetry", {"target": match.group(1), "input_a": match.group(2), "input_b": _strip_quotes(match.group(3))}, "regex")
        match = re.search(r"Is output (\S+) always ([01])", text, flags=re.I)
        if match:
            return ToolCall(
                "analysis_signal_constant",
                {"signal": _strip_quotes(match.group(1)), "value": f"1'b{match.group(2)}"},
                "regex",
            )
        match = re.search(r"NAND\(a, b\) is equivalent to (\S+)", text, flags=re.I)
        if match:
            target = _strip_quotes(match.group(1)).rstrip("?.,;:!")
            return ToolCall("analysis_find_nand_equivalent", {"target": target}, "regex")
        if "enable or hold structures" in lower:
            return ToolCall("analysis_dff_enable_hold_structures", {}, "regex")

        raise ValueError(f"unable to parse request: {request}")


def format_name_list(names: Iterable[str]) -> str:
    items = list(names)
    if not items:
        return "(none)"
    return ", ".join(items)


def _result_sentence(result: Dict[str, Any]) -> str:
    """最后兜底：绝不把原始 JSON 写进赛题 log，把结果字典转成可读句子。"""
    skip = {"ok", "tool", "design_id"}
    parts = [f"{k.replace('_', ' ')} = {v}" for k, v in result.items() if k not in skip]
    return ("; ".join(parts) + ".") if parts else "Done."


def _normalize_logic_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"0", "1'b0", "1b0", "false"}:
        return "0"
    if text in {"1", "1'b1", "1b1", "true"}:
        return "1"
    return text


def render_answer(call: ToolCall, result: Dict[str, Any], request: str) -> str:
    tool = call.tool
    args = call.arguments

    if tool == "begin_testcase":
        case_name = result["case_name"]
        return (
            f'Acknowledged. Initialized testcase "{case_name}". '
            f'All subsequent responses will be recorded to {case_name}.log.'
        )

    if tool == "design_load":
        return f'Loaded gate-level Verilog from "{result["netlist_path"]}" successfully.'

    if tool == "design_write":
        return f'Wrote the current design to "{result["output_path"]}" successfully.'

    if tool == "analysis_count_gates":
        gate_count_match = re.search(r"How many\s+(AND|OR|NOT|NAND|NOR|XOR|XNOR|BUF|DFF)\s+gates", request, flags=re.I)
        if gate_count_match:
            return str(result["by_type"].get(gate_count_match.group(1).upper(), 0))
        if args.get("answer_style") == "total_only" or request.strip() == "Compute the total gate count of the design.":
            return str(result["total"])
        if str(args.get("answer_style", "")).endswith("_only"):
            gate = str(args["answer_style"]).removesuffix("_only").upper()
            return str(result["by_type"].get(gate, 0))
        by_type = result["by_type"]
        parts = [f"{gate.upper()}={by_type.get(gate.upper(), 0)}" for gate in GATE_COUNT_ORDER]
        return f"Total gates: {result['total']}; " + ", ".join(parts)

    if tool == "analysis_fanin_cone_size":
        return str(result["gate_count"])

    if tool == "analysis_direct_fanout":
        return f"{result['gate_count']} gates: {format_name_list(result['gates'])}"

    if tool in {"analysis_path_exists", "analysis_path_exists_avoiding"}:
        all_paths_match = re.search(
            r"Does every path from input \S+ to output \S+ pass through gate \S+\?",
            request,
            flags=re.I,
        )
        if args.get("answer_style") == "all_paths_through" or all_paths_match:
            return "NO" if result["exists"] else "YES"
        return "YES" if result["exists"] else "NO"

    if tool == "analysis_enumerate_paths":
        count = result["path_count"]
        if count == 0:
            return "0 paths"
        if not result["complete"]:
            return f"{count} paths; enumeration skipped because path_limit={result['path_limit']}"
        paths = [" -> ".join(path) for path in result["paths"]]
        return f"{count} paths\n" + "\n".join(paths)

    if tool == "analysis_max_logic_depth":
        if not result["path_exists"]:
            return "No combinational path exists; depth is undefined"
        return str(result["depth"])

    if tool == "analysis_gate_successors":
        if args.get("answer_style") == "count_only" or "number of gates driven" in request:
            return str(result["successor_count"])
        return format_name_list(result["successors"])

    if tool == "analysis_transitive_fanin_cone":
        return f"{result['gate_count']} gates: {format_name_list(result['gates'])}"

    if tool == "analysis_transitive_fanout_cone":
        return f"{result['gate_count']} gates: {format_name_list(result['gates'])}"

    if tool == "verification_functional_equivalence":
        if result["status"] == "equivalent":
            return "YES"
        if result["status"] == "different":
            return "NO"
        return "UNKNOWN; " + result["message"]

    if tool.startswith("verification_design_equivalence"):
        return "YES" if result.get("equivalent") else "UNKNOWN; " + result.get("message", result.get("status", "not proven"))

    if tool == "analysis_last_transform_stats":
        return str(result.get("value", 0))

    if tool.startswith("transformation_") or tool.startswith("optimization_"):
        style = str(args.get("answer_style", ""))
        if tool == "transformation_constant_propagation" and result.get("report_only"):
            gates = [str(item.get("gate")) for item in result.get("reported_gates", [])]
            return f"{len(gates)} gates: {format_name_list(gates)}"
        if style in {"merged_count_only", "redundant_removed"}:
            return str(result.get("merged_gates", 0))
        if style == "nor_added":
            return str(result.get("added_by_type", {}).get("NOR", 0))
        if style == "nand_added":
            return str(result.get("added_by_type", {}).get("NAND", 0))
        if "added_buffers" in result:
            return f"Inserted {result['added_buffers']} BUF gates."
        if "removed_gates" in result:
            return f"Removed {result['removed_gates']} gates."
        if "renamed" in result:
            return "Renamed successfully." if result["renamed"] else "No matching identifier was renamed."
        if "collapsed_pairs" in result:
            return f"Collapsed {result['collapsed_pairs']} back-to-back inverter pairs."
        if "eliminated_gates" in result:
            return f"Eliminated {result['eliminated_gates']} gates."
        if "replaced_gates" in result:
            return f"Replaced {result['replaced_gates']} gates."
        if "merged_gates" in result:
            return f"Merged {result['merged_gates']} gates."
        if "depth" in result:
            return str(result["depth"])
        return _result_sentence(result)

    if tool == "analysis_max_fanout":
        if args.get("answer_style") == "signals_only" or args.get("scope") == "primary_inputs":
            return format_name_list(result.get("signals", []))
        return str(result["max_fanout"])

    if tool == "analysis_primary_io_summary":
        if args.get("answer_style") == "inputs":
            return format_name_list([f"{item['name']}[{item['width']}]" for item in result["inputs"]])
        if args.get("answer_style") == "outputs":
            return format_name_list([f"{item['name']}[{item['width']}]" for item in result["outputs"]])
        return f"inputs={result['input_count']}, outputs={result['output_count']}"

    if tool == "analysis_gate_info":
        return f"{result['gate']}: {result['type']} inputs={result['inputs']} outputs={result['outputs']}"

    if tool == "analysis_gate_on_max_depth_path":
        return "YES" if result["on_max_depth_path"] else "NO"

    if tool == "analysis_list_gates_by_type":
        return f"{result['count']} gates: " + format_name_list([gate['gate'] for gate in result['gates']])

    if tool == "analysis_cone_gate_type_count":
        by_type = result["by_type"]
        style = str(args.get("answer_style", ""))
        if style.endswith("_only"):
            gate = style.removesuffix("_only").upper()
            return str(by_type.get(gate, 0))
        if not by_type:
            return f"The logic cone of {result['target']} contains no gates."
        parts = ", ".join(f"{k}={v}" for k, v in by_type.items())
        return f"The logic cone of {result['target']} has {result['gate_count']} gates ({parts})."

    if tool == "analysis_shared_fanin_cone":
        return f"{result['gate_count']} gates: {format_name_list(result['gates'])}"

    if tool == "analysis_zero_length_paths":
        return f"{result['path_count']} paths"

    if tool == "analysis_register_paths":
        return f"{result['path_count']} register-to-register paths"

    if tool == "analysis_register_path_depth":
        return str(result["max_depth"])

    if tool == "analysis_pi_to_dff_depth":
        return str(result["max_depth"])

    if tool == "analysis_outputs_depth_over":
        return str(result["count"])

    if tool == "analysis_deepest_output":
        if result.get("output") is None:
            return "The design has no primary outputs."
        return f'Output "{result["output"]}" has the greatest logic depth, at {result["depth"]}.'

    if tool == "analysis_largest_fanin_cone_output":
        if result.get("output") is None:
            return "The design has no primary outputs."
        return f'Output "{result["output"]}" has the largest fanin cone, containing {result["gate_count"]} gates.'

    if tool == "analysis_dffs_by_clock":
        return f"{result['count']} DFFs: {format_name_list(result['dffs'])}"

    if tool == "analysis_floating_or_unconnected":
        return str(result["floating_count"])

    if tool == "analysis_cut_or_articulation":
        return "YES" if result["is_cut"] else "NO"

    if tool == "analysis_articulation_points_between":
        points = result.get("articulation_points", [])
        return f"{result.get('count', len(points))} points: {format_name_list(points)}"

    if tool == "analysis_boolean_expression":
        return result["expression"]

    if tool == "analysis_signal_dependency":
        return "YES" if result["depends"] else "NO"

    if tool == "analysis_signal_symmetry":
        return "YES" if result["symmetric"] else "NO"

    if tool == "analysis_signal_constant":
        requested = args.get("value")
        if requested is not None:
            expected = _normalize_logic_value(requested)
            actual = _normalize_logic_value(result.get("value"))
            return "YES" if result["is_constant"] and actual == expected else "NO"
        return "YES" if result["is_constant"] else "NO"

    if tool == "analysis_find_nand_equivalent":
        return "YES" if result["count"] else "NO"

    if tool == "analysis_dff_enable_hold_structures":
        return str(result["count"])

    return _result_sentence(result)


class ContestAgent:
    def __init__(self, config: AgentConfig, *, suppress_stdout: bool = False):
        self.config = config
        self.parser = RequestParser(config)
        self.suppress_stdout = suppress_stdout
        self.case_name: Optional[str] = None
        self.response_id = 0
        self.log_path: Optional[Path] = None
        self.current_testcase_dir: Optional[Path] = None
        self.action_history: List[Dict[str, Any]] = []
        self.last_auto_verify_seconds = 0.0
        self.last_auto_equivalence: Optional[Dict[str, Any]] = None

    def _resolve_netlist_path(self, file_name: str, directory: str) -> Path:
        file_name = _strip_quotes(file_name)
        directory = _strip_quotes(directory).rstrip("/")
        candidates = [
            Path(directory) / file_name,
            self.config.suite_root / directory / file_name,
            Path.cwd() / directory / file_name,
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate

        case_dir = Path(directory).name
        matches = sorted(self.config.suite_root.glob(f"**/testcase/{case_dir}/{file_name}"))
        if matches:
            return matches[0]

        matches = sorted(self.config.suite_root.glob(f"**/{directory}/{file_name}"))
        if matches:
            return matches[0]

        raise FileNotFoundError(f"cannot resolve netlist: file={file_name} directory={directory}")

    def _resolve_output_path(self, output_path: str) -> Path:
        path = Path(_strip_quotes(output_path))
        if path.is_absolute():
            return path
        if self.current_testcase_dir is not None:
            return self.current_testcase_dir / path
        current = self._current_netlist_parent()
        if current is not None:
            return current / path
        return Path.cwd() / path

    def _active_output_dir(self) -> Path:
        return self.current_testcase_dir or self.config.log_dir

    def _answer_artifact_path(self, response_id: int) -> Path:
        base = self.case_name if self.case_name else "response"
        filename = f"{base}_response_{response_id:04d}_answer.txt" if self.case_name else f"response_{response_id:04d}_answer.txt"
        return self._active_output_dir() / filename

    def _maybe_spill_long_answer(self, answer: str, response_id: int) -> str:
        threshold = self.config.answer_artifact_threshold
        if threshold <= 0 or len(answer) <= threshold:
            return answer

        try:
            out_dir = self._active_output_dir()
            out_dir.mkdir(parents=True, exist_ok=True)
            path = self._answer_artifact_path(response_id)
            path.write_text(answer, encoding="utf-8")
            display_path = path.resolve()
        except OSError as exc:
            print(
                f"[warning] failed to write long answer artifact for response {response_id}: {exc}",
                file=sys.stderr,
            )
            return answer

        return f"The answer content is too long for inline output. Full answer written to: {display_path}"

    def _set_log_path_for_current_case(self) -> None:
        if not self.case_name:
            return
        log_dir = self._active_output_dir()
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            new_log_path = log_dir / f"{self.case_name}.log"
            if self.log_path is not None and self.log_path.exists() and self.log_path != new_log_path:
                existing = self.log_path.read_text(errors="replace")
                new_log_path.write_text(existing)
                try:
                    self.log_path.unlink()
                except OSError:
                    pass
            elif not new_log_path.exists():
                new_log_path.write_text("")
            self.log_path = new_log_path
        except OSError:
            self.config.log_dir = Path.cwd()
            self.current_testcase_dir = None
            fallback = self.config.log_dir / f"{self.case_name}.log"
            fallback.write_text("")
            self.log_path = fallback

    def _current_netlist_parent(self) -> Optional[Path]:
        # eda_core keeps the design state internally. Import lazily to avoid
        # exposing that state as part of the public tool contract.
        import eda_core

        session = eda_core._DESIGN_SESSIONS.get("current")  # type: ignore[attr-defined]
        if session is None:
            return None
        return session.netlist_path.parent

    def _remember_action(self, call: ToolCall, result: Dict[str, Any]) -> None:
        if not result.get("ok"):
            return
        tool = call.tool
        if not (tool.startswith("transformation_") or tool.startswith("optimization_")):
            return
        if call.arguments.get("report_last") or result.get("report_only"):
            return
        self.action_history.append(
            {
                "tool": tool,
                "arguments": dict(call.arguments),
                "result": dict(result),
            }
        )

    def _last_transform_stats(self, args: Dict[str, Any]) -> Dict[str, Any]:
        metric = str(args.get("metric", "")).strip()
        source_tool = args.get("tool") or args.get("source_tool")
        source_tool = str(source_tool) if source_tool else None
        gate_type = str(args.get("gate_type", "")).upper()

        for entry in reversed(self.action_history):
            if source_tool and entry["tool"] != source_tool:
                continue
            stats = entry["result"]
            value: Any = None
            found = False
            if metric == "added_by_type":
                added = stats.get("added_by_type", {})
                if gate_type in {"BUF", "BUFFER"} and "added_buffers" in stats:
                    value = stats.get("added_buffers", 0)
                    found = True
                else:
                    value = added.get(gate_type, 0) if isinstance(added, dict) else 0
                    found = isinstance(added, dict)
            elif metric:
                value = stats.get(metric, 0)
                found = metric in stats
            else:
                value = 0
            if found or source_tool:
                return {
                    "ok": True,
                    "tool": "analysis_last_transform_stats",
                    "metric": metric,
                    "gate_type": gate_type or None,
                    "matched_tool": entry["tool"],
                    "found": found,
                    "value": value,
                }

        return {
            "ok": True,
            "tool": "analysis_last_transform_stats",
            "metric": metric,
            "gate_type": gate_type or None,
            "matched_tool": None,
            "found": False,
            "value": 0,
        }

    def _maybe_auto_verify_transform(self, call: ToolCall, result: Dict[str, Any]) -> Dict[str, Any]:
        self.last_auto_verify_seconds = 0.0
        self.last_auto_equivalence = None
        if not self.config.auto_verify_transforms or not result.get("ok"):
            return result
        tool = call.tool
        if not (tool.startswith("transformation_") or tool.startswith("optimization_")):
            return result
        if call.arguments.get("report_only") or result.get("report_only") or call.arguments.get("report_last"):
            return result
        if result.get("status") == "kept_original":
            return result
        t0 = time.perf_counter()
        try:
            eq = verification_design_equivalence("pre_transform", design_id="current")
        except Exception as exc:
            self.last_auto_verify_seconds = time.perf_counter() - t0
            result["auto_equivalence"] = {
                "reference": "pre_transform",
                "status": "error",
                "equivalent": None,
                "message": f"{type(exc).__name__}: {exc}",
            }
            self.last_auto_equivalence = result["auto_equivalence"]
            return result
        self.last_auto_verify_seconds = time.perf_counter() - t0
        result["auto_equivalence"] = {
            "reference": "pre_transform",
            "status": eq.get("status"),
            "equivalent": eq.get("equivalent"),
            "method": eq.get("method"),
            "message": eq.get("message"),
        }
        self.last_auto_equivalence = result["auto_equivalence"]
        return result

    def process_request(self, request: str) -> str:
        request = request.strip()
        self.last_tool = None
        self.last_auto_verify_seconds = 0.0
        self.last_auto_equivalence = None
        call = self.parser.parse(request)
        self.last_tool = call.tool
        result = self.execute(call)
        result = self._maybe_auto_verify_transform(call, result)
        self._remember_action(call, result)
        answer = render_answer(call, result, request)
        return self.emit_response(answer)

    def execute(self, call: ToolCall) -> Dict[str, Any]:
        args = dict(call.arguments)
        tool = call.tool

        if tool == "begin_testcase":
            case_name = str(args["case_name"])
            self.case_name = case_name
            self.response_id = 0
            self.current_testcase_dir = None
            self.action_history = []
            self._set_log_path_for_current_case()
            return {"ok": True, "tool": tool, "case_name": case_name, "log_path": str(self.log_path)}

        if tool == "design_load":
            netlist = self._resolve_netlist_path(str(args["file_name"]), str(args["directory"]))
            self.current_testcase_dir = netlist.parent
            self._set_log_path_for_current_case()
            miter_dir = self.config.miter_dir
            if miter_dir is None and self.case_name:
                miter_dir = self._active_output_dir() / "yosys_miters" / self.case_name
            return design_load(
                netlist,
                design_id="current",
                miter_dir=miter_dir,
                yosys_timeout=self.config.yosys_timeout,
            )

        if tool == "design_write":
            return design_write(self._resolve_output_path(str(args["output_path"])), design_id="current")

        if tool == "analysis_last_transform_stats":
            return self._last_transform_stats(args)

        if tool == "analysis_count_gates":
            return analysis_count_gates(design_id="current")

        if tool == "analysis_fanin_cone_size":
            return analysis_fanin_cone_size(str(args["output"]), design_id="current")

        if tool == "analysis_transitive_fanin_cone":
            return analysis_transitive_fanin_cone(str(args["output"]), design_id="current")

        if tool == "analysis_direct_fanout":
            return analysis_direct_fanout(str(args["signal"]), design_id="current")

        if tool == "analysis_transitive_fanout_cone":
            return analysis_transitive_fanout_cone(str(args["input"]), design_id="current")

        if tool == "analysis_path_exists":
            return analysis_path_exists(str(args["source"]), str(args["target"]), design_id="current")

        if tool == "analysis_path_exists_avoiding":
            return analysis_path_exists_avoiding(
                str(args["source"]),
                str(args["target"]),
                str(args["avoid"]),
                design_id="current",
            )

        if tool == "analysis_enumerate_paths":
            path_limit = int(args.get("path_limit", self.config.path_limit))
            return analysis_enumerate_paths(
                str(args["source"]),
                str(args["target"]),
                design_id="current",
                path_limit=path_limit,
            )

        if tool == "analysis_max_logic_depth":
            source = args.get("source")
            target = args.get("target")
            return analysis_max_logic_depth(
                str(target) if target not in (None, "") else None,
                source=str(source) if source not in (None, "") else None,
                design_id="current",
            )

        if tool == "analysis_gate_successors":
            return analysis_gate_successors(str(args["gate"]), design_id="current")

        if tool == "verification_functional_equivalence":
            return verification_functional_equivalence(str(args["left"]), str(args["right"]), design_id="current")

        if tool == "transformation_limit_fanout":
            return transformation_limit_fanout(
                max_fanout=int(args.get("max_fanout", 4)),
                signal=str(args["signal"]) if args.get("signal") else None,
                dedicated=bool(args.get("dedicated", False)),
                design_id="current",
            )

        if tool == "transformation_insert_dedicated_buffers":
            return transformation_insert_dedicated_buffers(str(args["signal"]), design_id="current")

        if tool == "transformation_remove_dangling_logic":
            return transformation_remove_dangling_logic(design_id="current")

        if tool == "transformation_rename_identifier":
            return transformation_rename_identifier(
                str(args["old_name"]),
                str(args["new_name"]),
                kind=str(args.get("kind", "auto")),
                design_id="current",
            )

        if tool == "transformation_reconnect_gate_input":
            return transformation_reconnect_gate_input(
                str(args["gate"]), str(args["pin"]), str(args["signal"]), design_id="current"
            )

        if tool == "transformation_collapse_back_to_back_inverters":
            return transformation_collapse_back_to_back_inverters(design_id="current")

        if tool == "transformation_constant_propagation":
            return transformation_constant_propagation(
                gate_type=str(args["gate_type"]) if args.get("gate_type") else None,
                constant=str(args["constant"]) if args.get("constant") else None,
                report_only=bool(args.get("report_only", False)),
                design_id="current",
            )

        if tool == "transformation_replace_gate_library":
            return transformation_replace_gate_library(
                scope=str(args.get("scope", "design")),
                target=str(args["target"]) if args.get("target") else None,
                from_gate=str(args["from_gate"]) if args.get("from_gate") else None,
                to_library=str(args.get("to_library", "nand_not")),
                design_id="current",
            )

        if tool == "optimization_reduce_depth":
            return optimization_reduce_depth(
                target=str(args["target"]) if args.get("target") else None,
                max_depth=int(args["max_depth"]) if args.get("max_depth") is not None else None,
                scope=str(args.get("scope", "design")),
                design_id="current",
            )

        if tool == "optimization_merge_equivalent_or_duplicate_gates":
            if args.get("report_last"):
                import eda_core
                session = eda_core._DESIGN_SESSIONS.get("current")  # type: ignore[attr-defined]
                if session is None:
                    raise KeyError("design is not loaded: current")
                last = dict(session.last_stats or {})
                if "merged_gates" in last:
                    # 报告“上一次合并”的结果，而不是在已合并的网表上重跑（重跑会得到 0）。
                    last["mode"] = str(args.get("mode", last.get("mode", "structural")))
                    return {"ok": True, "tool": tool, "design_id": "current", **last}
                # 没有可报告的历史合并结果 → 实跑一次。
            return optimization_merge_equivalent_or_duplicate_gates(mode=str(args.get("mode", "structural")), design_id="current")

        if tool == "analysis_max_fanout":
            return analysis_max_fanout(
                str(args["signal"]) if args.get("signal") else None,
                scope=str(args["scope"]) if args.get("scope") else None,
                design_id="current",
            )

        if tool == "analysis_primary_io_summary":
            return analysis_primary_io_summary(design_id="current")

        if tool == "analysis_gate_info":
            return analysis_gate_info(str(args["gate"]), design_id="current")

        if tool == "analysis_gate_on_max_depth_path":
            return analysis_gate_on_max_depth_path(str(args["gate"]), design_id="current")

        if tool == "analysis_list_gates_by_type":
            return analysis_list_gates_by_type(str(args["gate_type"]), design_id="current")

        if tool == "analysis_cone_gate_type_count":
            return analysis_cone_gate_type_count(str(args["target"]), design_id="current")

        if tool == "analysis_shared_fanin_cone":
            return analysis_shared_fanin_cone(str(args["left"]), str(args["right"]), design_id="current")

        if tool == "analysis_zero_length_paths":
            return analysis_zero_length_paths(design_id="current")

        if tool == "analysis_register_paths":
            return analysis_register_paths(design_id="current")

        if tool == "analysis_register_path_depth":
            return analysis_register_path_depth(design_id="current")

        if tool == "analysis_pi_to_dff_depth":
            return analysis_pi_to_dff_depth(design_id="current")

        if tool == "analysis_outputs_depth_over":
            return analysis_outputs_depth_over(int(args["threshold"]), design_id="current")

        if tool == "analysis_deepest_output":
            return analysis_deepest_output(design_id="current")

        if tool == "analysis_largest_fanin_cone_output":
            return analysis_largest_fanin_cone_output(design_id="current")

        if tool == "analysis_dffs_by_clock":
            return analysis_dffs_by_clock(str(args["clock"]), design_id="current")

        if tool == "analysis_floating_or_unconnected":
            return analysis_floating_or_unconnected(design_id="current")

        if tool == "analysis_cut_or_articulation":
            return analysis_cut_or_articulation(
                str(args["signal"]),
                source=str(args["source"]) if args.get("source") else None,
                target=str(args["target"]) if args.get("target") else None,
                scope=str(args["scope"]) if args.get("scope") else None,
                design_id="current",
            )

        if tool == "analysis_articulation_points_between":
            return analysis_articulation_points_between(str(args["source"]), str(args["target"]), design_id="current")

        if tool == "analysis_boolean_expression":
            return analysis_boolean_expression(str(args["target"]), design_id="current")

        if tool == "analysis_signal_dependency":
            return analysis_signal_dependency(str(args["output"]), str(args["input"]), design_id="current")

        if tool == "analysis_signal_symmetry":
            return analysis_signal_symmetry(
                str(args["target"]), str(args["input_a"]), str(args["input_b"]), design_id="current"
            )

        if tool == "analysis_signal_constant":
            value = args.get("value")
            return analysis_signal_constant(
                str(args["signal"]),
                str(value) if value is not None else None,
                design_id="current",
            )

        if tool == "analysis_find_nand_equivalent":
            return analysis_find_nand_equivalent(str(args["target"]), design_id="current")

        if tool == "analysis_dff_enable_hold_structures":
            return analysis_dff_enable_hold_structures(design_id="current")

        if tool == "verification_design_equivalence":
            return verification_design_equivalence(str(args.get("reference", "original")), design_id="current")

        raise ValueError(f"unsupported tool: {tool}")

    def emit_response(self, answer: str) -> str:
        self.response_id += 1
        emitted_answer = self._maybe_spill_long_answer(answer, self.response_id)
        payload = f"#RESPONSE {self.response_id}\n{emitted_answer}\n#END {self.response_id}\n"
        if not self.suppress_stdout:
            print(payload, end="", flush=True)
        if self.log_path is not None:
            with self.log_path.open("a") as log:
                log.write(payload)
        return emitted_answer


def find_case_prompt(root: Path, case_num: int) -> Path:
    case = f"test{case_num:02d}"
    matches = sorted(root.glob(f"**/testcase/{case}/prompt.txt"))
    if not matches:
        raise FileNotFoundError(f"cannot find prompt for {case} under {root}")
    return matches[0]

PROMPT_TIMEOUT_SEC = 600  # 单条 prompt 墙钟上限（秒）；若误杀大设计的合法优化就调大

# spec 3.1: basic op (load/write/init) 限 60s，其余分析/变换/优化限 300s。
# 诊断阶段 SIGALRM 仍用上面的 600s 宽口径，好让慢 prompt 跑完拿到真实耗时；
# 是否超赛题红线在 timing.csv 里按下面的阈值标记。
BASIC_OPS = {"begin_testcase", "design_load", "design_write"}


def _spec_limit(tool: Optional[str]) -> int:
    return 60 if tool in BASIC_OPS else 300


class _PromptTimeout(Exception):
    pass


@contextlib.contextmanager
def _time_limit(seconds: int):
    """超过 seconds 秒就抛 _PromptTimeout。基于 SIGALRM，仅 Unix/WSL 主线程有效。"""
    if seconds and hasattr(signal, "SIGALRM"):
        def _handler(signum, frame):
            raise _PromptTimeout(f"exceeded {seconds}s")
        old = signal.signal(signal.SIGALRM, _handler)
        signal.alarm(seconds)
        try:
            yield
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
    else:
        yield

def replay_suite(config: AgentConfig, start: int, end: int, *, suppress_stdout: bool) -> None:
    config.log_dir.mkdir(parents=True, exist_ok=True)
    timing_path = config.log_dir / "timing.csv"
    answers_path = config.log_dir / "answers.csv"
    with timing_path.open("w", newline="") as tf:
        writer = csv.writer(tf)
        writer.writerow([
            "case",
            "prompt_idx",
            "seconds",
            "tool",
            "spec_limit",
            "over_spec",
            "status",
            "auto_verify_seconds",
            "auto_verify_status",
            "auto_verify_equivalent",
            "wall_seconds",
            "prompt",
        ])
    with answers_path.open("w", newline="") as af:
        writer = csv.writer(af)
        writer.writerow(["case", "prompt_idx", "tool", "status", "answer", "prompt"])

    for case_num in range(start, end + 1):
        agent = ContestAgent(config, suppress_stdout=suppress_stdout)
        prompt = find_case_prompt(config.suite_root, case_num)
        idx = 0
        for line in prompt.read_text(errors="replace").splitlines():
            if not line.strip():
                continue
            idx += 1
            t0 = time.perf_counter()
            status = "ok"
            answer = ""
            try:
                with _time_limit(PROMPT_TIMEOUT_SEC):
                    answer = agent.process_request(line)
            except _PromptTimeout as exc:
                status = "timeout"
                answer = f"TIMEOUT: {exc}"
                print(f"[timeout] test{case_num:02d}: prompt #{idx} ({exc}): {line[:80]}", file=sys.stderr)
            except Exception as exc:
                status = f"skip:{type(exc).__name__}"
                answer = f"{type(exc).__name__}: {exc}"
                print(f"[skip] test{case_num:02d}: prompt #{idx}: {type(exc).__name__}: {exc}", file=sys.stderr)
            wall_elapsed = time.perf_counter() - t0
            auto_elapsed = getattr(agent, "last_auto_verify_seconds", 0.0)
            elapsed = max(0.0, wall_elapsed - auto_elapsed)
            tool = getattr(agent, "last_tool", None) or "parse_error"
            limit = _spec_limit(tool)
            over = "YES" if elapsed > limit else ""
            auto_eq = getattr(agent, "last_auto_equivalence", None) or {}
            with timing_path.open("a", newline="") as tf:
                writer = csv.writer(tf)
                writer.writerow([
                    f"test{case_num:02d}",
                    idx,
                    f"{elapsed:.2f}",
                    tool,
                    limit,
                    over,
                    status,
                    f"{auto_elapsed:.2f}" if auto_elapsed else "",
                    auto_eq.get("status", ""),
                    auto_eq.get("equivalent", ""),
                    f"{wall_elapsed:.2f}",
                    line,
                ])
            with answers_path.open("a", newline="") as af:
                writer = csv.writer(af)
                writer.writerow([f"test{case_num:02d}", idx, tool, status, answer, line])
            if suppress_stdout:
                auto_note = ""
                if auto_elapsed:
                    auto_note = f" auto_verify={auto_eq.get('status', '')}/{auto_eq.get('equivalent', '')} {auto_elapsed:.2f}s"
                print(
                    f"progress test{case_num:02d} #{idx}: {tool} {status} {elapsed:.2f}s{auto_note}",
                    flush=True,
                )
        if suppress_stdout:
            print(f"replayed test{case_num:02d}: log={agent.log_path}", flush=True)
    print(f"[timing] per-prompt timing written to {timing_path}", file=sys.stderr)
    print(f"[answers] per-prompt answers written to {answers_path}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="ICCAD Problem A contest agent.")
    parser.add_argument("-config", "--config", type=Path, help="contest LLM/config file")
    parser.add_argument("--parser", choices=["hybrid", "llm", "regex"], help="override request parser mode")
    parser.add_argument("--suite-root", type=Path, help="project root used to resolve testcase paths")
    parser.add_argument("--log-dir", type=Path, help="directory for <case>.log files")
    parser.add_argument("--path-limit", type=int, help="path enumeration limit")
    parser.add_argument(
        "--answer-artifact-threshold",
        type=int,
        help="maximum inline answer characters before writing the full answer to a sidecar file; <=0 disables",
    )
    parser.add_argument("--replay-suite", action="store_true", help="development helper: replay prompt.txt files")
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--end", type=int, default=20)
    parser.add_argument("--suppress-stdout", action="store_true", help="development helper: do not print responses")
    args = parser.parse_args()

    config = load_agent_config(args.config)
    if args.parser:
        config.parser = args.parser
    if args.suite_root:
        config.suite_root = args.suite_root
    if args.log_dir:
        config.log_dir = args.log_dir
    if args.path_limit is not None:
        config.path_limit = args.path_limit
    if args.answer_artifact_threshold is not None:
        config.answer_artifact_threshold = args.answer_artifact_threshold

    if args.replay_suite:
        replay_suite(config, args.start, args.end, suppress_stdout=args.suppress_stdout)
        return

    agent = ContestAgent(config, suppress_stdout=args.suppress_stdout)
    for line in sys.stdin:
        if line.strip():
            try:
                agent.process_request(line)
            except Exception as exc:  # Contest guard: one bad prompt should not stop later prompts.
                try:
                    agent.emit_response(
                        f"Unable to complete this request ({type(exc).__name__}: {exc})."
                    )
                except Exception:
                    pass


if __name__ == "__main__":
    main()
