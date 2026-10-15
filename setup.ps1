# 一条命令创建 venv 并安装依赖：  ./setup.ps1
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true
python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -r requirements.txt
