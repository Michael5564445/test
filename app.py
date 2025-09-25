import asyncio
import json
import os
import shutil
from pathlib import Path
import logging
import aiohttp
import yaml
from fastapi import FastAPI, Request

app = FastAPI()

# --- Налаштування через змінні середовища ---
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
LANGUAGE = os.getenv("LANGUAGE", "uk")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 604800))  # 1 тиждень
FRAME_PATH = os.getenv("FRAME_PATH")
MOVIES_FILE = Path(os.getenv("MOVIES_FILE", "movies.json"))
OVERLAY_FILE = Path(os.getenv("OVERLAY_FILE", "upcoming_overlays.yml"))
UPCOMING_DIR = Path(os.getenv("UPCOMING_DIR", "Upcoming Movies"))

# --- Логування ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("upcoming_movies")

# --- Створюємо потрібні папки/файли якщо відсутні ---
UPCOMING_DIR.mkdir(parents=True, exist_ok=True)
MOVIES_FILE.parent.mkdir(parents=True, exist_ok=True)
if not MOVIES_FILE.exists():
    MOVIES_FILE.write_text("[]", encoding="utf-8")
if not OVERLAY_FILE.exists():
    OVERLAY_FILE.write_text(json.dumps({"overlays": {}, "templates": {}}), encoding="utf-8")

# --- Helper ---
async def fetch_tmdb_movie(tmdb_id):
    url = f"https://api.themoviedb.org/3/movie/{tmdb_id}"
    params = {"api_key": TMDB_API_KEY, "language": LANGUAGE, "append_to_response": "videos,release_dates"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as r:
            if r.status != 200:
                logger.warning(f"TMDb API error {r.status} for movie {tmdb_id}")
                return None
            return await r.json()

async def download_file(url, dest_path):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as r:
            if r.status == 200:
                with open(dest_path, "wb") as f:
                    f.write(await r.read())
                return True
    return False

async def download_trailer(youtube_url, dest_file):
    import subprocess
    subprocess.run(["yt-dlp", "-o", str(dest_file), youtube_url])

def add_overlay(movie_id, release_date):
    if OVERLAY_FILE.exists():
        with open(OVERLAY_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {"overlays": {}, "templates": {}}
    else:
        data = {"overlays": {}, "templates": {}}

    if "templates" not in data or "ExpectedRelease" not in data["templates"]:
        data["templates"] = {
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
        }

    key = f"movie_{movie_id}_expected"
    data["overlays"][key] = {
        "template": {"name": "ExpectedRelease", "release_date": release_date},
        "tmdb_id": movie_id
    }

    with open(OVERLAY_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

def remove_overlay(movie_id):
    if OVERLAY_FILE.exists():
        with open(OVERLAY_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        key = f"movie_{movie_id}_expected"
        if "overlays" in data and key in data["overlays"]:
            del data["overlays"][key]
            with open(OVERLAY_FILE, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

def create_upcoming_folder(title, poster_file, trailer_file, movie_id, release_date):
    folder_name = f"{title}"
    folder = UPCOMING_DIR / folder_name
    folder.mkdir(parents=True, exist_ok=True)

    if poster_file.exists():
        shutil.copy2(poster_file, folder / "poster.jpg")
    if trailer_file.exists():
        shutil.copy2(trailer_file, folder / trailer_file.name)

    add_overlay(movie_id, release_date)
    return folder

def remove_upcoming_folder(title):
    folder = UPCOMING_DIR / title
    if folder.exists():
        shutil.rmtree(folder)

async def handle_movie_added(movie):
    tmdb_id = movie.get("tmdbId")
    folder_path = Path(movie.get("folderPath", UPCOMING_DIR / movie.get("title", "Unknown")))
    title = movie.get("title")

    tmdb_data = await fetch_tmdb_movie(tmdb_id)
    if not tmdb_data:
        return

    # Шукаємо фізичний реліз
    physical_release = None
    for rel in tmdb_data.get("release_dates", {}).get("results", []):
        if rel.get("iso_3166_1") == "US":  # можна змінити на потрібну країну
            for r in rel.get("release_dates", []):
                if r.get("type") == 5:  # Physical
                    physical_release = r.get("release_date")
                    break
    if not physical_release:
        # Додаємо у JSON для відстеження
        with open(MOVIES_FILE, "r", encoding="utf-8") as f:
            movies_list = json.load(f)
        if not any(m.get("tmdbId") == tmdb_id for m in movies_list):
            movies_list.append({
                "title": title,
                "folderPath": str(folder_path),
                "tmdbId": tmdb_id
            })
            with open(MOVIES_FILE, "w", encoding="utf-8") as f:
                json.dump(movies_list, f, indent=2, ensure_ascii=False)
        return

    # Завантажуємо постер
    poster_path = tmdb_data.get("poster_path")
    poster_file = folder_path / f"poster_{LANGUAGE}.jpg"
    if poster_path:
        await download_file(f"https://image.tmdb.org/t/p/original{poster_path}", poster_file)

    # Завантажуємо трейлер
    videos = tmdb_data.get("videos", {}).get("results", [])
    trailer_url = None
    for v in videos:
        if v["type"] == "Trailer" and v["site"] == "YouTube" and v.get("iso_639_1") == LANGUAGE:
            trailer_url = f"https://www.youtube.com/watch?v={v['key']}"
            break
    trailer_file = folder_path / f"trailer_{LANGUAGE}.mp4"
    if trailer_url:
        await download_trailer(trailer_url, trailer_file)

    # Створюємо папку і оверлей
    create_upcoming_folder(title, poster_file, trailer_file, tmdb_id, physical_release)

    # Після додавання оверлею видаляємо з JSON
    if MOVIES_FILE.exists():
        with open(MOVIES_FILE, "r", encoding="utf-8") as f:
            movies_list = json.load(f)
        movies_list = [m for m in movies_list if m.get("tmdbId") != tmdb_id]
        with open(MOVIES_FILE, "w", encoding="utf-8") as f:
            json.dump(movies_list, f, indent=2, ensure_ascii=False)

async def scheduled_check():
    while True:
        if MOVIES_FILE.exists():
            with open(MOVIES_FILE, "r", encoding="utf-8") as f:
                movies = json.load(f)
            for m in movies:
                try:
                    await handle_movie_added(m)
                except Exception as e:
                    logger.error(f"Error handling scheduled movie {m.get('title')}: {e}")
        await asyncio.sleep(CHECK_INTERVAL)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(scheduled_check())
    logger.info("Startup complete. Scheduled task running.")

@app.post("/radarr-webhook")
async def radarr_webhook(req: Request):
    data = await req.json()
    logger.info(f"Webhook received: {json.dumps(data, indent=2)}")
    event_type = data.get("eventType")
    movie = data.get("movie", {})

    tmdb_id = movie.get("tmdbId")
    title = movie.get("title")

    if event_type == "MovieAdded":
        await handle_movie_added(movie)
        return {"status": "processed_added"}

    elif event_type in ["MovieDownloaded", "MovieDelete"]:
        remove_upcoming_folder(title)
        remove_overlay(tmdb_id)
        # Видалення з JSON, якщо ще залишилось
        if MOVIES_FILE.exists():
            with open(MOVIES_FILE, "r", encoding="utf-8") as f:
                movies_list = json.load(f)
            movies_list = [m for m in movies_list if m.get("tmdbId") != tmdb_id]
            with open(MOVIES_FILE, "w", encoding="utf-8") as f:
                json.dump(movies_list, f, indent=2, ensure_ascii=False)
        return {"status": "processed_removed"}

    return {"status": "ignored"}
