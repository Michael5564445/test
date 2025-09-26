import os
import json
import requests
import yt_dlp
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pathlib import Path
from datetime import datetime
import uvicorn
import asyncio
import threading

# ========================
# ENVIRONMENT VARIABLES
# ========================
UPCOMING_PATH = Path(os.environ.get("UPCOMING_PATH", "/app/Upcoming Movies"))
LANGUAGE = os.environ.get("LANGUAGE", "uk")  # uk/en
JSON_FILE = Path(os.environ.get("JSON_FILE", "/app/upcoming_movies.json"))
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
RELEASE_TYPE_NUMBER = int(os.environ.get("RELEASE_TYPE_NUMBER", 5))  # 1–6
CHECK_INTERVAL = 7 * 24 * 3600  # раз на тиждень у секундах

# ========================
# INITIALIZATION
# ========================
app = FastAPI()
UPCOMING_PATH.mkdir(parents=True, exist_ok=True)
if not JSON_FILE.exists():
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump({}, f, indent=2, ensure_ascii=False)

def log(msg):
    print(f"[{datetime.now().isoformat()}] {msg}")

def load_upcoming():
    with open(JSON_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_upcoming(data):
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def sanitize_filename(name):
    return "".join(c if c.isalnum() or c in " ._-" else "_" for c in name)

# ========================
# TMDb FUNCTIONS
# ========================
def get_tmdb_movie(tmdb_id):
    url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={TMDB_API_KEY}&language={LANGUAGE}&append_to_response=release_dates,videos"
    resp = requests.get(url, timeout=10)
    if resp.status_code != 200:
        return None
    return resp.json()

RELEASE_TYPE_MAP = {
    1: "Premiere",
    2: "Theatrical (limited)",
    3: "Theatrical",
    4: "Digital",
    5: "Physical",
    6: "TV",
}

def get_us_release_date(tmdb_info, release_type_number=5):
    """Вибір дати релізу в США за типом релізу"""
    release_dates = tmdb_info.get("release_dates", {}).get("results", [])
    for country in release_dates:
        if country.get("iso_3166_1") != "US":
            continue
        for entry in country.get("release_dates", []):
            if entry.get("type") == release_type_number:
                return entry.get("release_date")
    return None

def download_poster(poster_path, dest_path):
    resp = requests.get(poster_path, stream=True, timeout=10)
    if resp.status_code == 200:
        with open(dest_path / "poster.jpg", "wb") as f:
            for chunk in resp.iter_content(1024):
                f.write(chunk)

def download_trailer(url, dest_path):
    """Завантажує трейлер в оригінальній якості"""
    ydl_opts = {
        "outtmpl": str(dest_path / "trailer.%(ext)s"),
        "format": "best",
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

# ========================
# PROCESS MOVIE
# ========================
def process_movie(movie):
    tmdb_id = str(movie.get("tmdbId"))
    title = movie.get("title")
    
    tmdb_info = get_tmdb_movie(tmdb_id)
    release_date_str = None
    if tmdb_info:
        release_date_str = get_us_release_date(tmdb_info, RELEASE_TYPE_NUMBER)
    
    # Оновлюємо JSON
    upcoming_data = load_upcoming()
    upcoming_data[tmdb_id] = {
        "title": title,
        "release_date": release_date_str,
        "folder_created": False
    }
    save_upcoming(upcoming_data)

    if not release_date_str:
        log(f"No release date for {title}, folder not created")
        return

    release_date = datetime.fromisoformat(release_date_str)
    now = datetime.now()
    if release_date <= now:
        log(f"{title} already released, skipping folder creation")
        return

    # Створюємо папку
    folder_name = sanitize_filename(f"{title} ({release_date.year})")
    movie_path = UPCOMING_PATH / folder_name
    movie_path.mkdir(parents=True, exist_ok=True)

    # Завантажуємо постер
    poster_url = f"https://image.tmdb.org/t/p/original{tmdb_info.get('poster_path')}"
    download_poster(poster_url, movie_path)

    # Завантажуємо трейлер мовою LANGUAGE
    videos = tmdb_info.get("videos", {}).get("results", [])
    trailer_url = None
    for v in videos:
        if v["type"] == "Trailer" and v["site"] == "YouTube" and v["iso_639_1"] == LANGUAGE:
            trailer_url = f"https://www.youtube.com/watch?v={v['key']}"
            break
    if trailer_url:
        download_trailer(trailer_url, movie_path)

    # Оновлюємо JSON про створення папки
    upcoming_data = load_upcoming()
    upcoming_data[tmdb_id]["folder_created"] = True
    save_upcoming(upcoming_data)

    log(f"Upcoming movie processed: {title}")

# ========================
# WEBHOOK HANDLER
# ========================
@app.post("/radarr/webhook")
async def radarr_webhook(request: Request):
    data = await request.json()
    log(f"Webhook received: {data}")

    event_type = data.get("eventType")
    movie = data.get("movie", {})
    tmdb_id = str(movie.get("tmdbId"))
    title = movie.get("title", "unknown")
    folder_name = sanitize_filename(f"{title} ({datetime.now().year})")
    movie_path = UPCOMING_PATH / folder_name

    if event_type in ["MovieDownloaded", "MovieDelete"]:
        # Видаляємо з JSON
        upcoming_data = load_upcoming()
        if tmdb_id in upcoming_data:
            del upcoming_data[tmdb_id]
            save_upcoming(upcoming_data)
            # Видаляємо папку
            if movie_path.exists() and movie_path.is_dir():
                for f in movie_path.iterdir():
                    f.unlink()
                movie_path.rmdir()
            log(f"Movie {title} downloaded or deleted – removed from upcoming")
        return JSONResponse({"status": "removed"})

    # Обробляємо новий фільм
    process_movie(movie)
    return JSONResponse({"status": "ok"})

# ========================
# WEEKLY CHECK
# ========================
async def weekly_check():
    while True:
        upcoming_data = load_upcoming()
        for tmdb_id, info in upcoming_data.items():
            if info.get("release_date") and not info.get("folder_created"):
                log(f"Weekly check: creating folder for {info['title']}")
                movie = {"tmdbId": tmdb_id, "title": info["title"]}
                process_movie(movie)
        await asyncio.sleep(CHECK_INTERVAL)

# ========================
# AUTO RUN
# ========================
if __name__ == "__main__":
    # Запускаємо щотижневу перевірку в окремому потоці
    threading.Thread(target=lambda: asyncio.run(weekly_check()), daemon=True).start()

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
