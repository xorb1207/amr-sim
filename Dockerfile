# syntax=docker/dockerfile:1
# 멀티 스테이지: 폐쇄망에서 `docker load` 후 동일 태그로 재현 가능하도록 베이스 이미지 버전 고정

# ─── 프론트엔드 빌드 ─────────────────────────────────────────
FROM node:22.12.0-alpine AS frontend-build
WORKDIR /build
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci
COPY dashboard/ ./
ENV VITE_API_URL=
RUN npm run build

# ─── 백엔드 (FastAPI + 시뮬레이터) ────────────────────────────
FROM python:3.12.8-slim-bookworm AS backend
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
COPY venv/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY venv/main.py venv/simulation.py venv/wave_map.py ./
COPY venv/map.json ./map.json
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

# ─── 정적 배포 (nginx) ───────────────────────────────────────
FROM nginx:1.27.3-alpine AS frontend
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=frontend-build /build/dist /usr/share/nginx/html
EXPOSE 80
