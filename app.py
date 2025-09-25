import os
import json
import asyncio
import shutil
from pathlib import Path
import logging
import yaml
import aiohttp
from fastapi import FastAPI, Request
from datetime import datetime
import subprocess

app = FastAPI()

# ---------------------------
# Змінні з docker-compose
# ---------------------------
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
LANGUAGE = os.getenv("LANGUAGE", "uk")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 604800))
FRAME_PATH = os.getenv("FRAME_PATH", "/overlays/red_frame.png")
MOVIES_FILE = Path(os.getenv("MOVIES_FILE", "/movies/movies.json"))
OVERLAY_FILE = Path(os.getenv("OVERLAY_FILE", "/overlays/upcoming_overlays.yml"))
UPCOMING_DIR = Path(os.getenv("UPCOMING_DIR", "/Upcoming Movies"))

# ---------------------------
# Логи
# ---------------------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

# ---------------------------
# Допоміжні функції
# ---------------------------
def ensure_files():
    UPCOMING_DIR.mkdir(parents=True, exist_ok=True)
    MOVIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not MOVIES_FILE.exists():
        MOVIES_FILE.write_text("[]", encoding="utf-8")
    OVERLAY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not OVERLAY_FILE.exists():
        default_yaml = {"overlays": {}, "templates": {
            "ExpectedRelease": {
                "overlay": FRAME_PATH,
                "builder": "text",
                "text": "<<release_date>>",
                "horizontal_offset": 0,
                "vertical_offset": 0,
                "font": "Arial",
                "font_color": "#FFFFFF",
                "font_size": 42,
                "back_color": "#000000AA"
            }
        }}
        with open(OVERLAY_FILE, "w", encoding="utf-8") as f:
            yaml.safe_dump(default_yaml, f, allow_unicode=True, sort_keys=False)

async def fetch_tmdb_physical_date(tmdb_id):
    url = f"https://api.themoviedb.org/3/movie/{tmdb_id}/release_dates?api_key={TMDB_API_KEY}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                logging.warning(f"TMDb API error {resp.status} for movie {tmdb_id}")
                return None
            data = await resp.json()
    # шукаємо physical release
    for country_data in data.get("results", []):
        for release in country_data.get("release_dates", []):
            if release.get("type") == 5:  # Physical
                return release.get("release_date")
    return None

async def download_poster(tmdb_id, dest_file):
    url = f"https://image.tmdb.org/t/p/original"
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{url}/w500/{tmdb_id}.jpg") as resp:
            if resp.status == 200:
                with open(dest_file, "wb") as f:
                    f.write(await resp.read())

async def download_trailer(tmdb_id, dest_file):
    url = f"https://api.themoviedb.org/3/movie/{tmdb_id}/videos?api_key={TMDB_API_KEY}&language={LANGUAGE}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                logging.warning(f"TMDb API error {resp.status} for trailer {tmdb_id}")
                return
            data = await resp.json()
    # беремо перший YouTube трейлер
    for video in data.get("results", []):
        if video.get("site") == "YouTube" and video.get("type") == "Trailer":
            youtube_url = f"https://www.youtube.com/watch?v={video.get('key')}"
            subprocess.run(["yt-dlp", "-o", str(dest_file), youtube_url])
            return

