from fastapi import FastAPI, Request
import asyncio, json, os, shutil, aiohttp, yaml
from pathlib import Path

app = FastAPI()

# --- Параметри через Compose ---
MOVIES_FILE = Path(os.getenv("MOVIES_FILE", "movies.json"))
OVERLAY_FILE = Path(os.getenv("OVERLAY_FILE", "kometa_config/upcoming_overlays.yml"))
UPCOMING_DIR = Path(os.getenv("UPCOMING_DIR", "Upcoming Movies"))
LANGUAGE = os.getenv("LANGUAGE", "uk")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 604800))  # 1 тиждень за замовчуванням
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
FRAME_PATH = os.getenv("FRAME_PATH", "kometa_config/overlays/red_frame.png")

# --- Допоміжні функції ---
async def fetch_tmdb_release_date(tmdb_id):
    url = f"https://api.themoviedb.org/3/movie/{tmdb_id}/release_dates?api_key={TMDB_API_KEY}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            for item in data.get("results", []):
                for rel in item.get("release_dates", []):
                    if rel.get("type") == 5:  # Physical release
                        return rel.get("release_date")
    return None

async def fetch_tmdb_poster(tmdb_id):
    url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={TMDB_API_KEY}&language={LANGUAGE}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            poster_path = data.get("poster_path")
            if poster_path:
                poster_file = UPCOMING_DIR / f"{tmdb_id}_poster.jpg"
                poster_url = f"https://image.tmdb.org/t/p/original{poster_path}"
                async with session.get(poster_url) as p:
                    poster_file.write_bytes(await p.read())
                return poster_file
    return None

async def fetch_tmdb_trailer(tmdb_id):
    url = f"https://api.themoviedb.org/3/movie/{tmdb_id}/videos?api_key={TMDB_API_KEY}&language={LANGUAGE}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            for video in data.get("results", []):
                if video.get("site") == "YouTube" and video.get("type") == "Trailer":
                    import yt_dlp
                    trailer_file = UPCOMING_DIR / f"{tmdb_id}_trailer.mp4"
                    ydl_opts = {'outtmpl': str(trailer_file), 'format': 'mp4', 'quiet': True}
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([f"https://www.youtube.com/watch?v={video.get('key')}"])
                    return trailer_file
    return None

def add_overlay(movie_id, release_date):
    if OVERLAY_FILE.exists():
        with open(OVERLAY_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    else:
        data = {"overlays": {}, "templates": {
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
    key = f"movie_{movie_id}_expected"
    data["overlays"][key] = {"template": {"name": "ExpectedRelease", "release_date": release_date}, "tmdb_id": movie_id}
    with open(OVERLAY_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

async def create_upcoming_movie(tmdb_id, title, year, release_date):
    folder = UPCOMING_DIR / f"{title} ({year})"
    folder.mkdir(parents=True, exist_ok=True)

    poster = await fetch_tmdb_poster(tmdb_id)
    trailer = await fetch_tmdb_trailer(tmdb_id)

    add_overlay(tmdb_id, release_date)

# --- Щотижнева перевірка ---
async def scheduled_check():
    while True:
        if MOVIES_FILE.exists():
            with open(MOVIES_FILE, "r", encoding="utf-8") as f:
                movies = json.load(f)
            updated = []
            for m in movies:
                tmdb_id = m["tmdb_id"]
                release_date = await fetch_tmdb_release_date(tmdb_id)
                if release_date:
                    await create_upcoming_movie(tmdb_id, m["title"], m["year"], release_date)
                else:
                    updated.append(m)
            with open(MOVIES_FILE, "w", encoding="utf-8") as f:
                json.dump(updated, f, indent=2, ensure_ascii=False)
        await asyncio.sleep(CHECK_INTERVAL)

@app.on_event("startup")
async def startup_event():
    UPCOMING_DIR.mkdir(exist_ok=True)
    asyncio.create_task(scheduled_check())

# --- Webhook Radarr ---
@app.post("/radarr-webhook")
async def radarr_webhook(req: Request):
    data = await req.json()
    event_type = data.get("eventType")
    movie = data.get("movie", {})
    tmdb_id = movie.get("tmdbId")
    title = movie.get("title")
    year = movie.get("year")

    if not tmdb_id:
        return {"status": "ignored"}

    release_date = await fetch_tmdb_release_date(tmdb_id)

    if event_type == "MovieAdded":
        if release_date:
            await create_upcoming_movie(tmdb_id, title, year, release_date)
        else:
            if MOVIES_FILE.exists():
                with open(MOVIES_FILE, "r", encoding="utf-8") as f:
                    movies = json.load(f)
            else:
                movies = []
            if not any(m.get("tmdb_id") == tmdb_id for m in movies):
                movies.append({"tmdb_id": tmdb_id, "title": title, "year": year})
            with open(MOVIES_FILE, "w", encoding="utf-8") as f:
                json.dump(movies, f, indent=2, ensure_ascii=False)
        return {"status": "processed_added"}

    elif event_type == "MovieDownloaded":
        folder = UPCOMING_DIR / f"{title} ({year})"
        if folder.exists():
            shutil.rmtree(folder)

        if MOVIES_FILE.exists():
            with open(MOVIES_FILE, "r", encoding="utf-8") as f:
                movies = json.load(f)
            movies = [m for m in movies if m.get("tmdb_id") != tmdb_id]
            with open(MOVIES_FILE, "w", encoding="utf-8") as f:
                json.dump(movies, f, indent=2, ensure_ascii=False)

        if OVERLAY_FILE.exists():
            with open(OVERLAY_FILE, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            key = f"movie_{tmdb_id}_expected"
            if "overlays" in data and key in data["overlays"]:
                del data["overlays"][key]
                with open(OVERLAY_FILE, "w", encoding="utf-8") as f:
                    yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        return {"status": "processed_downloaded"}

    return {"status": "ignored"}
