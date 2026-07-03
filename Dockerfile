FROM python:3.11-slim

WORKDIR /app

# 安装依赖（先复制 requirements 利用 Docker 层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY server.py .
COPY skill.md .
COPY static/ ./static/

# 数据目录（SQLite 文件存放在此，挂载 volume 保证持久化）
RUN mkdir -p /app/data

EXPOSE 8765

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8765"]
