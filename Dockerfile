FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PORT=7860

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/

# Install CPU-only PyTorch first to reduce Docker image size
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# Install project requirements
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code and pre-built vector storage
COPY backend ./backend
COPY frontend ./frontend

EXPOSE 7860

CMD ["sh", "-c", "python -m uvicorn backend.server:app --host 0.0.0.0 --port ${PORT}"]
