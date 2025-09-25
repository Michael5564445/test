from fastapi import FastAPI, Request
import json, asyncio, os
from pathlib import Path
import shutil
import yaml

app = FastAPI()

# Параметри
MOVIES_FILE = Path("movies.json")
OVERLAY_FILE = Path("kometa_config/upcoming_overlays.yml")
UPCOMING_DIR = Path("Upcoming Movies")
COMPOSE_DIR = Path("compose_movies")

LANGUAGE = os.getenv("LANGUAGE", "uk")  # мова з Compose
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 604800))  # за замовчуванням 1 тиждень
FRAME_PATH = "kometa_config/overlays/red_frame.png"

# --- Helper функції ---
def create_upcoming_folder(title, year, poster_file, trailer_file, movie_id, release_date):
    folder_name = f"{title} ({year})"
    folder = UPCOMING_DIR / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    
    # Копіюємо постер
    if poster_file.exists():
        shutil.copy2(poster_file, folder / "poster.jpg")
    
    # Копіюємо трейлер
    if trailer_file.exists():
        shutil.copy2(trailer_file, folder / trailer_file.name)
    
    # Додаємо overlay у YAML
    add_overlay(movie_id, release_date)
    return folder

def add_overlay(movie_id, release_date):
    # Читаємо або створюємо YAML
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
    data["overlays"][key] = {
        "template": {"name": "ExpectedRelease", "release_date": release_date},
        "tmdb_id": movie_id
    }

    with open(OVERLAY_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

def get_movie_files(folder_path):
    poster = None
    trailer = None
    for f in folder_path.iterdir():
        if f.is_file():
            if f.name.lower().startswith(f"poster_{LANGUAGE}"):
                poster = f
            elif f.name.lower().startswith(f"trailer_{LANGUAGE}"):
                trailer = f
    return poster, trailer

# --- Щотижнева перевірка ---
async def scheduled_check():
    while True:
        if MOVIES_FILE.exists():
            with open(MOVIES_FILE, "r", encoding="utf-8") as f:
                movies = json.load(f)
            updated = []
            for m in movies:
                folder_path = COMPOSE_DIR / m["folder_name"]
                poster, trailer = get_movie_files(folder_path)
                release_date = m.get("release_date")
                if poster and trailer and release_date:
                    create_upcoming_folder(m["title"], m["year"], poster, trailer, m["folder_id"], release_date)
                else:
                    updated.append(m)
            with open(MOVIES_FILE, "w", encoding="utf-8") as f:
                json.dump(updated, f, indent=2, ensure_ascii=False)
        await asyncio.sleep(CHECK_INTERVAL)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(scheduled_check())

# --- Webhook Radarr ---
@app.post("/radarr-webhook")
async def radarr_webhook(req: Request):
    data = await req.json()
    event_type = data.get("eventType")
    movie = data.get("movie", {})
    folder_name = movie.get("folderName")
    movie_id = movie.get("tmdbId")
    release_date = movie.get("physicalReleaseDate")  # вказана дата в вебхуку
    year = movie.get("year")
    title = movie.get("title")

    folder_path = COMPOSE_DIR / folder_name
    poster, trailer = get_movie_files(folder_path)

    if event_type == "MovieAdded":
        if poster and trailer and release_date:
            create_upcoming_folder(title, year, poster, trailer, movie_id, release_date)
        else:
            # Додати у JSON для подальшої перевірки
            if MOVIES_FILE.exists():
                with open(MOVIES_FILE, "r", encoding="utf-8") as f:
                    movies = json.load(f)
            else:
                movies = []
            if not any(m.get("folder_id") == movie_id for m in movies):
                movies.append({
                    "folder_name": folder_name,
                    "folder_id": movie_id,
                    "title": title,
                    "year": year,
                    "release_date": release_date
                })
            with open(MOVIES_FILE, "w", encoding="utf-8") as f:
                json.dump(movies, f, indent=2, ensure_ascii=False)
        return {"status": "processed_added"}

    elif event_type == "MovieDownloaded":
        # Видалення папки з UPCOMING
        upcoming_folder = UPCOMING_DIR / f"{title} ({year})"
        if upcoming_folder.exists():
            shutil.rmtree(upcoming_folder)

        # Видалення з JSON
        if MOVIES_FILE.exists():
            with open(MOVIES_FILE, "r", encoding="utf-8") as f:
                movies = json.load(f)
            movies = [m for m in movies if m.get("folder_id") != movie_id]
            with open(MOVIES_FILE, "w", encoding="utf-8") as f:
                json.dump(movies, f, indent=2, ensure_ascii=False)

        # Видалення запису з YAML
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
