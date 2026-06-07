# EDA Tool Contract

This document defines the MCP-ready tool surface for the current analysis backend. The Python implementations live in `src/eda_core.py` and return JSON-friendly dictionaries.

## State Model

- `design_load` parses a gate-level Verilog netlist and stores it under a server-side `design_id`.
- All other tools default to `design_id="current"`.
- Net and signal names must be passed exactly, including bus bits such as `n0[1]`.
- Path/depth analysis is combinational; DFFs are treated as boundaries.

## Tools

### `design_load`

**Category:** `design`

**Purpose:** Load a flattened gate-level Verilog netlist into the server-side current design state.

**Use When:** Use first for every testcase, before analysis or verification tools.

**Parameters:**

```json
{
  "type": "object",
  "properties": {
    "netlist_path": {
      "type": "string",
      "description": "Path to the Verilog netlist file."
    },
    "design_id": {
      "type": "string",
      "default": "current",
      "description": "Server-side design handle."
    },
    "miter_dir": {
      "type": "string",
      "description": "Optional directory for generated Yosys miter files."
    },
    "yosys_timeout": {
      "type": "integer",
      "default": 240,
      "description": "Timeout in seconds for each Yosys SAT query."
    }
  },
  "required": [
    "netlist_path"
  ]
}
```

**Returns:**

```json
{
  "ok": "boolean",
  "design_id": "string",
  "module": "string",
  "signal_count": "integer",
  "net_count": "integer",
  "cell_count": "integer",
  "port_order": "array[string]"
}
```

### `design_write`

**Category:** `design`

**Purpose:** Write the current design to a Verilog file.

**Use When:** Use when the request asks to write/output the current design.

**Parameters:**

```json
{
  "type": "object",
  "properties": {
    "output_path": {
      "type": "string",
      "description": "Path where the output netlist should be written."
    },
    "design_id": {
      "type": "string",
      "default": "current"
    }
  },
  "required": [
    "output_path"
  ]
}
```

**Returns:**

```json
{
  "ok": "boolean",
  "output_path": "string",
  "source_path": "string",
  "bytes": "integer"
}
```

**Notes:** For the current analysis-only backend this writes an unchanged copy of the loaded design.

### `analysis_count_gates`

**Category:** `analysis`

**Purpose:** Count all cells and report totals broken down by gate type.

**Use When:** Use for requests asking total gate count or gate count by type.

**Parameters:**

```json
{
  "type": "object",
  "properties": {
    "design_id": {
      "type": "string",
      "default": "current"
    }
  }
}
```

**Returns:**

```json
{
  "total": "integer",
  "by_type": "object"
}
```

### `analysis_fanin_cone_size`

**Category:** `analysis`

**Purpose:** Return the number of gates in the transitive fanin cone of an output/signal.

**Use When:** Use when the request asks how many gates are in a fanin cone.

**Parameters:**

```json
{
  "type": "object",
  "properties": {
    "output": {
      "type": "string",
      "description": "Target net or signal, e.g. n15 or n31[0]."
    },
    "design_id": {
      "type": "string",
      "default": "current"
    }
  },
  "required": [
    "output"
  ]
}
```

**Returns:**

```json
{
  "output": "string",
  "gate_count": "integer"
}
```

### `analysis_transitive_fanin_cone`

**Category:** `analysis`

**Purpose:** List all gates in the transitive fanin cone of an output/signal.

**Use When:** Use when the request asks to compute or list the transitive fanin cone.

**Parameters:**

```json
{
  "type": "object",
  "properties": {
    "output": {
      "type": "string"
    },
    "design_id": {
      "type": "string",
      "default": "current"
    }
  },
  "required": [
    "output"
  ]
}
```

**Returns:**

```json
{
  "output": "string",
  "gate_count": "integer",
  "gates": "array[string]"
}
```

### `analysis_direct_fanout`

**Category:** `analysis`

**Purpose:** List gates directly driven by a primary input, signal, or net.

**Use When:** Use for direct fanout questions.

**Parameters:**

```json
{
  "type": "object",
  "properties": {
    "signal": {
      "type": "string"
    },
    "design_id": {
      "type": "string",
      "default": "current"
    }
  },
  "required": [
    "signal"
  ]
}
```

**Returns:**

```json
{
  "signal": "string",
  "gate_count": "integer",
  "gates": "array[string]"
}
```

### `analysis_transitive_fanout_cone`

**Category:** `analysis`

**Purpose:** List all gates in the transitive fanout cone of an input/signal.

**Use When:** Use when the request asks to compute or list the transitive fanout cone.

**Parameters:**

