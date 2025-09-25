FROM python:3.11-slim

# --- Змінні середовища для локального користування ---
ENV PYTHONUNBUFFERED=1
WORKDIR /app

# --- Встановлюємо залежності системні ---
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    wget \
    git \
    && rm -rf /var/lib/apt/lists/*

# --- Встановлюємо yt-dlp ---
RUN pip install --no-cache-dir yt-dlp

# --- Копіюємо файли додатку ---
COPY app.py .
COPY requirements.txt .

# --- Встановлюємо Python-залежності ---
RUN pip install --no-cache-dir -r requirements.txt

# --- Порт FastAPI ---
EXPOSE 8000

# --- Запуск FastAPI ---
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
