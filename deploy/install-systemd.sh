#!/bin/bash
# Auto Router systemd 部署脚本
# 用法: bash install-systemd.sh
set -e

INSTALL_DIR="/opt/ai-hub/auto-router"
SERVICE_FILE="/etc/systemd/system/auto-router.service"
LOGROTATE_FILE="/etc/logrotate.d/auto-router"
LOG_DIR="/var/log/auto-router"

echo "=== Auto Router systemd 部署 ==="

# 1. 创建日志目录
echo "[1/6] 创建日志目录..."
mkdir -p "$LOG_DIR"
chown root:root "$LOG_DIR"

# 2. 停止旧 nohup 进程
echo "[2/6] 停止旧 nohup 进程..."
pkill -f "uvicorn.*8080" 2>/dev/null && echo "  已停止旧进程" || echo "  无旧进程"
sleep 2

# 3. 安装 service 文件
echo "[3/6] 安装 systemd service 文件..."
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/auto-router.service" ]; then
    cp "$SCRIPT_DIR/auto-router.service" "$SERVICE_FILE"
else
    # 从仓库 deploy 目录复制
    if [ -f "$INSTALL_DIR/../auto-router-repo/deploy/auto-router.service" ]; then
        cp "$INSTALL_DIR/../auto-router-repo/deploy/auto-router.service" "$SERVICE_FILE"
    else
        echo "  错误: 找不到 auto-router.service 文件"
        exit 1
    fi
fi
echo "  已安装到 $SERVICE_FILE"

# 4. 安装 logrotate
echo "[4/6] 安装 logrotate 配置..."
if [ -f "$SCRIPT_DIR/auto-router.logrotate" ]; then
    cp "$SCRIPT_DIR/auto-router.logrotate" "$LOGROTATE_FILE"
elif [ -f "$INSTALL_DIR/../auto-router-repo/deploy/auto-router.logrotate" ]; then
    cp "$INSTALL_DIR/../auto-router-repo/deploy/auto-router.logrotate" "$LOGROTATE_FILE"
else
    cat > "$LOGROTATE_FILE" << 'LOGROTATE_EOF'
/var/log/auto-router/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0644 root root
    postrotate
        systemctl reload auto-router > /dev/null 2>&1 || true
    endpostrotate
}
LOGROTATE_EOF
fi
echo "  已安装到 $LOGROTATE_FILE"

# 5. 重载 systemd 并启动
echo "[5/6] 启动服务..."
systemctl daemon-reload
systemctl enable auto-router
systemctl start auto-router
sleep 3

# 6. 健康检查
echo "[6/6] 健康检查..."
if curl -s http://127.0.0.1:8080/health | grep -q "ok"; then
    echo "  ✅ 服务启动成功"
    systemctl status auto-router --no-pager -l | head -15
else
    echo "  ❌ 健康检查失败，检查日志:"
    journalctl -u auto-router --no-pager -n 20
    tail -20 "$LOG_DIR/app.log" 2>/dev/null
    exit 1
fi

echo ""
echo "=== 部署完成 ==="
echo "  启动: systemctl start auto-router"
echo "  停止: systemctl stop auto-router"
echo "  重启: systemctl restart auto-router"
echo "  状态: systemctl status auto-router"
echo "  日志: journalctl -u auto-router -f"
echo "  应用日志: tail -f /var/log/auto-router/app.log"
