FROM python:3.11-slim

# --- Змінні середовища ---
ENV PYTHONUNBUFFERED=1
WORKDIR /app

# --- Системні залежності ---
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    wget \
    git \
    && rm -rf /var/lib/apt/lists/*

# --- Встановлення yt-dlp ---
RUN pip install --no-cache-dir yt-dlp

# --- Копіюємо код додатку ---
COPY app.py .
COPY requirements.txt .

# --- Встановлення Python-залежностей ---
RUN pip install --no-cache-dir -r requirements.txt

# --- Відкриваємо порт для FastAPI ---
EXPOSE 8000

# --- Запуск FastAPI ---
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]

