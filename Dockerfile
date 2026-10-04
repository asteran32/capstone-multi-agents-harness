FROM node:22-bookworm-slim AS frontend
WORKDIR /web
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /workspace
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend/ ./backend/
COPY data/ ./data/
COPY policies/ ./policies/
COPY scripts/ ./scripts/
COPY --from=frontend /web/dist ./frontend/dist
ENV PYTHONPATH=/workspace/backend PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /workspace/backend
RUN useradd --uid 10001 --no-create-home app
USER app
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
