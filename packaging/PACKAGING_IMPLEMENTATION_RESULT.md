# ICCAD 打包实施结果

## 已完成事项

已将打包方案落地到当前仓库，并将队伍编号更新为 `1078`。

alpha 阶段最终可执行文件名为：

```bash
cada1078_alpha
```

评测调用格式为：

```bash
./cada1078_alpha -config <config_file_path>
```

---

## 已修改源码

### `src/contest_agent.py`

新增/调整了资源路径查找逻辑，使程序在 PyInstaller 冻结后仍能找到：

```text
mcp_tools_spec.json
```

资源查找兼容：

1. 源码运行；
2. 非仓库目录运行；
3. PyInstaller 冻结运行。

### `src/eda_abc.py`

新增/调整了 ABC 资源路径查找逻辑，使程序在 PyInstaller 冻结后仍能找到：

```text
abc_resources/my.genlib
abc_resources/abc.rc
```

同时保留环境变量覆盖能力：

```text
GENLIB_PATH
ABC_RC_PATH
ABC_BIN
ABC_CEC_BIN
```

---

## 新增 packaging 文件

新增目录：

```text
packaging/
```

其中包含：

```text
packaging/
├── README.md
├── PACKAGING_PLAN.md
├── PACKAGING_IMPLEMENTATION_RESULT.md
├── build_pyinstaller.sh
├── cada1078_alpha
├── cada1078_alpha.spec
├── cada1078_alpha_wrapper.py
├── requirements-lock.txt
└── docker/
    └── Dockerfile.pyinstaller
```

---

## 关键文件说明

### `packaging/cada1078_alpha_wrapper.py`

PyInstaller 的 Python 入口包装器。

职责：

- 找到 PyInstaller 资源根目录；
- 将 `src` 加入 `sys.path`；
- 设置 `GENLIB_PATH`；
- 设置 `ABC_RC_PATH`；
- 设置 `ABC_BIN`；
- 设置 `ABC_CEC_BIN`；
- 调用原始入口 `contest_agent.main()`。

---

### `packaging/cada1078_alpha.spec`

PyInstaller spec 文件。

会生成：

```text
dist/cada1078_alpha_dist/cada1078_alpha_bin
```

并打包：

```text
src/
mcp_tools_spec.json
abc_resources/
configs/contest.yml
```

同时声明 hidden imports：

```text
yaml
networkx
pyverilog
pyverilog.vparser
pyverilog.vparser.parser
pyverilog.vparser.ast
ply
ply.lex
ply.yacc
```

---

### `packaging/cada1078_alpha`

最终提交目录中的顶层 shell wrapper。

职责：

- 设置 `PATH`，优先使用提交包自带 `bin/`；
- 设置 ABC 相关环境变量；
- 执行真实 PyInstaller 程序：

```bash
app/cada1078_alpha_dist/cada1078_alpha_bin
```

---

### `packaging/build_pyinstaller.sh`

构建脚本。

执行后生成：

```text
submission/
├── cada1078_alpha
├── app/
│   └── cada1078_alpha_dist/
├── bin/
├── configs/
├── abc_resources/
└── mcp_tools_spec.json
```

---

### `packaging/docker/Dockerfile.pyinstaller`

Docker 构建环境。

安装：

```text
python3
pip
pyinstaller
pyverilog
networkx
PyYAML
iverilog
yosys
berkeley-abc
```

并默认运行：

```bash
bash packaging/build_pyinstaller.sh
```

---

## 构建命令

### Docker 构建

在项目根目录执行：

```bash
docker build \
  -f packaging/docker/Dockerfile.pyinstaller \
  -t iccad-cada1078-builder \
  .
```

然后：

```bash
mkdir -p dist_out

docker run --rm \
  -v "$PWD/dist_out:/out" \
  iccad-cada1078-builder
```

构建完成后产物位于：

```text
dist_out/submission/
```

最终评测入口为：

```text
dist_out/submission/cada1078_alpha
```

---

## 本地构建命令

如果本机已有 PyInstaller、Python 依赖和 EDA 工具，可执行：

```bash
python3 -m pip install -r packaging/requirements-lock.txt
bash packaging/build_pyinstaller.sh
```

生成：

```text
submission/cada1078_alpha
```

---

## 已执行验证

已执行 Python 编译检查：

```bash
python3 -m py_compile /home/xjw/ICCAD/src/*.py /home/xjw/ICCAD/packaging/cada1078_alpha_wrapper.py
```

结果：通过。

已执行非仓库目录资源查找测试：

```bash
cd /tmp && PYTHONPATH=/home/xjw/ICCAD/src python3 - <<'PY'
from pathlib import Path
from contest_agent import load_tool_contract
contract = load_tool_contract(Path('/definitely/not/iccad'))
print('tools', len(contract.get('tools', [])))
import eda_abc
print('genlib', bool(eda_abc.GENLIB_PATH), eda_abc.GENLIB_PATH)
print('abc_rc', bool(eda_abc.ABC_RC_PATH), eda_abc.ABC_RC_PATH)
PY
```

输出确认：

```text
tools 48
genlib True /home/xjw/ICCAD/abc_resources/my.genlib
abc_rc True /home/xjw/ICCAD/abc_resources/abc.rc
```

说明核心资源查找正常。

---

## 当前环境限制

当前环境中未检测到：

```text
pyinstaller
docker
```

因此已经完成打包代码和构建脚本改造，但未在当前机器实际生成最终二进制。

需要在具备 Docker 或 PyInstaller 的环境中执行构建命令，生成最终：

```text
submission/cada1078_alpha
```

---

## 最终检查清单

提交前应确认：

1. 文件名严格为 `cada1078_alpha`；
2. 文件具有可执行权限；
3. 支持：

   ```bash
   ./cada1078_alpha -config <config_file_path>
   ```

4. 不依赖 `/home/xjw/ICCAD` 源码路径；
5. 不依赖联网安装 packages；
6. 不包含 API key；
7. `mcp_tools_spec.json` 可找到；
8. `abc_resources/my.genlib` 可找到；
9. `abc_resources/abc.rc` 可找到；
10. `yosys`、`yosys-abc`、`abc`、`iverilog` 可找到；
11. stdin/stdout 协议能输出 `#RESPONSE <id>` 和 `#END <id>`。
