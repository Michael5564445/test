import os
import json
import aiohttp
import asyncio
from fastapi import FastAPI, Request
from pathlib import Path
from datetime import datetime
import uvicorn
import yt_dlp
import shutil

# ========================
# ENVIRONMENT VARIABLES
# ========================
TMDB_API_KEY = os.environ.get("TMDB_API_KEY")
UPCOMING_PATH = Path(os.environ.get("UPCOMING_PATH", "./Upcoming"))
JSON_FILE = Path(os.environ.get("JSON_FILE", "./upcoming_movies.json"))
LANGUAGE = os.environ.get("LANGUAGE", "en")   # використовується для постера і трейлера
UPDATE_INTERVAL = int(os.environ.get("UPDATE_INTERVAL", 24*3600))  # дефолт 24 години
RELEASE_TYPE = int(os.environ.get("RELEASE_TYPE", 5))  # тип релізу (default Physical)

# ========================
# INIT
# ========================
app = FastAPI()
UPCOMING_PATH.mkdir(parents=True, exist_ok=True)
if not JSON_FILE.exists():
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump({}, f)

# ========================
# UTILS
# ========================
def load_upcoming():
    if JSON_FILE.exists():
        with JSON_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_upcoming(data):
    with JSON_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def log(msg):
    print(f"[{datetime.now()}] {msg}", flush=True)

async def fetch_tmdb_release(tmdb_id):
    """Шукаємо реліз у США за типом RELEASE_TYPE"""
    url = f"https://api.themoviedb.org/3/movie/{tmdb_id}/release_dates?api_key={TMDB_API_KEY}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
    for country in data.get("results", []):
        if country["iso_3166_1"] == "US":
            for rd in country["release_dates"]:
                if rd["type"] == RELEASE_TYPE:
                    return rd.get("release_date")
    return None

async def download_file(url, path):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status == 200:
                with open(path, "wb") as f:
                    f.write(await resp.read())

async def fetch_poster_and_trailer(tmdb_id, folder_path):
    folder_path.mkdir(parents=True, exist_ok=True)

    # ---- POSTER ----
    url_info = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={TMDB_API_KEY}&language={LANGUAGE}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url_info) as resp:
            info = await resp.json()
            poster_path = info.get("poster_path")
            if poster_path:
                poster_url = f"https://image.tmdb.org/t/p/original{poster_path}"
                await download_file(poster_url, folder_path / "poster.jpg")

    # ---- TRAILER ----
    trailer_api = f"https://api.themoviedb.org/3/movie/{tmdb_id}/videos?api_key={TMDB_API_KEY}&language={LANGUAGE}"
    async with aiohttp.ClientSession() as session:
        async with session.get(trailer_api) as resp:
            data = await resp.json()
            trailer = next((v for v in data.get("results", []) if v["type"].lower() == "trailer" and v["site"] == "YouTube"), None)
            if trailer:
                trailer_url = f"https://www.youtube.com/watch?v={trailer['key']}"
                trailer_path = folder_path / "trailer.mp4"
                ydl_opts = {"outtmpl": str(trailer_path)}
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([trailer_url])

# ========================
# PROCESS MOVIE
# ========================
async def process_movie(movie):
    tmdb_id = str(movie["tmdbId"])
    title = movie["title"]

    release_date = await fetch_tmdb_release(tmdb_id)

    upcoming_data = load_upcoming()
    upcoming_data[tmdb_id] = {
        "title": title,
        "release_date": release_date,
        "language": LANGUAGE,
    }
    save_upcoming(upcoming_data)

    if not release_date:
        log(f"Немає дати релізу для {title}, лише збережено у json")
        return

    release_date = datetime.fromisoformat(release_date.replace("Z", "+00:00"))
    now = datetime.now(datetime.utcnow().astimezone().tzinfo)
    if release_date <= now:
        log(f"{title} вже вийшов — пропускаю")
        return

    folder_name = f"{title} ({release_date.year})"
    folder_path = UPCOMING_PATH / folder_name
    await fetch_poster_and_trailer(tmdb_id, folder_path)

    log(f"Фільм {title} оброблено, постер і трейлер збережено мовою {LANGUAGE}")

# ========================
# WEBHOOK HANDLER
# ========================
@app.post("/radarr/webhook")
async def radarr_webhook(request: Request):
    data = await request.json()
    log(f"Webhook отримано: {data}")

    movie = data.get("movie", {})
    tmdb_id = str(movie.get("tmdbId"))
    title = movie.get("title")

    event_type = data.get("eventType")

    # якщо файл завантажено або видалено
    if event_type in ("Download", "MovieDelete", "MovieFileDelete"):
        upcoming_data = load_upcoming()
        if tmdb_id in upcoming_data:
            del upcoming_data[tmdb_id]
            save_upcoming(upcoming_data)

        # видаляємо папку
        for folder in UPCOMING_PATH.iterdir():
            if folder.is_dir() and folder.name.startswith(title):
                shutil.rmtree(folder, ignore_errors=True)
                log(f"Видалено {title} з json і папки")
        return {"status": "removed"}

    # інакше додаємо фільм
    if event_type == "MovieAdded":
        await process_movie(movie)
        return {"status": "added"}

    return {"status": "ignored"}

# ========================
# BACKGROUND TASK: перевірка дат раз у інтервал
# ========================
async def periodic_update():
    while True:
        upcoming_data = load_upcoming()
        for tmdb_id, entry in list(upcoming_data.items()):
            release_date = await fetch_tmdb_release(tmdb_id)
            if release_date:
                entry["release_date"] = release_date
                log(f"Оновлено дату для {entry['title']}: {release_date}")
        save_upcoming(upcoming_data)
        await asyncio.sleep(UPDATE_INTERVAL)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(periodic_update())

# ========================
# RUN
# ========================
if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
