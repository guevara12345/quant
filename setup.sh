#!/usr/bin/env sh
# 一条命令创建 venv 并安装依赖：  sh setup.sh
set -e
python -m venv .venv
if [ -x .venv/Scripts/python ]; then PY=.venv/Scripts/python; else PY=.venv/bin/python; fi
$PY -m pip install --upgrade pip
$PY -m pip install -r requirements.txt
