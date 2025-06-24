FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 复制需求文件
COPY requirements.txt .

# 安装依赖
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用文件
COPY . .

# 创建数据目录
RUN mkdir -p /app/data

# 设置环境变量
ENV PYTHONUNBUFFERED=1

# 运行应用
CMD ["python", "telegram_forward_bot.py"]