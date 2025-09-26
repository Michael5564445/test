import os
import json
import requests
import yt_dlp
from flask import Flask, request, jsonify
from pathlib import Path
from datetime import datetime

# ========================
# ENVIRONMENT VARIABLES
# ========================
UPCOMING_PATH = Path(os.environ.get("UPCOMING_PATH", "/app/Upcoming Movies"))
LANGUAGE = os.environ.get("LANGUAGE", "uk")  # uk/en
JSON_FILE = Path(os.environ.get("JSON_FILE", "/app/upcoming_movies.json"))
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")

# ========================
# INITIALIZATION
# ========================
app = Flask(__name__)
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

def get_tmdb_movie(tmdb_id):
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
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

def download_poster(poster_path, dest_path):
    resp = requests.get(poster_path, stream=True, timeout=10)
    if resp.status_code == 200:
        with open(dest_path / "poster.jpg", "wb") as f:
            for chunk in resp.iter_content(1024):
                f.write(chunk)

def process_movie(movie):
    tmdb_id = str(movie.get("tmdbId"))
    title = movie.get("title")
    
    # Використовуємо US реліз
    release_date_str = movie.get("physicalRelease") or movie.get("digitalRelease") or movie.get("usRelease")
    
    upcoming_data = load_upcoming()
    
    # Завжди додаємо у JSON
    upcoming_data[tmdb_id] = {"title": title, "release_date": release_date_str}
    save_upcoming(upcoming_data)

    if not release_date_str:
        log(f"No release date for {title}, folder not created")
        return  # не створюємо папку, постер і трейлер

    # Створюємо папку
    release_date = datetime.fromisoformat(release_date_str)
    now = datetime.now()
    if release_date <= now:
        log(f"{title} already released, skipping folder creation")
        return

    folder_name = sanitize_filename(f"{title} ({release_date.year})")
    movie_path = UPCOMING_PATH / folder_name
    movie_path.mkdir(parents=True, exist_ok=True)

    # TMDB info
    tmdb_info = get_tmdb_movie(tmdb_id)
    if not tmdb_info:
        log(f"TMDb data not found for {title}")
        return

    # Завантажуємо постер
    poster_url = f"https://image.tmdb.org/t/p/original{tmdb_info.get('poster_path')}"
    download_poster(poster_url, movie_path)

    # Завантажуємо трейлер (YouTube, мова з LANGUAGE)
    videos = tmdb_info.get("videos", {}).get("results", [])
    trailer_url = None
    for v in videos:
        if v["type"] == "Trailer" and v["site"] == "YouTube" and v["iso_639_1"] == LANGUAGE:
            trailer_url = f"https://www.youtube.com/watch?v={v['key']}"
            break
    if trailer_url:
        download_trailer(trailer_url, movie_path)

    log(f"Upcoming movie processed: {title}")

# ========================
# WEBHOOK HANDLER
# ========================
@app.route("/radarr/webhook", methods=["POST"])
def radarr_webhook():
    data = request.json
    log(f"Webhook received: {data}")

    tmdb_id = str(data.get("tmdbId"))
    title = data.get("title", "unknown")
    movie_path = UPCOMING_PATH / sanitize_filename(title)

    # Якщо фільм завантажено – видаляємо папку та JSON
    if data.get("downloaded", False):
        upcoming_data = load_upcoming()
        if tmdb_id in upcoming_data:
            del upcoming_data[tmdb_id]
            save_upcoming(upcoming_data)
            if movie_path.exists():
                for f in movie_path.iterdir():
                    f.unlink()
                movie_path.rmdir()
            log(f"Movie downloaded: {title} – removed from upcoming")
        return jsonify({"status": "removed"})

    # Інакше перевіряємо і додаємо
    process_movie(data)
    return jsonify({"status": "ok"})

# ========================
# RUN
# ========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