```json
{
  "type": "object",
  "properties": {
    "input": {
      "type": "string"
    },
    "design_id": {
      "type": "string",
      "default": "current"
    }
  },
  "required": [
    "input"
  ]
}
```

**Returns:**

```json
{
  "input": "string",
  "gate_count": "integer",
  "gates": "array[string]"
}
```

### `analysis_path_exists`

**Category:** `analysis`

**Purpose:** Check whether a combinational path exists from source to target.

**Use When:** Use for simple path-existence questions.

**Parameters:**

```json
{
  "type": "object",
  "properties": {
    "source": {
      "type": "string"
    },
    "target": {
      "type": "string"
    },
    "design_id": {
      "type": "string",
      "default": "current"
    }
  },
  "required": [
    "source",
    "target"
  ]
}
```

**Returns:**

```json
{
  "source": "string",
  "target": "string",
  "exists": "boolean"
}
```

### `analysis_path_exists_avoiding`

**Category:** `analysis`

**Purpose:** Check whether a combinational path exists while avoiding a specified net or gate.

**Use When:** Use for requests saying path exists that does not traverse/pass through/while avoiding a node.

**Parameters:**

```json
{
  "type": "object",
  "properties": {
    "source": {
      "type": "string"
    },
    "target": {
      "type": "string"
    },
    "avoid": {
      "type": "string",
      "description": "Net/signal bit or gate to remove from the graph."
    },
    "design_id": {
      "type": "string",
      "default": "current"
    }
  },
  "required": [
    "source",
    "target",
    "avoid"
  ]
}
```

**Returns:**

```json
{
  "source": "string",
  "target": "string",
  "avoid": "string",
  "exists": "boolean"
}
```

**All-paths note:** For 'Does every path from A to B pass through C?', call this tool with `avoid=C`. A returned `exists=false` means the answer is YES; `exists=true` means NO.

### `analysis_enumerate_paths`

**Category:** `analysis`

**Purpose:** Enumerate combinational paths from source to target, subject to path_limit.

**Use When:** Use when the request asks to list every path or provide complete path enumeration.

**Parameters:**

```json
{
  "type": "object",
  "properties": {
    "source": {
      "type": "string"
    },
    "target": {
      "type": "string"
    },
    "path_limit": {
      "type": "integer",
      "default": 1000000
    },
    "design_id": {
      "type": "string",
      "default": "current"
    }
  },
  "required": [
    "source",
    "target"
  ]
}
```

**Returns:**

```json
{
  "source": "string",
  "target": "string",
  "path_count": "integer",
  "complete": "boolean",
  "paths": "array[array[string]]"
}
```

**Notes:** For official test01-test20, path_limit=1000000 is enough to avoid truncation.

### `analysis_max_logic_depth`

**Category:** `analysis`

**Purpose:** Compute maximum combinational logic depth.

**Use When:** Use for maximum logic depth, longest combinational path depth, or critical path depth questions.

**Parameters:**

```json
{
  "type": "object",
  "properties": {
    "target": {
      "type": "string"
    },
    "source": {
      "type": "string",
      "description": "Optional. If omitted, computes max depth of the target fanin cone."
    },
    "design_id": {
      "type": "string",
      "default": "current"
    }
  },
  "required": [
    "target"
  ]
}
```

**Returns:**

```json
{
  "source": "string|null",
  "target": "string",
  "mode": "string",
  "path_exists": "boolean",
  "depth": "integer|null"
}
```

### `analysis_gate_successors`

**Category:** `analysis`

**Purpose:** List immediate successor gates driven by a gate output.

**Use When:** Use for questions asking gates driven by a gate or immediate successors of a gate.

**Parameters:**

```json
{
  "type": "object",
  "properties": {
    "gate": {
      "type": "string"
    },
    "design_id": {
      "type": "string",
      "default": "current"
    }
  },
  "required": [
    "gate"
  ]
}
```

**Returns:**

```json
{
  "gate": "string",
  "successor_count": "integer",
  "successors": "array[string]"
}
```

### `verification_functional_equivalence`

**Category:** `verification`

**Purpose:** Formally check whether two signals are functionally equivalent using Yosys SAT/miter.

**Use When:** Use for requests asking whether two signals are functionally equivalent or produce identical logic values for all inputs.

**Parameters:**

```json
{
  "type": "object",
  "properties": {
    "left": {
      "type": "string"
    },
    "right": {
      "type": "string"
    },
    "design_id": {
      "type": "string",
      "default": "current"
    }
  },
  "required": [
    "left",
    "right"
  ]
}
```

**Returns:**

```json
{
  "left": "string",
  "right": "string",
  "equivalent": "boolean|null",
  "status": "string",
  "method": "string",
  "support_size": "integer",
  "support": "array[string]",
  "message": "string"
}
```