def add_overlay(movie_id, release_date):
    if OVERLAY_FILE.exists():
        with open(OVERLAY_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    else:
        data = {"overlays": {}, "templates": {}}
    key = f"movie_{movie_id}_expected"
    data["overlays"][key] = {
        "template": {"name": "ExpectedRelease", "release_date": release_date},
        "tmdb_id": movie_id
    }
    with open(OVERLAY_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

def create_upcoming_folder(title, year, poster_file, trailer_file, movie_id, release_date):
    folder_name = f"{title} ({year})"
    folder = UPCOMING_DIR / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    if poster_file.exists():
        shutil.copy2(poster_file, folder / "poster.jpg")
    if trailer_file.exists():
        shutil.copy2(trailer_file, folder / trailer_file.name)
    add_overlay(movie_id, release_date)
    logging.info(f"Upcoming folder created: {folder}")

# ---------------------------
# Перевірка файлу раз на тиждень
# ---------------------------
async def scheduled_check():
    while True:
        if MOVIES_FILE.exists():
            with open(MOVIES_FILE, "r", encoding="utf-8") as f:
                movies = json.load(f)
            updated = []
            for m in movies:
                movie_id = m.get("folder_id")
                release_date = await fetch_tmdb_physical_date(movie_id)
                if release_date:
                    # створюємо папку і файли
                    folder_name = m["folder_name"]
                    poster_file = UPCOMING_DIR / f"{folder_name}_poster.jpg"
                    trailer_file = UPCOMING_DIR / f"{folder_name}_trailer.mp4"
                    await download_trailer(movie_id, trailer_file)
                    await download_poster(movie_id, poster_file)
                    create_upcoming_folder(m["title"], m["year"], poster_file, trailer_file, movie_id, release_date)
                else:
                    updated.append(m)
            with open(MOVIES_FILE, "w", encoding="utf-8") as f:
                json.dump(updated, f, ensure_ascii=False, indent=2)
        await asyncio.sleep(CHECK_INTERVAL)

# ---------------------------
# FastAPI стартап
# ---------------------------
@app.on_event("startup")
async def startup_event():
    ensure_files()
    asyncio.create_task(scheduled_check())
    logging.info("Startup complete. Scheduled task running.")

# ---------------------------
# Webhook Radarr
# ---------------------------
@app.post("/radarr-webhook")
async def radarr_webhook(req: Request):
    payload = await req.json()
    logging.info(f"Webhook received: {json.dumps(payload, ensure_ascii=False, indent=2)}")
    
    event_type = payload.get("eventType")
    movie = payload.get("movie", {})
    folder_name = movie.get("folderName")
    movie_id = movie.get("tmdbId")
    title = movie.get("title")
    year = movie.get("year")

    if event_type == "MovieAdded":
        release_date = await fetch_tmdb_physical_date(movie_id)
        if release_date:
            poster_file = UPCOMING_DIR / f"{folder_name}_poster.jpg"
            trailer_file = UPCOMING_DIR / f"{folder_name}_trailer.mp4"
            await download_trailer(movie_id, trailer_file)
            await download_poster(movie_id, poster_file)
            create_upcoming_folder(title, year, poster_file, trailer_file, movie_id, release_date)
        else:
            # додати до JSON для подальшої перевірки
            with open(MOVIES_FILE, "r", encoding="utf-8") as f:
                movies = json.load(f)
            if not any(m.get("folder_id") == movie_id for m in movies):
                movies.append({
                    "folder_name": folder_name,
                    "folder_id": movie_id,
                    "title": title,
                    "year": year
                })
            with open(MOVIES_FILE, "w", encoding="utf-8") as f:
                json.dump(movies, f, ensure_ascii=False, indent=2)
        return {"status": "processed_added"}

    elif event_type == "MovieDownloaded":
        upcoming_folder = UPCOMING_DIR / f"{title} ({year})"
        if upcoming_folder.exists():
            shutil.rmtree(upcoming_folder)
        # видалити з JSON
        if MOVIES_FILE.exists():
            with open(MOVIES_FILE, "r", encoding="utf-8") as f:
                movies = json.load(f)
            movies = [m for m in movies if m.get("folder_id") != movie_id]
            with open(MOVIES_FILE, "w", encoding="utf-8") as f:
                json.dump(movies, f, ensure_ascii=False, indent=2)
        # видалити з YAML
        if OVERLAY_FILE.exists():
            with open(OVERLAY_FILE, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            key = f"movie_{movie_id}_expected"
            if "overlays" in data and key in data["overlays"]:
                del data["overlays"][key]
                with open(OVERLAY_FILE, "w", encoding="utf-8") as f:
                    yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        return {"status": "processed_downloaded"}

    return {"status": "ignored"}

# ---------------------------
# Healthcheck
# ---------------------------
@app.get("/health")
async def health():
    return {"status": "ok"}
