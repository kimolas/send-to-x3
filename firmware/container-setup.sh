#!/bin/sh
# Turns a python:3.13-slim container into the firmware builder (build.sh then
# commits it as an image). The same tools as upstream CI
# (.github/workflows/ci.yml): Python 3.13, pioarduino core 6.1.19 and the
# platform's Python modules, plus a C++ compiler, cmake and ninja for the host
# tests. Made with `docker run` + `docker commit` rather than `docker build`,
# which insists on contacting the registry for the base image.
set -eu
apt-get update
apt-get install -y --no-install-recommends git g++ make cmake ninja-build libexpat1-dev
rm -rf /var/lib/apt/lists/*
pip install --no-cache-dir uv
uv pip install --system --no-cache \
  https://github.com/pioarduino/platformio-core/archive/refs/tags/v6.1.19.zip \
  "littlefs-python>=0.16.0" "fatfs-ng>=0.1.14" "pyyaml>=6.0.2" "rich-click>=1.8.6" \
  "zopfli>=0.2.2" "intelhex>=2.3.0" "rich>=14.0.0" "urllib3<2" "cryptography>=45.0.3" \
  "certifi>=2025.8.3" "ecdsa>=0.19.1" "bitstring>=4.3.1" "reedsolo>=1.5.3,<1.8" \
  "esp-idf-size>=2.0.0" "esp-coredump>=1.14.0"