## Recommended Mapping For Test01-Test20

- Gate count questions -> `analysis_count_gates`
- Fanin cone size questions -> `analysis_fanin_cone_size`
- Direct fanout questions -> `analysis_direct_fanout`
- Path existence questions -> `analysis_path_exists` or `analysis_path_exists_avoiding`
- Complete path listing questions -> `analysis_enumerate_paths` with `path_limit=1000000`
- Depth / longest path depth / critical path depth questions -> `analysis_max_logic_depth`
- Gate successor questions -> `analysis_gate_successors`
- Transitive cone questions -> `analysis_transitive_fanin_cone` or `analysis_transitive_fanout_cone`
- Functional equivalence questions -> `verification_functional_equivalence`
- Write-output requests -> `design_write`


## Transformation / Optimization Extensions

The following tools extend the original analysis backend for official test21-test40. Their Python wrappers live in `src/eda_core.py`; isolated implementation details live in `src/eda_transform.py` under Transformation / Optimization sections.

### Transformation tools

- `transformation_limit_fanout(max_fanout=4, signal=null, dedicated=false)`: insert BUF gates so selected net(s) do not exceed the requested fanout.
- `transformation_insert_dedicated_buffers(signal)`: insert one BUF per current load of `signal`.
- `transformation_remove_dangling_logic()`: remove gates/wires that do not contribute to primary outputs or sequential boundaries.
- `transformation_rename_identifier(old_name, new_name, kind="auto")`: rename a gate or signal/net and update references.
- `transformation_reconnect_gate_input(gate, pin, signal)`: reconnect one gate input pin.
- `transformation_collapse_back_to_back_inverters()`: collapse NOT->NOT chains into direct connections.
- `transformation_constant_propagation(gate_type=null, constant=null)`: report and simplify gates with constant inputs.
- `transformation_replace_gate_library(scope="design", target=null, from_gate=null, to_library="nand_not")`: replace gates with equivalent templates using libraries such as `nand_not`, `nor_not`, `and_not`, or `and_or_not`. The `nor_not` implementation uses direct NOR/NOT templates and does not recurse through itself.

### Optimization tools

- `optimization_reduce_depth(target=null, max_depth=null, scope="design")`: safe hook for depth optimization. It reports measured depth and keeps the original when safe ABC/Yosys merge is not proven.
- `optimization_merge_equivalent_or_duplicate_gates(mode="structural")`: merge structural duplicate gates when safe.

### Extended analysis tools

- `analysis_max_fanout(signal=null)`: report maximum fanout globally or for a signal.
- `analysis_primary_io_summary()`: report primary input/output counts and bit widths.
- `analysis_gate_info(gate)`: report gate type and pin connections.
- `analysis_gate_on_max_depth_path(gate)`: report whether a gate lies on any maximum-depth combinational path.
- `analysis_list_gates_by_type(gate_type)`: list gates of a type with pin info.
- `analysis_cone_gate_type_count(target)`: count gate types in a fanin cone.
- `analysis_shared_fanin_cone(left, right)`: list shared gates in two fanin cones.
- `analysis_zero_length_paths()`: list direct PI-to-PO wire connections.
- `analysis_register_paths()`: list register-to-register combinational paths.
- `analysis_pi_to_dff_depth()`: report maximum PI-to-DFF-D combinational depth.
- `analysis_outputs_depth_over(threshold)`: count outputs with fanin depth greater than a threshold.
- `analysis_deepest_output()`: report output bit with deepest cone.
- `analysis_largest_fanin_cone_output()`: report output with largest fanin cone.
- `analysis_dffs_by_clock(clock)`: list DFFs driven by a clock net.
- `analysis_floating_or_unconnected()`: report floating input/unconnected-output style issues.
- `analysis_cut_or_articulation(signal)`: check whether a signal is an articulation/cut point in the combinational graph.
- `analysis_boolean_expression(target)`: derive a recursive Boolean expression for a target net.
- `analysis_signal_dependency(output, input)`: check structural dependence of output on input.
- `analysis_signal_symmetry(target, input_a, input_b)`: symmetry query hook for future SAT/BDD implementation.
- `analysis_signal_constant(signal)`: report whether a signal is structurally constant.
- `analysis_find_nand_equivalent(target)`: find existing NAND gate pairs producing a target.
- `analysis_dff_enable_hold_structures()`: report DFFs whose D cone has simple AND/OR enable/hold-like structures.

### Verification tools

- `verification_design_equivalence(reference="original")`: compare current netlist with the original or pre-transform snapshot. Exact serializer match returns equivalent; otherwise the Yosys design-level proof hook reports unknown until fully enabled.
