import asyncio
import json
import os
import shutil
from pathlib import Path
import logging
import aiohttp
import yaml
from fastapi import FastAPI, Request
import yt_dlp  # Для завантаження YouTube трейлерів

app = FastAPI()

TMDB_API_KEY = os.getenv("TMDB_API_KEY")
LANGUAGE = os.getenv("LANGUAGE", "uk")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 604800))  # 1 тиждень
FRAME_PATH = os.getenv("FRAME_PATH")
MOVIES_FILE = Path(os.getenv("MOVIES_FILE", "movies.json"))
OVERLAY_FILE = Path(os.getenv("OVERLAY_FILE", "upcoming_overlays.yml"))
UPCOMING_DIR = Path(os.getenv("UPCOMING_DIR", "Upcoming Movies"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("upcoming_movies")

UPCOMING_DIR.mkdir(parents=True, exist_ok=True)
MOVIES_FILE.parent.mkdir(parents=True, exist_ok=True)
if not MOVIES_FILE.exists():
    MOVIES_FILE.write_text("[]", encoding="utf-8")
if not OVERLAY_FILE.exists():
    OVERLAY_FILE.write_text(json.dumps({"overlays": {}, "templates": {}}), encoding="utf-8")

# --- Fetch TMDb ---
async def fetch_tmdb_movie(tmdb_id):
    url = f"https://api.themoviedb.org/3/movie/{tmdb_id}"
    params = {"api_key": TMDB_API_KEY, "language": LANGUAGE, "append_to_response": "videos,release_dates,images"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as r:
            if r.status != 200:
                logger.warning(f"TMDb API error {r.status} for movie {tmdb_id}")
                return None
            return await r.json()

# --- Fetch Blu-ray date ---
async def fetch_bluray_date(title_eng):
    from bs4 import BeautifulSoup

    search_url = f"https://www.blu-ray.com/search/?quicksearch={title_eng.replace(' ', '+')}"
    async with aiohttp.ClientSession() as session:
        async with session.get(search_url) as r:
            if r.status != 200:
                return None
            html = await r.text()
    soup = BeautifulSoup(html, "html.parser")
    link = soup.select_one("td.searchTitle a")
    if not link:
        return None
    movie_url = "https://www.blu-ray.com" + link["href"]
    async with aiohttp.ClientSession() as session:
        async with session.get(movie_url) as r:
            if r.status != 200:
                return None
            html = await r.text()
    soup = BeautifulSoup(html, "html.parser")
    date_elem = soup.select_one("td.releaseDate")
    if date_elem:
        return date_elem.text.strip()
    return None

# --- Overlay functions ---
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

# --- Create / Remove upcoming folder ---
async def download_file(url, path):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as r:
            if r.status == 200:
                content = await r.read()
                with open(path, "wb") as f:
                    f.write(content)

async def create_upcoming_folder(title, poster_url=None, trailer_url=None, trailer_name="trailer.mp4"):
    folder = UPCOMING_DIR / title
    folder.mkdir(parents=True, exist_ok=True)

    if poster_url:
        await download_file(poster_url, folder / "poster.jpg")

    if trailer_url:
        ydl_opts = {
            "outtmpl": str(folder / trailer_name),
            "format": "mp4",
            "quiet": True,
            "merge_output_format": "mp4",
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([trailer_url])

    return folder

def remove_upcoming_folder(title):
    folder = UPCOMING_DIR / title
    if folder.exists():
        shutil.rmtree(folder)

# --- Handle MovieAdded ---
async def handle_movie_added(movie):
    tmdb_id = movie.get("tmdbId")
    title = movie.get("title")

    tmdb_data = await fetch_tmdb_movie(tmdb_id)
    if not tmdb_data:
        return

    title_eng = tmdb_data.get("original_title", title)
    bluray_date = await fetch_bluray_date(title_eng) or "TBD"

    add_overlay(tmdb_id, bluray_date)

    poster_path = tmdb_data.get("poster_path")
    poster_url = f"https://image.tmdb.org/t/p/original{poster_path}" if poster_path else None

    trailer_url = None
    for v in tmdb_data.get("videos", {}).get("results", []):
        if v.get("iso_639_1") == "uk" and v.get("site") == "YouTube" and v.get("type") == "Trailer":
            trailer_url = f"https://www.youtube.com/watch?v={v['key']}"
            break

    await create_upcoming_folder(title, poster_url, trailer_url)

    with open(MOVIES_FILE, "r", encoding="utf-8") as f:
        movies_list = json.load(f)
    if not any(m.get("tmdbId") == tmdb_id for m in movies_list):
        movies_list.append({"title": title, "tmdbId": tmdb_id})
        with open(MOVIES_FILE, "w", encoding="utf-8") as f:
            json.dump(movies_list, f, indent=2, ensure_ascii=False)

# --- Scheduled check once a week ---
async def scheduled_check():
    while True:
        if MOVIES_FILE.exists():
            with open(MOVIES_FILE, "r", encoding="utf-8") as f:
                movies = json.load(f)
            for m in movies:
                tmdb_id = m.get("tmdbId")
                title = m.get("title")
                with open(OVERLAY_FILE, "r", encoding="utf-8") as f:
                    overlay_data = yaml.safe_load(f) or {}
                key = f"movie_{tmdb_id}_expected"
                overlay_entry = overlay_data.get("overlays", {}).get(key)

                tmdb_data = await fetch_tmdb_movie(tmdb_id)
                if not tmdb_data:
                    continue
                title_eng = tmdb_data.get("original_title", title)
                bluray_date = await fetch_bluray_date(title_eng) or "TBD"

                if overlay_entry:
                    current_date = overlay_entry.get("template", {}).get("release_date")
                    if current_date != bluray_date:
                        overlay_entry["template"]["release_date"] = bluray_date
                        with open(OVERLAY_FILE, "w", encoding="utf-8") as f:
                            yaml.safe_dump(overlay_data, f, allow_unicode=True, sort_keys=False)
                        logger.info(f"Updated overlay for {title} with new date {bluray_date}")
                else:
                    add_overlay(tmdb_id, bluray_date)
                    logger.info(f"Added overlay for {title} with date {bluray_date}")
        await asyncio.sleep(CHECK_INTERVAL)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(scheduled_check())
    logger.info("Startup complete. Scheduled task running.")

# --- Radarr webhook ---
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
        if MOVIES_FILE.exists():
            with open(MOVIES_FILE, "r", encoding="utf-8") as f:
                movies_list = json.load(f)
            movies_list = [m for m in movies_list if m.get("tmdbId") != tmdb_id]
            with open(MOVIES_FILE, "w", encoding="utf-8") as f:
                json.dump(movies_list, f, indent=2, ensure_ascii=False)
        return {"status": "processed_removed"}

    return {"status": "ignored"}
