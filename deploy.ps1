# AI Gateway 部署脚本
# 读取 config.conf 配置
# 依赖：本地 Windows 自带 tar + ssh（无需安装 rsync）

$config = @{}
Get-Content "config.conf" -Encoding UTF8 | ForEach-Object {
    $line = $_.Trim()
    if ($line -and !$line.StartsWith("#")) {
        $parts = $line -split "=", 2
        if ($parts.Count -eq 2) {
            $config[$parts[0].Trim()] = $parts[1].Trim()
        }
    }
}

# 部署配置
$DEPLOY_HOST = $config["DEPLOY_HOST"]
$DEPLOY_USER = $config["DEPLOY_USER"]
$SSH_PORT = $config["SSH_PORT"]
$SSH_KEY = $config["SSH_KEY"]
$DEPLOY_PATH = $config["DEPLOY_PATH"]
$WEB_PORT = $config["WEB_PORT"]
$APP_NAME = $config["APP_NAME"]
$VERSION = $config["VERSION"]

$SSH_ARGS = @("-i", $SSH_KEY, "-p", $SSH_PORT, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10")
# scp 端口参数为 -P（大写），与 ssh 的 -p 不同，不能共用 $SSH_ARGS
$SCP_ARGS = @("-i", $SSH_KEY, "-P", $SSH_PORT, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10")

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "AI Gateway 部署脚本" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "目标服务器: $DEPLOY_HOST" -ForegroundColor Yellow
Write-Host "部署路径: $DEPLOY_PATH" -ForegroundColor Yellow
Write-Host "Web端口: $WEB_PORT" -ForegroundColor Yellow
Write-Host "版本: $VERSION" -ForegroundColor Yellow
Write-Host "========================================" -ForegroundColor Cyan

# 检查 SSH 连接
Write-Host "`n[1/5] 检查 SSH 连接..." -ForegroundColor Green
$sshTest = ssh $SSH_ARGS "${DEPLOY_USER}@${DEPLOY_HOST}" "echo 'SSH OK'" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "SSH 连接失败: $sshTest" -ForegroundColor Red
    exit 1
}
Write-Host "SSH 连接成功" -ForegroundColor Green

# 创建远程目录
Write-Host "`n[2/5] 创建远程目录..." -ForegroundColor Green
ssh $SSH_ARGS "${DEPLOY_USER}@${DEPLOY_HOST}" "mkdir -p $DEPLOY_PATH"
if ($LASTEXITCODE -ne 0) {
    Write-Host "创建目录失败" -ForegroundColor Red
    exit 1
}

# 部署前备份服务器数据库到本地（绝不删除服务器 db）
# 带宽有限（~0.5MB/s）且单文件大 scp 会卡死，采用：gzip 压缩 → 远程 split 成 30M 小块 → 逐块 scp → 本地合并
Write-Host "`n[0/5] 备份服务器数据库到本地（gzip + 分块传输）..." -ForegroundColor Green
$backupDir = Join-Path (Split-Path -Parent $PSCommandPath) "backups"
if (-not (Test-Path $backupDir)) { New-Item -ItemType Directory -Path $backupDir | Out-Null }
$ts = Get-Date -Format "yyyyMMdd_HHmmss"
$localDb = Join-Path $backupDir "usage_${DEPLOY_HOST}_${ts}.db.gz"
$localParts = Join-Path $backupDir "parts_${DEPLOY_HOST}_${ts}"
try {
    # 1. 远程 gzip + split
    ssh $SSH_ARGS "${DEPLOY_USER}@${DEPLOY_HOST}" "rm -rf /tmp/usage.parts; mkdir -p /tmp/usage.parts; gzip -c /opt/ai-getway/data/usage.db | split -b 30M - /tmp/usage.parts/part-"
    # 2. 本地建目录，逐块 scp（小块避免大流量卡死）
    if (Test-Path $localParts) { Remove-Item $localParts -Recurse -Force }
    New-Item -ItemType Directory -Path $localParts | Out-Null
    $partList = ssh $SSH_ARGS "${DEPLOY_USER}@${DEPLOY_HOST}" "ls /tmp/usage.parts/"
    $i = 0
    foreach ($p in $partList) {
        $i++
        scp $SCP_ARGS "${DEPLOY_USER}@${DEPLOY_HOST}:/tmp/usage.parts/$p" (Join-Path $localParts $p)
    }
    ssh $SSH_ARGS "${DEPLOY_USER}@${DEPLOY_HOST}" "rm -rf /tmp/usage.parts"
    # 3. 本地合并（cmd type）+ 清理临时目录
    $files = Get-ChildItem $localParts -Filter "part-*" | Sort-Object Name
    $cmd = "type " + ($files.FullName -join " ")
    cmd /c "$cmd > `"$localDb`""
    Remove-Item $localParts -Recurse -Force
    "本地备份完成: $localDb (" + (Get-Item $localDb).Length + " bytes, gzip 压缩 + 分块传输)"
} catch {
    Write-Host "备份失败（不影响部署）: " + $_.Exception.Message -ForegroundColor Yellow
    ssh $SSH_ARGS "${DEPLOY_USER}@${DEPLOY_HOST}" "rm -rf /tmp/usage.parts /tmp/usage.db.gz" 2>$null
    Remove-Item $localParts -Recurse -Force -ErrorAction SilentlyContinue
}

# 同步文件到远程服务器（tar 流式传输）
Write-Host "`n[3/5] 同步文件到远程服务器..." -ForegroundColor Green
$excludes = @(
    "--exclude", ".git",
    "--exclude", "__pycache__",
    "--exclude", "*.pyc",
    "--exclude", "dist",
    "--exclude", "build",
    "--exclude", "*.spec",
    "--exclude", ".pytest_cache",
    "--exclude", "*.db*",
    "--exclude", "*.log",
    "--exclude", "deploy.ps1.bak",
    "--exclude", "*.vbs"
)
$tarCmd = "tar -czf - $($excludes -join ' ') -C . ."
$remoteUnpack = "mkdir -p $DEPLOY_PATH && tar -xzf - -C $DEPLOY_PATH"
$pipeline = "$tarCmd | ssh $SSH_ARGS ${DEPLOY_USER}@${DEPLOY_HOST} `"$remoteUnpack`""
& cmd /c $pipeline
if ($LASTEXITCODE -ne 0) {
    Write-Host "文件同步失败" -ForegroundColor Red
    exit 1
}
Write-Host "文件同步完成" -ForegroundColor Green

# 在远程服务器上安装依赖和配置
Write-Host "`n[4/5] 配置远程环境..." -ForegroundColor Green
$remoteScript = @"
cd $DEPLOY_PATH

# 确保有 swap：内存触顶时提供缓冲，避免直接 OOM 把整机拖到假死
if ! swapon --show | grep -q .; then
  if [ ! -f /swapfile ]; then
    fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
    chmod 600 /swapfile
    mkswap /swapfile
  fi
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo 'swap 已启用: 2G'
else
  echo 'swap 已存在，跳过创建'
fi
sysctl -w vm.swappiness=10
grep -q '^vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >> /etc/sysctl.conf

# 安装 Python 依赖
pip3 install -r requirements.txt

# 创建 systemd 服务文件
cat > /etc/systemd/system/${APP_NAME}.service << 'EOF'
[Unit]
Description=AI Gateway Service
After=network.target
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
User=root
WorkingDirectory=$DEPLOY_PATH
ExecStart=/usr/bin/python3 app.py
Restart=on-failure
RestartSec=10
Environment=PORT=$WEB_PORT
Environment=DB_PATH=$DEPLOY_PATH/data/usage.db
Environment=ENABLE_DEEPSEEK_TOKENIZER=0
Environment=THREADS=32
Environment=PYTHONUNBUFFERED=1
MemoryHigh=2000M
MemoryMax=2500M
MemorySwapMax=512M
OOMPolicy=stop

[Install]
WantedBy=multi-user.target
EOF

# 重新加载 systemd 并启动服务
systemctl daemon-reload
systemctl enable ${APP_NAME}
systemctl restart ${APP_NAME}

# 检查服务状态
sleep 2
systemctl status ${APP_NAME} --no-pager

# 内存水位快照：部署后确认服务实际占用
echo '--- 内存水位 (MB) ---'
free -m
echo '--- 服务实际占用 ---'
systemctl show ${APP_NAME} -p MemoryCurrent
"@

ssh $SSH_ARGS "${DEPLOY_USER}@${DEPLOY_HOST}" $remoteScript
if ($LASTEXITCODE -ne 0) {
    Write-Host "远程配置失败" -ForegroundColor Red
    exit 1
}

# 验证部署
Write-Host "`n[5/5] 验证部署..." -ForegroundColor Green
$checkUrl = "http://${DEPLOY_HOST}:${WEB_PORT}"
try {
    $response = Invoke-WebRequest -Uri $checkUrl -TimeoutSec 10 -ErrorAction Stop
    if ($response.StatusCode -eq 200) {
        Write-Host "部署成功！" -ForegroundColor Green
        Write-Host "访问地址: $checkUrl" -ForegroundColor Cyan
    }
} catch {
    Write-Host "警告: 无法访问 $checkUrl" -ForegroundColor Yellow
    Write-Host "请检查服务器防火墙设置" -ForegroundColor Yellow
}

Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host "部署完成" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
