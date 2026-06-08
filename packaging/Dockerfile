FROM python:3.11-bullseye AS package

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /src

RUN apt-get update \
 && apt-get install -y --no-install-recommends --fix-missing \
    build-essential \
    iverilog \
    yosys \
    berkeley-abc \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY packaging/requirements-packaging.txt /src/packaging/requirements-packaging.txt
RUN python -m pip install --upgrade pip \
 && python -m pip install --no-cache-dir -r /src/packaging/requirements-packaging.txt

COPY src /src/src
COPY abc_resources /src/abc_resources
COPY mcp_tools_spec.json /src/mcp_tools_spec.json
COPY packaging /src/packaging

RUN bash /src/packaging/build_pyinstaller.sh

WORKDIR /app
RUN mkdir -p /app/configs \
 && cp /src/packaging/out/dist/cada1078_alpha /app/cada1078_alpha \
 && chmod +x /app/cada1078_alpha
COPY configs/cada1078_alpha.yml /app/configs/cada1078_alpha.yml
RUN command -v iverilog \
 && command -v yosys \
 && (command -v abc || command -v berkeley-abc || command -v yosys-abc) \
 && /app/cada1078_alpha --help >/dev/null

ENTRYPOINT ["/app/cada1078_alpha"]
