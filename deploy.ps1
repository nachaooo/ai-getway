# AI Gateway 部署脚本
# 读取 config.conf 配置

$config = @{}
Get-Content "config.conf" | ForEach-Object {
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
$sshTest = ssh -i $SSH_KEY -p $SSH_PORT -o StrictHostKeyChecking=no -o ConnectTimeout=10 "$DEPLOY_HOST" "echo 'SSH OK'" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "SSH 连接失败: $sshTest" -ForegroundColor Red
    exit 1
}
Write-Host "SSH 连接成功" -ForegroundColor Green

# 创建远程目录
Write-Host "`n[2/5] 创建远程目录..." -ForegroundColor Green
ssh -i $SSH_KEY -p $SSH_PORT "$DEPLOY_HOST" "mkdir -p $DEPLOY_PATH"
if ($LASTEXITCODE -ne 0) {
    Write-Host "创建目录失败" -ForegroundColor Red
    exit 1
}

# 同步文件到远程服务器
Write-Host "`n[3/5] 同步文件到远程服务器..." -ForegroundColor Green
$rsyncArgs = @(
    "-avz",
    "-e", "ssh -i $SSH_KEY -p $SSH_PORT",
    "--exclude", ".git",
    "--exclude", "__pycache__",
    "--exclude", "*.pyc",
    "--exclude", "dist",
    "--exclude", "build",
    "--exclude", "*.spec",
    "--exclude", ".pytest_cache",
    "--exclude", "usage.db*",
    "--exclude", "*.log",
    "./",
    "${DEPLOY_USER}@${DEPLOY_HOST}:${DEPLOY_PATH}/"
)
& rsync @rsyncArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host "文件同步失败" -ForegroundColor Red
    exit 1
}

# 在远程服务器上安装依赖和配置
Write-Host "`n[4/5] 配置远程环境..." -ForegroundColor Green
$remoteScript = @"
cd $DEPLOY_PATH

# 安装 Python 依赖
pip3 install -r requirements.txt

# 创建 systemd 服务文件
cat > /etc/systemd/system/${APP_NAME}.service << 'EOF'
[Unit]
Description=AI Gateway Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$DEPLOY_PATH
ExecStart=/usr/bin/python3 app.py
Restart=always
RestartSec=5
Environment=PORT=$WEB_PORT

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
"@

ssh -i $SSH_KEY -p $SSH_PORT "$DEPLOY_HOST" $remoteScript
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