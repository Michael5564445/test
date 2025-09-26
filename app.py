import os
import json
import requests
import yt_dlp
import asyncio
import threading
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pathlib import Path
from datetime import datetime, timezone
import uvicorn

# ========================
# ENVIRONMENT VARIABLES
# ========================
UPCOMING_PATH = Path(os.environ.get("UPCOMING_PATH", "/data/movies"))
JSON_FILE = Path(os.environ.get("JSON_FILE", "/data/upcoming_movies.json"))
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
LANGUAGE = os.environ.get("LANGUAGE", "en")  # мова постерів і трейлерів
RELEASE_TYPE = int(os.environ.get("RELEASE_TYPE", 5))  # тип релізу
UPDATE_INTERVAL = int(os.environ.get("UPDATE_INTERVAL", 604800))  # раз на тиждень

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


def get_tmdb_release_date(tmdb_id: str):
    url = f"https://api.themoviedb.org/3/movie/{tmdb_id}/release_dates?api_key={TMDB_API_KEY}"
    resp = requests.get(url, timeout=10)
    if resp.status_code != 200:
        return None
    data = resp.json()
    for country in data.get("results", []):
        if country["iso_3166_1"] == "US":
            for rel in country.get("release_dates", []):
                if rel["type"] == RELEASE_TYPE and rel.get("release_date"):
                    return rel["release_date"]
    return None


def get_tmdb_movie(tmdb_id: str):
    url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={TMDB_API_KEY}&language={LANGUAGE}&append_to_response=videos"
    resp = requests.get(url, timeout=10)
    if resp.status_code != 200:
        return None
    return resp.json()


def download_trailer(url, dest_path):
    ydl_opts = {
        "outtmpl": str(dest_path / "trailer.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "format": "bestvideo+bestaudio/best",
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])


def download_poster(poster_url, dest_path):
    resp = requests.get(poster_url, stream=True, timeout=10)
    if resp.status_code == 200:
        with open(dest_path / "poster.jpg", "wb") as f:
            for chunk in resp.iter_content(1024):
                f.write(chunk)


def process_movie(movie):
    tmdb_id = str(movie.get("tmdbId"))
    title = movie.get("title")

    release_date_str = get_tmdb_release_date(tmdb_id)

    upcoming_data = load_upcoming()
    upcoming_data[tmdb_id] = {"title": title, "release_date": release_date_str}
    save_upcoming(upcoming_data)

    if not release_date_str:
        log(f"No release date for {title}, skipping folder creation")
        return

    release_date = datetime.fromisoformat(release_date_str.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc).astimezone()
    if release_date <= now:
        log(f"{title} already released, skipping folder creation")
        return

    folder_name = sanitize_filename(f"{title} ({release_date.year})")
    movie_path = UPCOMING_PATH / folder_name
    movie_path.mkdir(parents=True, exist_ok=True)

    # TMDb data
    tmdb_info = get_tmdb_movie(tmdb_id)
    if not tmdb_info:
        log(f"TMDb data not found for {title}")
        return

    # Постер мовою LANGUAGE
    poster_path = tmdb_info.get("poster_path")
    if poster_path:
        poster_url = f"https://image.tmdb.org/t/p/original{poster_path}"
        download_poster(poster_url, movie_path)

    # Трейлер мовою LANGUAGE
    videos = tmdb_info.get("videos", {}).get("results", [])
    trailer_url = None
    for v in videos:
        if (
            v.get("type") == "Trailer"
            and v.get("site") == "YouTube"
            and v.get("iso_639_1") == LANGUAGE
        ):
            trailer_url = f"https://www.youtube.com/watch?v={v['key']}"
            break

    # fallback: будь-який трейлер якщо мовного немає
    if not trailer_url:
        for v in videos:
            if v.get("type") == "Trailer" and v.get("site") == "YouTube":
                trailer_url = f"https://www.youtube.com/watch?v={v['key']}"
                break

    if trailer_url:
        download_trailer(trailer_url, movie_path)

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

    # Видалення фільму після завантаження або видалення
    if event_type in ["Download", "MovieDownloaded", "MovieDelete"]:
        upcoming_data = load_upcoming()
        if tmdb_id in upcoming_data:
            del upcoming_data[tmdb_id]
            save_upcoming(upcoming_data)

        folder_name = sanitize_filename(f"{title} ({movie.get('year')})")
        movie_path = UPCOMING_PATH / folder_name
        if movie_path.exists():
            for f in movie_path.iterdir():
                f.unlink()
            movie_path.rmdir()
        log(f"Movie removed: {title}")
        return JSONResponse({"status": "removed"})

    # Обробка нових фільмів
    if event_type == "MovieAdded":
        process_movie(movie)
        return JSONResponse({"status": "ok"})

    return JSONResponse({"status": "ignored"})


# ========================
# BACKGROUND TASK
# ========================
async def scheduled_task():
    while True:
        log("Scheduled check for upcoming movies...")
        upcoming_data = load_upcoming()
        for tmdb_id, info in list(upcoming_data.items()):
            release_date_str = get_tmdb_release_date(tmdb_id)
            if release_date_str:
                upcoming_data[tmdb_id]["release_date"] = release_date_str
        save_upcoming(upcoming_data)
        await asyncio.sleep(UPDATE_INTERVAL)


def start_scheduler():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(scheduled_task())


threading.Thread(target=start_scheduler, daemon=True).start()

# ========================
# AUTO RUN
# ========================
if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000)
