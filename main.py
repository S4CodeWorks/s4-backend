import os
import uuid
import re
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp

app = FastAPI(title="S4 Player API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Banco de dados temporário em memória para guardar o progresso dos downloads
progress_db = {}

# Função para limpar sujeira de texto (cores do terminal) do yt-dlp
def clean_ansi(text: str):
    if not text: return "0"
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text).strip()

def remove_file(path: str):
    if os.path.exists(path):
        os.remove(path)

# Modelo para receber os dados do React
class DownloadRequest(BaseModel):
    url: str
    format_type: str
    quality: str

@app.get("/")
def home():
    return {"message": "S4 Player API está rodando!"}

@app.get("/api/info")
def get_video_info(url: str):
    ydl_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': False}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            duration_seconds = info.get('duration', 0)
            minutes = duration_seconds // 60
            seconds = duration_seconds % 60
            
            return {
                "title": info.get('title'),
                "artist": info.get('artist') or info.get('uploader'),
                "thumbnail": info.get('thumbnail'),
                "duration": f"{minutes}:{seconds:02d}",
                "original_url": url
            }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# Função que cria o "espião" do yt-dlp para pegar a porcentagem
def get_progress_hook(task_id):
    def hook(d):
        if d['status'] == 'downloading':
            progress_db[task_id] = {
                "status": "downloading",
                "percent": clean_ansi(d.get('_percent_str', '0%')),
                "speed": clean_ansi(d.get('_speed_str', 'Calculando...')),
                "eta": clean_ansi(d.get('_eta_str', 'Calculando...')),
                "size": clean_ansi(d.get('_total_bytes_str', d.get('_estimated_total_bytes_str', 'N/A')))
            }
        elif d['status'] == 'finished':
            progress_db[task_id]["status"] = "processing" # yt-dlp está juntando o arquivo
    return hook

def process_download(task_id: str, req: DownloadRequest):
    os.makedirs("temp_downloads", exist_ok=True)
    output_template = f"temp_downloads/{task_id}.%(ext)s"

    ydl_opts = {
        'outtmpl': output_template,
        'quiet': True,
        'noprogress': True,
        'progress_hooks': [get_progress_hook(task_id)],
    }

    if req.format_type == 'audio':
        ydl_opts['format'] = 'bestaudio/best'
    else:
        height = ''.join(filter(str.isdigit, req.quality))
        ydl_opts['format'] = f'bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=True)
            filename = ydl.prepare_filename(info)
            safe_title = "".join([c for c in info.get('title', 'video') if c.isalpha() or c.isdigit() or c==' ']).rstrip()
            final_ext = filename.split('.')[-1]
            
            progress_db[task_id] = {
                "status": "done",
                "percent": "100%",
                "filename": filename,
                "download_name": f"{safe_title}.{final_ext}"
            }
    except Exception as e:
        progress_db[task_id] = {"status": "error"}

@app.post("/api/download/start")
def start_download(req: DownloadRequest, background_tasks: BackgroundTasks):
    task_id = str(uuid.uuid4())
    progress_db[task_id] = {"status": "starting", "percent": "0%", "speed": "", "eta": "", "size": ""}
    background_tasks.add_task(process_download, task_id, req)
    return {"task_id": task_id}

@app.get("/api/download/progress/{task_id}")
def get_progress(task_id: str):
    return progress_db.get(task_id, {"status": "not_found"})

@app.get("/api/download/file/{task_id}")
def get_file(task_id: str, background_tasks: BackgroundTasks):
    task = progress_db.get(task_id)
    if not task or task["status"] != "done":
        raise HTTPException(status_code=400, detail="Arquivo não pronto")
    
    # Apaga o arquivo do servidor depois que enviar pro usuário
    background_tasks.add_task(remove_file, task["filename"])
    return FileResponse(path=task["filename"], filename=task["download_name"], media_type='application/octet-stream')