#!/bin/bash

# Telegram转发机器人安装脚本

echo "🚀 开始安装 Telegram 转发机器人..."

# 检查Python版本
python_version=$(python3 --version 2>&1 | awk '{print $2}' | cut -d. -f1,2)
required_version="3.8"

if [ "$(printf '%s\n' "$required_version" "$python_version" | sort -V | head -n1)" != "$required_version" ]; then
    echo "❌ 需要 Python 3.8 或更高版本，当前版本: $python_version"
    exit 1
fi

# 创建虚拟环境
echo "📦 创建虚拟环境..."
python3 -m venv venv
source venv/bin/activate

# 安装依赖
echo "📥 安装依赖包..."
pip install -r requirements.txt

# 创建必要目录
echo "📁 创建目录结构..."
mkdir -p data logs backups

# 设置权限
chmod +x telegram_forward_bot.py

echo "✅ 安装完成！"
echo ""
echo "📝 下一步操作："
echo "1. 设置环境变量: export TELEGRAM_BOT_TOKEN='你的机器人token'"
echo "2. 运行机器人: python telegram_forward_bot.py"
echo "3. 使用 /start 命令开始配置"
echo ""
echo "📚 更多帮助请查看 README.md"