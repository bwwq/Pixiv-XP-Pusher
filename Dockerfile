FROM python:3.11-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY . .

# 创建数据目录
RUN mkdir -p /app/data /app/logs

# 创建启动脚本：同时运行 Web UI 和主程序
RUN echo '#!/bin/bash\n\
# 后台启动 Web UI\n\
uvicorn web.app:app --host 0.0.0.0 --port 8000 &\n\
\n\
# 前台启动主程序（调度器模式）\n\
exec python main.py --now' > /app/start.sh \
    && chmod +x /app/start.sh

# 暴露 Web UI 端口
EXPOSE 8000

# 启动所有服务
CMD ["/bin/bash", "/app/start.sh"]
