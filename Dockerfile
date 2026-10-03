# syntax=docker/dockerfile:1

# ---- Stage 1: 构建前端静态资源 ----
FROM node:20-alpine AS frontend-builder
WORKDIR /frontend

# 先装依赖，最大化 layer 缓存命中
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

# 再拷源码构建（frontend/node_modules 与 dist 已在 .dockerignore 中排除）
COPY frontend/ ./
RUN npm run build

# ---- Stage 2: Python 运行时（同时服务 API 与前端静态文件）----
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TRANSLATION_SERVER_RELOAD=0

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends pandoc && rm -rf /var/lib/apt/lists/*
# PDF 转换的模型依赖较大，可按需构建；网站只开放已安装的格式。
ARG ENABLE_PDF=0

# 仅安装运行期依赖
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
RUN if [ "$ENABLE_PDF" = "1" ]; then pip install --no-cache-dir marker-pdf; fi

# 应用源码与配置
COPY src/ ./src/
COPY config/ ./config/
COPY translate.py run_server.py run_worker.py ./

# 术语表随镜像发货，作为空数据卷的初始种子
COPY data/glossaries/ ./data/glossaries/

# 前端构建产物：FastAPI 会挂载 frontend/dist 到根路径
COPY --from=frontend-builder /frontend/dist ./frontend/dist

# 运行期目录（首次挂载空卷时会以这些空目录作为种子）
RUN mkdir -p data/uploads data/results data/output data/temp logs

EXPOSE 8000

# 默认起 API；worker 由 compose 用同一镜像覆盖 command 启动
CMD ["python", "run_server.py"]
