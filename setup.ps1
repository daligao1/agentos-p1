<#
.SYNOPSIS
    一键重建 AgentOS LangGraph POC 的运行环境（venv + 依赖 + 本机兼容补丁 + 冒烟测试）。

.DESCRIPTION
    本机 Python 3.12（D:\DSH-tools\python312）上，ormsgpack>=1.12 与 xxhash 4.x 的 Windows wheel
    会报 "DLL load failed"，所以固定：ormsgpack==1.10.0、xxhash==3.5.0，并补一个 sitecustomize.py
    （ormsgpack 1.10 缺 OPT_REPLACE_SURROGATES 常量，补 0 即可）。
    优先用 uv（秒级）；没有 uv 则退回 python -m venv + pip。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1 -VenvDir .venv-test
#>
param(
    [string]$VenvDir = '.venv',
    [string]$Python = 'D:\DSH-tools\python312\python.exe',
    [string]$IndexUrl = 'https://mirrors.aliyun.com/pypi/simple/'
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
chcp 65001 > $null
$env:PYTHONIOENCODING = 'utf-8'

function Write-Head([string]$t) {
    Write-Host ''
    Write-Host ("=" * 60) -ForegroundColor DarkGray
    Write-Host "  $t" -ForegroundColor Cyan
    Write-Host ("=" * 60) -ForegroundColor DarkGray
}

Write-Head "1/4 创建虚拟环境（$VenvDir）"
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (Test-Path $VenvDir) { Remove-Item $VenvDir -Recurse -Force }
if ($uv) {
    & uv venv $VenvDir --python $Python
    $useUv = $true
} else {
    & $Python -m venv $VenvDir
    $useUv = $false
}
$py = Join-Path $VenvDir 'Scripts\python.exe'
if (-not (Test-Path $py)) { throw "虚拟环境创建失败：$py 不存在" }

Write-Head "2/4 安装依赖（镜像：$IndexUrl）"
$env:UV_INDEX_URL = $IndexUrl
if ($useUv) {
    & uv pip install "langgraph==1.2.20" "langgraph-checkpoint-sqlite==3.1.3" --python $py
    & uv pip install --no-deps "ormsgpack==1.10.0" "xxhash==3.5.0" --python $py
} else {
    & $py -m pip install -q --upgrade pip
    & $py -m pip install -q -i $IndexUrl "langgraph==1.2.20" "langgraph-checkpoint-sqlite==3.1.3"
    & $py -m pip install -q -i $IndexUrl --no-deps "ormsgpack==1.10.0" "xxhash==3.5.0"
}

Write-Head "3/4 写本机兼容补丁 sitecustomize.py"
$siteDir = Join-Path $VenvDir 'Lib\site-packages'
$patch = @'
# 本机兼容补丁：ormsgpack 1.10 缺少 1.12+ 才有的 OPT_REPLACE_SURROGATES 常量；
# langgraph 的 checkpoint serde 在 import 时会读取它。补 0（无效果位），不影响普通序列化。
try:
    import ormsgpack
    if not hasattr(ormsgpack, "OPT_REPLACE_SURROGATES"):
        ormsgpack.OPT_REPLACE_SURROGATES = 0
except Exception:
    pass
'@
Set-Content -Path (Join-Path $siteDir 'sitecustomize.py') -Value $patch -Encoding UTF8

Write-Head "4/4 冒烟测试（interrupt -> resume）"
$smoke = Join-Path $env:TEMP ("poc-smoke-" + [guid]::NewGuid().ToString('N') + ".py")
$smokeCode = @'
import os
import sqlite3
import tempfile
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


class S(TypedDict, total=False):
    x: int
    d: str


def a(s):
    return {"x": 1}


def b(s):
    d = interrupt({"q": "approve?"})
    return {"d": d}


def c(s):
    return {"x": s.get("x", 0) + 1}


g = StateGraph(S)
g.add_node("a", a)
g.add_node("b", b)
g.add_node("c", c)
g.add_edge(START, "a")
g.add_edge("a", "b")
g.add_edge("b", "c")
g.add_edge("c", END)

db = os.path.join(tempfile.gettempdir(), "poc-smoke.sqlite")
if os.path.exists(db):
    os.remove(db)
ck = SqliteSaver(sqlite3.connect(db, check_same_thread=False))
try:
    ck.setup()
except Exception:
    pass
app = g.compile(checkpointer=ck)
cfg = {"configurable": {"thread_id": "smoke"}}
out = app.invoke({"x": 0}, cfg)
assert "__interrupt__" in out, "interrupt 未触发"
out2 = app.invoke(Command(resume="approve"), cfg)
assert out2.get("d") == "approve" and out2.get("x") == 2, out2
print("SMOKE OK: interrupt -> resume 通过")
'@
Set-Content -Path $smoke -Value $smokeCode -Encoding UTF8
try {
    & $py $smoke
    $code = $LASTEXITCODE
} finally {
    Remove-Item $smoke -Force -ErrorAction SilentlyContinue
}
if ($code -ne 0) { throw "冒烟测试失败（exit=$code）" }

Write-Head "环境就绪"
Write-Host "下一步：" -ForegroundColor Green
Write-Host "  .\$VenvDir\Scripts\python.exe make_demo_db.py    # 生成示例库"
Write-Host "  .\$VenvDir\Scripts\python.exe poc.py demo        # 跑全部演示场景（输出 evidence/）"
Write-Host "  .\$VenvDir\Scripts\python.exe poc.py run --task ""查询 orders 表并导出 CSV"""
