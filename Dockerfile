FROM python:3.11-slim

WORKDIR /app

# 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    default-libmysqlclient-dev \
    pkg-config \
 && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖（优先复制 requirements.txt，利用 Docker 层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制源码
COPY src/ ./src/
COPY project_configs/ ./project_configs/
COPY main.py .

EXPOSE 8080

CMD ["uvicorn", "src.webhook.main:app", "--host", "0.0.0.0", "--port", "8080"]
