import sys
from pathlib import Path
sys.path.insert(0, "src")
from pyv_extractor import parse_verilog_to_ir
from eda_transform import design_max_logic_depth, _max_depth
for p in sys.argv[1:]:
    ir = parse_verilog_to_ir(p); ir.rebuild_indices()
    print(f"{p}\n    PO-only(旧) = {_max_depth(ir):>4}     四帧含reg2reg(新) = {design_max_logic_depth(ir):>4}")
