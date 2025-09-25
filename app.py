from fastapi import FastAPI, Request
import json, asyncio, os, shutil, aiohttp, yaml, subprocess
from pathlib import Path
from datetime import datetime

app = FastAPI()

# --- Параметри з середовища ---
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
LANGUAGE = os.getenv("LANGUAGE", "uk")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 604800))
FRAME_PATH = os.getenv("FRAME_PATH", "/overlays/red_frame.png")
MOVIES_FILE = Path(os.getenv("MOVIES_FILE", "/movies/movies.json"))
OVERLAY_FILE = Path(os.getenv("OVERLAY_FILE", "/overlays/upcoming_overlays.yml"))
UPCOMING_DIR = Path(os.getenv("UPCOMING_DIR", "/UpcomingMovies"))

# Створюємо директорії та файли, якщо їх немає
UPCOMING_DIR.mkdir(parents=True, exist_ok=True)
MOVIES_FILE.parent.mkdir(parents=True, exist_ok=True)
if not MOVIES_FILE.exists():
    MOVIES_FILE.write_text("[]", encoding="utf-8")
if not OVERLAY_FILE.exists():
    OVERLAY_FILE.parent.mkdir(parents=True, exist_ok=True)
    OVERLAY_FILE.write_text(json.dumps({"overlays": {}, "templates": {}}), encoding="utf-8")

# --- Функції ---
async def fetch_physical_release_date(tmdb_id):
    url = f"https://api.themoviedb.org/3/movie/{tmdb_id}/release_dates?api_key={TMDB_API_KEY}&language={LANGUAGE}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
    for r in data.get("results", []):
        for rel in r.get("release_dates", []):
            if rel.get("type") == 5:  # Physical
                return rel.get("release_date")
    return None

def download_poster(tmdb_poster_path, dest_file):
    url = f"https://image.tmdb.org/t/p/original{tmdb_poster_path}"
    try:
        subprocess.run(["curl", "-s", "-o", str(dest_file), url], check=True)
    except Exception as e:
        print("Poster download failed:", e)

def download_trailer(youtube_url, dest_file):
    try:
        subprocess.run(["yt-dlp", "-o", str(dest_file), youtube_url], check=True)
    except Exception as e:
        print("Trailer download failed:", e)

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
    folder = UPCOMING_DIR / f"{title} ({year})"
    folder.mkdir(parents=True, exist_ok=True)
    if poster_file:
        shutil.copy2(poster_file, folder / "poster.jpg")
    if trailer_file:
        shutil.copy2(trailer_file, folder / trailer_file.name)
    add_overlay(movie_id, release_date)
    return folder

async def handle_movie_added(movie):
    title = movie["title"]
    year = movie["year"]
    tmdb_id = movie["tmdbId"]
    folder_name = movie.get("folderPath") or f"{title} ({year})"

    release_date = await fetch_physical_release_date(tmdb_id)
    if not release_date:
        # Додаємо у JSON для відкладеної перевірки
        with open(MOVIES_FILE, "r+", encoding="utf-8") as f:
            movies = json.load(f)
            if not any(m.get("tmdbId") == tmdb_id for m in movies):
                movies.append({
                    "title": title,
                    "year": year,
                    "tmdbId": tmdb_id,
                    "folder_name": folder_name
                })
                f.seek(0)
                json.dump(movies, f, ensure_ascii=False, indent=2)
                f.truncate()
        return

    # Якщо дата є — завантажуємо постер і трейлер
    poster_file = Path("/tmp/poster.jpg")
    trailer_file = Path("/tmp/trailer.mp4")

    if movie.get("poster_path"):
        download_poster(movie["poster_path"], poster_file)

    if movie.get("trailer_youtube_url"):
        download_trailer(movie["trailer_youtube_url"], trailer_file)

    create_upcoming_folder(title, year, poster_file, trailer_file, tmdb_id, release_date)

async def scheduled_check():
    while True:
        with open(MOVIES_FILE, "r+", encoding="utf-8") as f:
            movies = json.load(f)
            updated = []
            for m in movies:
                tmdb_id = m["tmdbId"]
                release_date = await fetch_physical_release_date(tmdb_id)
                if release_date:
                    poster_file = Path("/tmp/poster.jpg")
                    trailer_file = Path("/tmp/trailer.mp4")
                    if m.get("poster_path"):
                        download_poster(m["poster_path"], poster_file)
                    if m.get("trailer_youtube_url"):
                        download_trailer(m["trailer_youtube_url"], trailer_file)
                    create_upcoming_folder(m["title"], m["year"], poster_file, trailer_file, tmdb_id, release_date)
                else:
                    updated.append(m)
            f.seek(0)
            json.dump(updated, f, ensure_ascii=False, indent=2)
            f.truncate()
        await asyncio.sleep(CHECK_INTERVAL)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(scheduled_check())

@app.post("/radarr-webhook")
async def radarr_webhook(req: Request):
    data = await req.json()
    print("Webhook received:", json.dumps(data, indent=2))  # Лог всього вебхуку

    movie = data.get("movie", {})
    event_type = data.get("eventType")

    if event_type == "MovieAdded":
        await handle_movie_added(movie)
        return {"status": "processed_added"}

    elif event_type == "MovieDownloaded":
        title = movie["title"]
        year = movie["year"]
        tmdb_id = movie["tmdbId"]

        # Видаляємо папку
        folder = UPCOMING_DIR / f"{title} ({year})"
        if folder.exists():
            shutil.rmtree(folder)

        # Видаляємо з JSON
        with open(MOVIES_FILE, "r+", encoding="utf-8") as f:
            movies = json.load(f)
            movies = [m for m in movies if m["tmdbId"] != tmdb_id]
            f.seek(0)
            json.dump(movies, f, ensure_ascii=False, indent=2)
            f.truncate()

        # Видаляємо overlay
        if OVERLAY_FILE.exists():
            with open(OVERLAY_FILE, "r", encoding="utf-8") as f:
                data_yaml = yaml.safe_load(f)
            key = f"movie_{tmdb_id}_expected"
            if "overlays" in data_yaml and key in data_yaml["overlays"]:
                del data_yaml["overlays"][key]
                with open(OVERLAY_FILE, "w", encoding="utf-8") as f:
                    yaml.safe_dump(data_yaml, f, allow_unicode=True, sort_keys=False)

        return {"status": "processed_downloaded"}

    return {"status": "ignored"}
