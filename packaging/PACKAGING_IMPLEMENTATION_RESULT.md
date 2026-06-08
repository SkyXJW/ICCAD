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

## 当前环境构建情况

当前环境检测结果：

- Python: `/home/xjw/miniconda3/envs/ICCAD/bin/python3`
- pip: 可用
- Docker: 当前 PATH 中未检测到
- PyInstaller: 已安装并可用，版本 `6.20.0`
- Python 依赖：
  - `pyverilog 1.3.0`
  - `networkx 3.4.2`
  - `PyYAML 6.0.3`
- EDA 工具：
  - `/usr/bin/yosys`
  - `/usr/bin/yosys-abc`
  - `/usr/bin/iverilog`

由于当前环境没有 Docker CLI，因此本次实际构建采用本地 PyInstaller 路径完成。Dockerfile 仍已保留，可在有 Docker 的环境中复现构建。

---

## 构建过程中处理的问题

### 1. 缺少 PyInstaller

已执行：

```bash
python3 -m pip install pyinstaller
```

安装成功：

```text
pyinstaller 6.20.0
```

### 2. 缺少 `objdump` / `objcopy`

PyInstaller 在 Linux 下需要 binutils。系统 `sudo apt-get install binutils` 因当前非交互环境无法输入 sudo 密码而失败。

改用 conda 安装：

```bash
conda install -y -n ICCAD -c conda-forge binutils_impl_linux-64
```

然后为 PyInstaller 暴露标准命令名：

```bash
ln -sf /home/xjw/miniconda3/envs/ICCAD/bin/x86_64-conda-linux-gnu-objdump /home/xjw/miniconda3/envs/ICCAD/bin/objdump
ln -sf /home/xjw/miniconda3/envs/ICCAD/bin/x86_64-conda-linux-gnu-objcopy /home/xjw/miniconda3/envs/ICCAD/bin/objcopy
ln -sf /home/xjw/miniconda3/envs/ICCAD/bin/x86_64-conda-linux-gnu-strip /home/xjw/miniconda3/envs/ICCAD/bin/strip
ln -sf /home/xjw/miniconda3/envs/ICCAD/bin/x86_64-conda-linux-gnu-readelf /home/xjw/miniconda3/envs/ICCAD/bin/readelf
```

### 3. PyVerilog 缺少 `VERSION` 数据文件

第一次生成的可执行程序运行时报错：

```text
FileNotFoundError: .../pyverilog/VERSION
```

已修改：

```text
packaging/cada1078_alpha.spec
```

加入：

```python
from PyInstaller.utils.hooks import collect_data_files
...
*collect_data_files("pyverilog")
```

随后重新构建成功，并通过 `--help` 运行验证。

---

## 已生成最终提交目录

已成功生成：

```text
/home/xjw/ICCAD/submission/
```

目录内容：

```text
submission/
├── cada1078_alpha
├── app/
│   └── cada1078_alpha_dist/
│       └── cada1078_alpha_bin
├── bin/
│   ├── iverilog
│   ├── yosys
│   └── yosys-abc
├── configs/
│   └── contest.yml
├── abc_resources/
│   ├── abc.rc
│   └── my.genlib
└── mcp_tools_spec.json
```

最终评测入口：

```text
/home/xjw/ICCAD/submission/cada1078_alpha
```

---

## 已执行验证

### 1. 构建成功

执行：

```bash
bash /home/xjw/ICCAD/packaging/build_pyinstaller.sh
```

结果：成功。

输出：

```text
Build complete! The results are available in: /home/xjw/ICCAD/dist
Submission generated at /home/xjw/ICCAD/submission
```

### 2. 文件存在且可执行

验证：

```bash
test -x /home/xjw/ICCAD/submission/cada1078_alpha
test -x /home/xjw/ICCAD/submission/app/cada1078_alpha_dist/cada1078_alpha_bin
```

结果：通过。

文件类型：

```text
/home/xjw/ICCAD/submission/cada1078_alpha: Bourne-Again shell script, ASCII text executable
/home/xjw/ICCAD/submission/app/cada1078_alpha_dist/cada1078_alpha_bin: ELF 64-bit LSB executable, x86-64
```

### 3. `--help` 验证

执行：

```bash
/home/xjw/ICCAD/submission/cada1078_alpha --help
```

结果：成功输出 CLI 帮助，包含：

```text
-config CONFIG, --config CONFIG
--parser {hybrid,llm,regex}
--suite-root SUITE_ROOT
--log-dir LOG_DIR
--path-limit PATH_LIMIT
```

### 4. stdin 协议 smoke test

执行：

```bash
tmpdir=$(mktemp -d)
cd "$tmpdir"
printf 'This is the beginning of a new testcase. The case name is case28.\n' | \
  /home/xjw/ICCAD/submission/cada1078_alpha \
    -config /home/xjw/ICCAD/submission/configs/contest.yml \
    --parser regex
```

输出：

```text
#RESPONSE 1
Acknowledged. Initialized testcase "case28". All subsequent responses will be recorded to case28.log.
#END 1
```

并生成 `case28.log`，其中包含：

```text
#RESPONSE 1
#END 1
```

### 5. 提交目录大小

```text
submission: 80M
dist: 59M
build: 11M
```

---

## 当前限制和注意事项

1. 本次最终二进制是本地 PyInstaller 构建产物，不是 Docker 构建产物，因为当前环境没有 Docker CLI。
2. Dockerfile 已存在，可在有 Docker 的环境中执行：

   ```bash
   docker build -f packaging/docker/Dockerfile.pyinstaller -t iccad-cada1078-builder .
   docker run --rm -v "$PWD/dist_out:/out" iccad-cada1078-builder
   ```

3. 当前提交目录中未包含独立 `abc`，因为当前环境检测到的是 shell alias `abc='berkeley-abc'`，非真实可复制文件；但已包含 `yosys-abc`，wrapper 会设置 `ABC_CEC_BIN` 使用它。
4. 提交配置中的 `api_key` 为 `null`，没有写入真实 API key。
5. 若评测环境要求严格单文件而非目录，需要进一步改为 onefile 或把目录打包成官方允许的提交格式；目前方案是可执行入口 + 依赖目录的 submission layout。

---

## 最终检查清单

提交前应确认：

1. 文件名严格为 `cada1078_alpha`；
2. 文件具有可执行权限；
3. 支持：

   ```bash
   ./cada1078_alpha -config <config_file_path>
   ```

4. `submission/app/cada1078_alpha_dist/cada1078_alpha_bin` 存在；
5. `mcp_tools_spec.json` 存在；
6. `abc_resources/my.genlib` 存在；
7. `abc_resources/abc.rc` 存在；
8. `bin/yosys`、`bin/yosys-abc`、`bin/iverilog` 存在；
9. 不包含真实 API key；
10. stdin/stdout 协议可输出 `#RESPONSE <id>` 和 `#END <id>`。
