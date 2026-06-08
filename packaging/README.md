# ICCAD PyInstaller Packaging

This directory builds a contest submission executable named `cada1078_alpha`.

## Build in Docker

```bash
docker build -f packaging/docker/Dockerfile.pyinstaller -t iccad-cada1078-builder .
docker run --rm -v "$PWD/dist_out:/out" iccad-cada1078-builder
```

The submission directory will be copied to:

```text
dist_out/submission/
```

The evaluator-facing executable is:

```bash
dist_out/submission/cada1078_alpha
```

It accepts the contest invocation format:

```bash
./cada1078_alpha -config <config_file_path>
```

## Local build

If the local machine already has PyInstaller, Python dependencies, and EDA tools installed:

```bash
bash packaging/build_pyinstaller.sh
```

This creates:

```text
submission/
├── cada1078_alpha
├── app/cada1078_alpha_dist/
├── bin/
├── configs/contest.yml
├── abc_resources/
└── mcp_tools_spec.json
```

## Runtime notes

The top-level `cada1078_alpha` wrapper prepends `submission/bin` to `PATH` and sets `GENLIB_PATH`, `ABC_RC_PATH`, `ABC_BIN`, and `ABC_CEC_BIN` when bundled files are present. Do not store API keys in the submitted config; let the judge config or environment provide them.
