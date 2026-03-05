import os
import uuid
import re
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp

app = FastAPI(title="S4 Player API")

# Liberação de CORS - Essencial para a Vercel conseguir falar com o Render
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

progress_db = {}

# Cabeçalhos realistas para evitar detecção de robô
HEADERS = {
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'accept-language': 'pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7',
}

def clean_ansi(text: str):
    if not text: return "0"
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text).strip()

def remove_file(path: str):
    if os.path.exists(path):
        os.remove(path)

class DownloadRequest(BaseModel):
    url: str
    format_type: str
    quality: str

@app.get("/")
def home():
    return {"message": "S4 Player API está rodando!"}

# --- ROTA DE PREVIEW (BUSCA INFO DO VÍDEO) ---
@app.get("/api/info")
def get_video_info(url: str):
    ydl_opts = {
        'quiet': True, 
        'no_warnings': True, 
        'extract_flat': False,
        'http_headers': HEADERS,
        # 🛡️ ESTRATÉGIA MULTI-CLIENTE: Tenta várias identidades para pular o bloqueio de bot
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios', 'web_embedded'],
                'player_skip': ['webpage', 'configs'],
            }
        },
        'youtube_include_dash_manifest': False,
    }
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
        print(f"ERRO NOS LOGS DO RENDER: {str(e)}")
        raise HTTPException(status_code=400, detail="O YouTube bloqueou o acesso temporariamente. Tente outro link ou aguarde.")

# --- LÓGICA DE DOWNLOAD E PROGRESSO ---
def get_progress_hook(task_id):
    def hook(d):
        if d['status'] == 'downloading':
            progress_db[task_id] = {
                "status": "downloading",
                "percent": clean_ansi(d.get('_percent_str', '0%')),
                "speed": clean_ansi(d.get('_speed_str', '...')),
                "eta": clean_ansi(d.get('_eta_str', '...')),
                "size": clean_ansi(d.get('_total_bytes_str', 'N/A'))
            }
        elif d['status'] == 'finished':
            progress_db[task_id]["status"] = "processing"
    return hook

def process_download(task_id: str, req: DownloadRequest):
    os.makedirs("temp_downloads", exist_ok=True)
    output_template = f"temp_downloads/{task_id}.%(ext)s"

    ydl_opts = {
        'outtmpl': output_template,
        'quiet': True,
        'noprogress': True,
        'http_headers': HEADERS,
        'progress_hooks': [get_progress_hook(task_id)],
        # 🛡️ Repetindo a estratégia multi-cliente no download real
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios', 'web_embedded'],
            }
        }
    }

    if req.format_type == 'audio':
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    else:
        height = ''.join(filter(str.isdigit, req.quality))
        ydl_opts['format'] = f'bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=True)
            filename = ydl.prepare_filename(info)
            # Limpa o nome do arquivo final para o usuário
            safe_title = "".join([c for c in info.get('title', 'video') if c.isalnum() or c==' ']).strip()
            final_ext = filename.split('.')[-1]
            
            progress_db[task_id] = {
                "status": "done",
                "percent": "100%",
                "filename": filename,
                "download_name": f"{safe_title}.{final_ext}"
            }
    except Exception as e:
        print(f"ERRO NO DOWNLOAD: {str(e)}")
        progress_db[task_id] = {"status": "error"}

@app.post("/api/download/start")
def start_download(req: DownloadRequest, background_tasks: BackgroundTasks):
    task_id = str(uuid.uuid4())
    progress_db[task_id] = {"status": "starting", "percent": "0%"}
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
    
    background_tasks.add_task(remove_file, task["filename"])
    return FileResponse(path=task["filename"], filename=task["download_name"], media_type='application/octet-stream')