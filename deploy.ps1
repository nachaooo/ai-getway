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
$HEALTH_PORT = $config["HEALTH_PORT"]
# config.conf 未被 git 追踪，新环境可能没有该键，给默认值兜底
if (-not $HEALTH_PORT) { $HEALTH_PORT = "6771" }
$APP_NAME = $config["APP_NAME"]
$VERSION = $config["VERSION"]

$SSH_ARGS = @("-i", $SSH_KEY, "-p", $SSH_PORT, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10")

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

# 部署前在服务器本地做数据库快照（零带宽、秒级；绝不删除/覆盖服务器 db）
# 归档到 $DEPLOY_PATH/backups/usage_YYYYMMDD_HHMMSS.tar.gz，仅保留最近 5 份
# 直接 tar 源文件（含 -wal/-shm），不做 sqlite3 .backup —— 库很大且可能非 WAL，
# .backup 会在持续写入下反复重试跑不完（实测卡死），cp/tar 无此问题
Write-Host "`n[0/5] 在服务器本地备份数据库..." -ForegroundColor Green
$remoteBackup = @'
set -e
BACKUP_DIR=__DEPLOY_PATH__/backups
DATA_DIR=__DEPLOY_PATH__/data
mkdir -p $BACKUP_DIR
TS=$(date +%Y%m%d_%H%M%S)
if [ ! -f $DATA_DIR/usage.db ]; then
  echo skip: no database at $DATA_DIR/usage.db
  exit 0
fi
cd $DATA_DIR
RC=0
tar --warning=no-file-changed -czf $BACKUP_DIR/usage_$TS.tar.gz usage.db $(ls usage.db-wal usage.db-shm 2>/dev/null) || RC=$?
if [ $RC -gt 1 ]; then echo backup tar failed rc=$RC; exit 1; fi
echo backup done: $BACKUP_DIR/usage_$TS.tar.gz $(du -h $BACKUP_DIR/usage_$TS.tar.gz | cut -f1)
ls -1t $BACKUP_DIR/usage_*.tar.gz | tail -n +6 | xargs -r rm -f
'@
$remoteBackup = $remoteBackup.Replace('__DEPLOY_PATH__', $DEPLOY_PATH)
ssh $SSH_ARGS "${DEPLOY_USER}@${DEPLOY_HOST}" $remoteBackup
if ($LASTEXITCODE -ne 0) {
    Write-Host "数据库备份失败（不影响部署，继续）" -ForegroundColor Yellow
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
    "--exclude", "backups",
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
Environment=HEALTH_PORT=$HEALTH_PORT
Environment=MAX_REQUEST_MB=64
Environment=STATELESS_BODY_MB=4
Environment=LARGE_BODY_CONCURRENCY=4
Environment=TOKENIZE_MAX_BYTES=65536
Environment=CONV_CONTENT_MAX_BYTES=262144
MemoryHigh=1800M
MemoryMax=2000M
MemorySwapMax=512M
OOMPolicy=stop

[Install]
WantedBy=multi-user.target
EOF

# 假死看门狗：进程活着但无响应（GIL 被 C 扩展独占）时 Restart=on-failure 不会触发，
# 靠定时探活兜底。健康端口是独立线程/独立端口，线程池占满时仍能应答，
# 只有真假死才失败；连续 3 次失败（约 90s）才重启，避免误杀长流式请求。
cat > /etc/systemd/system/${APP_NAME}-watchdog.service << 'EOF'
[Unit]
Description=AI Gateway health watchdog

[Service]
Type=oneshot
ExecStart=/bin/bash -c 'for i in 1 2 3; do curl -sf -m 10 -o /dev/null http://127.0.0.1:$HEALTH_PORT/ && exit 0; sleep 20; done; echo watchdog: gateway unresponsive, restarting >> /var/log/ai-gateway-watchdog.log; systemctl restart $APP_NAME'
EOF

cat > /etc/systemd/system/${APP_NAME}-watchdog.timer << 'EOF'
[Unit]
Description=Probe AI Gateway health every 30s

[Timer]
OnBootSec=90
OnUnitActiveSec=30
AccuracySec=5s

[Install]
WantedBy=timers.target
EOF

# 重新加载 systemd 并启动服务
systemctl daemon-reload
systemctl enable ${APP_NAME}
systemctl restart ${APP_NAME}
systemctl enable --now ${APP_NAME}-watchdog.timer

# 同步 nginx 站点配置（deploy.ps1 以前不覆盖该文件，导致仓库里的 nginx.conf 与线上不一致）
# 注意：这里是 PowerShell 双引号 here-string，不能用 bash 局部变量（`$VAR 会被 PowerShell 吃掉），
# 只能用 ${APP_NAME}/${DEPLOY_PATH} 这些 PowerShell 里已定义的变量。
if [ -f ${DEPLOY_PATH}/nginx.conf ]; then
  cp /etc/nginx/conf.d/${APP_NAME}.conf /etc/nginx/conf.d/${APP_NAME}.conf.bak 2>/dev/null || true
  if cp ${DEPLOY_PATH}/nginx.conf /etc/nginx/conf.d/${APP_NAME}.conf; then
    if nginx -t; then
      systemctl reload nginx
      echo 'nginx 配置已同步并 reload'
      rm -f /etc/nginx/conf.d/${APP_NAME}.conf.bak
    else
      cp /etc/nginx/conf.d/${APP_NAME}.conf.bak /etc/nginx/conf.d/${APP_NAME}.conf 2>/dev/null || true
      systemctl reload nginx
      echo 'nginx 配置校验失败，已回滚旧配置' >&2
    fi
  else
    echo 'nginx 配置复制失败，未改动线上配置' >&2
  fi
fi

# 检查服务状态
sleep 2
systemctl status ${APP_NAME} --no-pager

# 健康检查（轻量端点，不查库）
echo '--- health ---'
for i in 1 2 3 4 5; do
  if curl -sf -m 5 http://127.0.0.1:${HEALTH_PORT}/; then echo; break; fi
  sleep 2
done

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
