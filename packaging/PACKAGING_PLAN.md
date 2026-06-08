# ICCAD Contest 可执行文件打包方案

## 目标

将当前 Python 实现转换为赛事可提交的 Linux 可执行文件。队伍编号为 `1078`，alpha 阶段的最终可执行文件名应为：

```bash
cada1078_alpha
```

评测环境调用格式为：

```bash
./cada1078_alpha -config <config_file_path>
```

同时，赛事说明要求参赛者不能假设评测环境可以联网安装依赖：

> No general internet access for package installation and contestants should assume no general internet access for package installation

因此提交物必须提前包含运行所需的 Python 依赖、资源文件和必要 EDA 工具，不能依赖评测时执行 `pip install` 或 `apt install`。

---

## 当前代码入口

当前项目主入口为：

```bash
PYTHONPATH=src python3 src/contest_agent.py -config configs/contest.yml
```

对应源文件：

```text
src/contest_agent.py
```

该入口已经支持：

```text
-config / --config
--parser
--suite-root
--log-dir
--path-limit
```

因此打包方案不需要重写核心逻辑，只需要创建一个可执行包装层，使其等价于：

```bash
python3 src/contest_agent.py -config <config_file_path>
```

并保持 stdin/stdout 交互协议不变。

---

## 推荐方案

采用：

```text
Docker 构建环境 + PyInstaller 冻结 Python 程序 + 顶层 cada1078_alpha wrapper
```

原因：

1. Docker 可以固定 Linux 构建环境，减少本机/WSL/评测环境差异。
2. PyInstaller 可以把 Python 源码和 Python 包依赖打包为可执行程序。
3. 顶层 wrapper 可以设置外部 EDA 工具路径和资源路径。
4. 提交目录可以包含 `yosys`、`yosys-abc`、`abc`、`iverilog` 等工具，避免评测环境缺失系统命令。

---

## 运行依赖

### Python 包

当前实现需要：

```text
pyverilog
networkx
PyYAML
```

构建 PyInstaller 还需要：

```text
pyinstaller
```

### 系统命令

当前实现涉及：

```text
iverilog
yosys
yosys-abc 或 abc
```

其中：

- `iverilog`：PyVerilog 预处理某些 Verilog 文件时可能需要；
- `yosys`：部分功能等价验证/miters 需要；
- `abc` / `yosys-abc`：CEC 和深度优化相关功能需要。

PyInstaller 不会自动打包这些系统命令，因此需要在 Docker 镜像或提交目录中提前准备。

---

## 建议提交目录结构

推荐生成如下提交目录：

```text
submission/
├── cada1078_alpha
├── app/
│   └── cada1078_alpha_dist/
│       └── cada1078_alpha_bin
├── bin/
│   ├── yosys
│   ├── yosys-abc
│   ├── abc
│   └── iverilog
├── abc_resources/
│   ├── abc.rc
│   └── my.genlib
├── configs/
│   └── contest.yml
└── mcp_tools_spec.json
```

其中：

- `submission/cada1078_alpha` 是评测环境直接调用的文件；
- `app/cada1078_alpha_dist/cada1078_alpha_bin` 是 PyInstaller 生成的真实程序；
- `bin/` 中放外部 EDA 命令；
- `abc_resources/` 中放 ABC 脚本和工艺库；
- `mcp_tools_spec.json` 是 agent 工具契约文件；
- `configs/contest.yml` 是不含密钥的示例配置。

---

## 顶层 wrapper 职责

`submission/cada1078_alpha` 应：

1. 计算自身所在目录；
2. 将 `submission/bin` 加入 `PATH`；
3. 设置：
   - `GENLIB_PATH`
   - `ABC_RC_PATH`
   - `ABC_BIN`
   - `ABC_CEC_BIN`
4. 将所有命令行参数原样转发给 PyInstaller 程序。

最终仍支持：

```bash
./cada1078_alpha -config <config_file_path>
```

---

## PyInstaller 打包要点

PyInstaller spec 应包含：

### 源码

```text
src/
```

### 资源文件

```text
mcp_tools_spec.json
abc_resources/abc.rc
abc_resources/my.genlib
configs/contest.yml
```

### Hidden imports

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

### 输出名

PyInstaller 真实程序命名为：

```text
cada1078_alpha_bin
```

PyInstaller onedir 目录命名为：

```text
cada1078_alpha_dist
```

---

## 资源路径兼容

源码运行时，资源位于仓库根目录。

PyInstaller 运行时，资源可能位于 `sys._MEIPASS`。

因此代码需要支持：

```python
getattr(sys, "_MEIPASS", None)
```

以便冻结后仍能找到：

```text
mcp_tools_spec.json
abc_resources/my.genlib
abc_resources/abc.rc
```

---

## 构建方式

### Docker 构建

```bash
docker build \
  -f packaging/docker/Dockerfile.pyinstaller \
  -t iccad-cada1078-builder \
  .
```

```bash
mkdir -p dist_out

docker run --rm \
  -v "$PWD/dist_out:/out" \
  iccad-cada1078-builder
```

构建产物：

```text
dist_out/submission/
```

最终可执行文件：

```text
dist_out/submission/cada1078_alpha
```

---

## 本地构建

如果本机已经安装 PyInstaller、Python 依赖和 EDA 工具，也可以运行：

```bash
python3 -m pip install -r packaging/requirements-lock.txt
bash packaging/build_pyinstaller.sh
```

生成：

```text
submission/cada1078_alpha
```

---

## 验证命令

### 检查可执行文件

```bash
test -x submission/cada1078_alpha
```

### 检查帮助输出

```bash
submission/cada1078_alpha --help
```

### 检查赛事调用格式

```bash
submission/cada1078_alpha -config submission/configs/contest.yml
```

### stdin 协议 smoke test

```bash
printf 'This is the beginning of testcase case28.\n' | \
  submission/cada1078_alpha -config submission/configs/contest.yml
```

预期输出包含：

```text
#RESPONSE 1
#END 1
```

并生成：

```text
case28.log
```

---

## API key 注意事项

不要把 API key 写入提交物。

当前 `configs/contest.yml` 中：

```yaml
api_key: null
```

这是合理的。评测环境可通过官方 config 或环境变量提供：

```text
OPENAI_API_KEY
ANTHROPIC_API_KEY
```

---

## 最终建议

最终提交时，应确认：

1. 顶层文件名严格为 `cada1078_alpha`；
2. 文件具有可执行权限；
3. 可执行文件支持 `-config <config_file_path>`；
4. 不依赖源码目录 `/home/xjw/ICCAD`；
5. 不依赖联网安装 Python packages；
6. 不在提交物中包含 API key；
7. `yosys`、`yosys-abc`、`abc`、`iverilog` 可被程序找到；
8. `mcp_tools_spec.json`、`abc.rc`、`my.genlib` 可被程序找到。
