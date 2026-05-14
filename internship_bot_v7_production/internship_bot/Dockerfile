# ── Dockerfile ───────────────────────────────────────────────────────────────
FROM python:3.11-slim

# System deps for Playwright + WeasyPrint
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl git \
    libpango-1.0-0 libpangocairo-1.0-0 \
    libcairo2 libgdk-pixbuf2.0-0 \
    libffi-dev libjpeg-dev \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright + Chromium
RUN playwright install chromium
RUN playwright install-deps chromium

# Download NLTK data
RUN python -c "import nltk; nltk.download('punkt'); nltk.download('stopwords')"

# Download spaCy model
RUN python -m spacy download en_core_web_sm

COPY . .

# Default: run the CLI (override in docker-compose for other services)
ENTRYPOINT ["python", "scripts/run_pipeline.py"]
