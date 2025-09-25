import os
import json
import yaml
import requests
import shutil
from pathlib import Path
from fastapi import FastAPI, Request
from threading import Thread
from datetime import datetime, timedelta
import time
import subprocess

# --- Завантаження конфігу ---
CONFIG_FILE = Path("/app/config/config.yml")
with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

UPCOMING_FOLDER = Path(config['radarr']['upcoming_folder'])
MISSING_JSON = Path(config['radarr']['json_file'])
KOMETA_YAML = Path(config['radarr']['yaml_file'])
LANG = config['radarr']['language']
CHECK_INTERVAL_DAYS = config['radarr']['check_interval_days']

# --- FastAPI ---
app = FastAPI()

# --- Допоміжні функції ---
def sanitize_folder_name(name):
    invalid = r'<>:"/\|?*'
    for c in invalid:
        name = name.replace(c, '-')
    return name.strip()

def download_file(url, dest_path):
    try:
        subprocess.run(['yt-dlp', '-o', str(dest_path), url], check=True)
    except Exception as e:
        print(f"Error downloading {url}: {e}")

def load_missing_dates():
    if not MISSING_JSON.exists():
        return {}
    with open(MISSING_JSON, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_missing_dates(data):
    with open(MISSING_JSON, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_kometa_yaml():
    if not KOMETA_YAML.exists():
        return {}
    with open(KOMETA_YAML, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f) or {}

def save_kometa_yaml(data):
    with open(KOMETA_YAML, 'w', encoding='utf-8') as f:
        yaml.dump(data, f, allow_unicode=True)

def create_movie_folder(movie_title, year):
    folder_name = sanitize_folder_name(f"{movie_title} ({year})")
    folder_path = UPCOMING_FOLDER / folder_name
    folder_path.mkdir(parents=True, exist_ok=True)
    return folder_path

def add_to_kometa(movie_title, release_date):
    data = load_kometa_yaml()
    data[movie_title] = release_date
    save_kometa_yaml(data)

def remove_movie(movie_title):
    folder_name = sanitize_folder_name(movie_title)
    folder_path = UPCOMING_FOLDER / folder_name
    if folder_path.exists():
        shutil.rmtree(folder_path)
    # Remove from missing JSON
    missing = load_missing_dates()
    missing.pop(movie_title, None)
    save_missing_dates(missing)
    # Remove from Kometa YAML
    kometa = load_kometa_yaml()
    kometa.pop(movie_title, None)
    save_kometa_yaml(kometa)

def process_movie(movie):
    """
    movie: dict з Radarr webhook
    Keys: title, year, physicalRelease, trailer_url, poster_url
    """
    title = movie['title']
    year = movie.get('year', '')
    release_date = movie.get('physicalRelease')
    trailer_url = movie.get('trailer_url')
    poster_url = movie.get('poster_url')

    now = datetime.utcnow().date()
    release_dt = datetime.fromisoformat(release_date).date() if release_date else None

    if release_dt and release_dt > now:
        # Створюємо папку і завантажуємо трейлер + постер
        folder_path = create_movie_folder(title, year)
        if trailer_url:
            download_file(trailer_url, folder_path / f"trailer.{LANG}.mp4")
        if poster_url:
            download_file(poster_url, folder_path / f"poster.{LANG}.jpg")
        add_to_kometa(title, release_dt.isoformat())
    else:
        # Додаємо у missing_dates
        missing = load_missing_dates()
        missing[title] = {
            'year': year,
            'trailer_url': trailer_url,
            'poster_url': poster_url
        }
        save_missing_dates(missing)

def weekly_check():
    while True:
        missing = load_missing_dates()
        to_remove = []
        for title, info in missing.items():
            release_date = info.get('physicalRelease')
            trailer_url = info.get('trailer_url')
            poster_url = info.get('poster_url')
            if release_date:
                folder_path = create_movie_folder(title, info.get('year',''))
                if trailer_url:
                    download_file(trailer_url, folder_path / f"trailer.{LANG}.mp4")
                if poster_url:
                    download_file(poster_url, folder_path / f"poster.{LANG}.jpg")
                add_to_kometa(title, release_date)
                to_remove.append(title)
        # Видаляємо з missing_dates ті, що вже обробили
        for t in to_remove:
            missing.pop(t, None)
        save_missing_dates(missing)
        time.sleep(CHECK_INTERVAL_DAYS * 86400)  # Інтервал у днях

# --- Вебхук Radarr ---
@app.post("/radarr/webhook")
async def radarr_webhook(req: Request):
    payload = await req.json()
    event_type = payload.get('eventType')
    movie = payload.get('movie', {})
    if event_type == 'MovieAdded':
        process_movie(movie)
    elif event_type == 'MovieDownloaded':
        remove_movie(movie.get('title'))
    return {"status": "ok"}

# --- Запуск планувальника в окремому потоці ---
thread = Thread(target=weekly_check, daemon=True)
thread.start()

# --- FastAPI запуск ---
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=config['radarr']['webhook_port'])
