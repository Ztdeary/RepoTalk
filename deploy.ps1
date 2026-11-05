# ============================================================
# RepoTalk 一键部署脚本 (PowerShell)
#
# 用法:
#   .\deploy.ps1                    # 完整部署:构建+启动+健康检查
#   .\deploy.ps1 -SkipBuild          # 跳过镜像构建,只重启服务
#   .\deploy.ps1 -RunEvaluation      # 部署后跑 RAGAS 评测
#   .\deploy.ps1 -Down               # 停止并删除所有容器
#
# 前置条件:
#   1. 已安装 Docker Desktop 且在运行
#   2. backend/.env 已配置好所有密钥
#   3. frontend 已 npm install
# ============================================================

# ---- 参数定义 ----
param(
    [switch]$SkipBuild,       # 跳过 Docker 镜像构建
    [switch]$RunEvaluation,   # 部署后运行 RAGAS 评测
    [switch]$Down             # 停止并删除所有容器
)

# ---- 错误处理:任何命令失败就停止 ----
$ErrorActionPreference = "Stop"

# ---- 颜色输出辅助函数 ----
function Write-Step($msg) {
    Write-Host "`n>>> $msg" -ForegroundColor Cyan
}
function Write-Ok($msg) {
    Write-Host "    [OK] $msg" -ForegroundColor Green
}
function Write-Err($msg) {
    Write-Host "    [FAIL] $msg" -ForegroundColor Red
}

# ---- 切换到项目根目录 ----
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot
# $MyInvocation.MyCommand.Path:当前脚本的绝对路径
# Split-Path -Parent:取父目录(项目根目录)
# Set-Location:切换工作目录

# ============================================================
# 模式 1:停止并删除所有容器
# ============================================================
if ($Down) {
    Write-Step "停止并删除所有容器..."
    docker compose down
    Write-Ok "所有容器已停止"
    exit 0
}

# ============================================================
# 模式 2:正常部署流程
# ============================================================

# ---- 步骤 1:检查 .env 是否存在 ----
Write-Step "检查 backend/.env ..."
$EnvFile = Join-Path $ProjectRoot "backend\.env"
if (-not (Test-Path $EnvFile)) {
    Write-Err "backend/.env 不存在,请先创建并配置密钥"
    exit 1
}
Write-Ok ".env 已存在"

# ---- 步骤 2:构建前端静态文件 ----
Write-Step "构建前端静态文件..."
Push-Location frontend
# Push-Location:保存当前目录并切换到 frontend/
try {
    npm run build
    # npm run build:执行 package.json 里的 build 脚本
    # Vite 会把产物输出到 frontend/dist/
    Write-Ok "前端构建完成"
}
finally {
    Pop-Location
    # Pop-Location:恢复到之前的目录
}

# ---- 步骤 3:构建后端 Docker 镜像 ----
if (-not $SkipBuild) {
    Write-Step "构建后端 Docker 镜像..."
    docker compose build backend
    # docker compose build backend:只构建 backend 服务的镜像
    # Dockerfile 在 backend/ 目录下
    Write-Ok "后端镜像构建完成"
} else {
    Write-Step "跳过镜像构建(-SkipBuild)"
}

# ---- 步骤 4:启动所有服务 ----
Write-Step "启动所有服务(db, redis, backend, nginx)..."
docker compose up -d
# docker compose up -d:后台启动所有在 docker-compose.yml 里定义的服务
# -d:detached 模式,容器在后台运行

Write-Ok "服务启动命令已发送"

# ---- 步骤 5:等待后端健康检查通过 ----
Write-Step "等待后端健康检查..."
$MaxRetries = 30
# MaxRetries:最多重试 30 次
$RetryInterval = 2
# RetryInterval:每次重试间隔 2 秒
$HealthOk = $false
# HealthOk:健康检查是否通过的标记

for ($i = 1; $i -le $MaxRetries; $i++) {
    # for 循环:重试 MaxRetries 次
    try {
        $resp = Invoke-WebRequest -Uri "http://localhost:8000/health" -TimeoutSec 3 -UseBasicParsing
        # Invoke-WebRequest:发 HTTP 请求
        # -Uri:请求地址
        # -TimeoutSec 3:3 秒超时
        # -UseBasicParsing:不用 IE 引擎解析(服务器环境没有 IE)
        if ($resp.StatusCode -eq 200) {
            $HealthOk = $true
            break
        }
    }
    catch {
        # 请求失败(服务还没起来),继续等
    }
    Write-Host "    等待中... ($i/$MaxRetries)"
    Start-Sleep -Seconds $RetryInterval
    # Start-Sleep:暂停指定秒数
}

if (-not $HealthOk) {
    Write-Err "后端健康检查超时,请查看日志: docker compose logs backend"
    exit 1
}
Write-Ok "后端健康检查通过"

# ---- 步骤 6:检查 Nginx ----
Write-Step "检查 Nginx..."
try {
    $resp = Invoke-WebRequest -Uri "http://localhost:80" -TimeoutSec 3 -UseBasicParsing
    if ($resp.StatusCode -eq 200) {
        Write-Ok "Nginx 正常,前端可访问 http://localhost"
    }
}
catch {
    Write-Err "Nginx 检查失败,请查看日志: docker compose logs nginx"
    exit 1
}

# ---- 步骤 7:可选 —— 运行 RAGAS 评测 ----
if ($RunEvaluation) {
    Write-Step "运行 RAGAS 四指标评测..."
    Push-Location backend
    try {
        uv run python -m tests.test_week6
        # uv run:在项目虚拟环境里运行命令
        # python -m tests.test_week6:以模块方式运行评测脚本
        Write-Ok "RAGAS 评测完成"
    }
    catch {
        Write-Err "RAGAS 评测失败,详见上方输出"
    }
    finally {
        Pop-Location
    }
}

# ---- 完成 ----
Write-Step "部署完成!"
Write-Host "  前端: http://localhost" -ForegroundColor White
Write-Host "  API 文档: http://localhost:8000/docs" -ForegroundColor White
Write-Host "  健康检查: http://localhost:8000/health" -ForegroundColor White
