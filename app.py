from __future__ import annotations
import io, ipaddress, socket, zipfile, re, html as html_lib, json
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import httpx
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
app = FastAPI(title="ImageNinja", docs_url=None, redoc_url=None)
MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_IMAGES = 100
TIMEOUT = httpx.Timeout(20.0, connect=8.0)
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"}
KEMONO_HEADERS = {**HEADERS, "Accept": "text/css", "Referer": "https://kemono.cr/"}
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp", "image/avif"}
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif", ".jfif")

class ScanRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)

class DownloadRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=30)

def validate_url(url: str) -> str:
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
            raise ValueError()
        host = p.hostname.rstrip(".").lower()
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            raise ValueError()
        records = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80), type=socket.SOCK_STREAM)
        for rec in records:
            if not ipaddress.ip_address(rec[4][0]).is_global:
                raise ValueError()
        return url
    except Exception:
        raise HTTPException(400, "URL no válida o destino no permitido.")

async def safe_get(client: httpx.AsyncClient, url: str, *, max_bytes: int, headers=None):
    current = validate_url(url)
    for _ in range(4):
        async with client.stream("GET", current, headers=headers or HEADERS, follow_redirects=False) as resp:
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("location")
                if not loc:
                    raise HTTPException(502, "Redirección sin destino.")
                current = validate_url(urljoin(current, loc))
                continue
            if resp.status_code >= 400:
                raise HTTPException(502, f"El servidor respondió HTTP {resp.status_code}.")
            length = resp.headers.get("content-length")
            if length and int(length) > max_bytes:
                raise HTTPException(413, "El archivo supera el límite permitido.")
            data = bytearray()
            async for chunk in resp.aiter_bytes():
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise HTTPException(413, "El contenido supera el límite permitido.")
            return bytes(data), resp.headers.get("content-type", "").split(";")[0].lower(), str(resp.url)
    raise HTTPException(502, "Demasiadas redirecciones.")

def add_item(items, url, page_url, alt="", kind="Imagen"):
    if not url:
        return
    url = html_lib.unescape(str(url).strip()).replace("\\/", "/")
    if url.startswith(("data:", "blob:", "javascript:")):
        return
    full = urljoin(page_url, url)
    p = urlparse(full)
    if p.scheme not in ("http", "https") or not p.hostname:
        return
    # Keep the original URL unique; don't discard thumbnails automatically.
    if not any(x["url"] == full for x in items):
        items.append({"url": full, "alt": (str(alt or unquote(p.path.rsplit("/", 1)[-1]) or kind)[:180]),
                      "source": p.netloc, "kind": kind})

def parse_kemono_post_url(url):
    p = urlparse(url)
    if not (p.hostname or "").lower().endswith("kemono.cr"):
        return None
    m = re.match(r"^/([^/]+)/user/([^/]+)/post/([^/?#]+)", p.path)
    if not m:
        return None
    service, user_id, post_id = m.groups()
    if service in {"api", "search", "artists", "favorites"}:
        return None
    return service, user_id, post_id

def kemono_file_url(path, preview=False):
    if not path:
        return None
    path = str(path).replace("\\", "/")
    if path.startswith(("http://", "https://")):
        if preview and "/data/" in path:
            path = path.split("/data/", 1)[1]
        else:
            return path
    path = "/" + path.lstrip("/")
    if path.startswith("/data/"):
        path = path[len("/data"):]
    if preview:
        return "https://img.kemono.cr/thumbnail/data" + path
    return "https://kemono.cr/data" + path

async def scan_kemono_api(client, post_url, items):
    parsed = parse_kemono_post_url(post_url)
    if not parsed:
        return False, "No es una URL de publicación Kemono reconocida."
    service, user_id, post_id = parsed
    api_url = f"https://kemono.cr/api/v1/{service}/user/{user_id}/post/{post_id}"
    try:
        data, ctype, _ = await safe_get(client, api_url, max_bytes=MAX_PAGE_BYTES, headers=KEMONO_HEADERS)
        obj = json.loads(data.decode("utf-8", errors="replace"))
        post = obj.get("post", obj) if isinstance(obj, dict) else {}
        if not isinstance(post, dict):
            return False, "La API no devolvió los datos esperados."
        count_before = len(items)
        main_file = post.get("file") or {}
        if isinstance(main_file, dict) and main_file.get("path"):
            name = main_file.get("name", "Archivo principal")
            path = main_file["path"]
            is_img = (str(name).lower().endswith(IMAGE_SUFFIXES) or str(path).lower().split("?")[0].endswith(IMAGE_SUFFIXES))
            if is_img:
                add_item(items, kemono_file_url(path), post_url, name, "Original / archivo principal")
        for attachment in post.get("attachments") or []:
            if not isinstance(attachment, dict) or not attachment.get("path"):
                continue
            name = attachment.get("name") or attachment.get("path")
            path = attachment["path"]
            is_img = str(name).lower().endswith(IMAGE_SUFFIXES) or str(path).lower().split("?")[0].endswith(IMAGE_SUFFIXES)
            if is_img:
                add_item(items, kemono_file_url(path), post_url, name, "Original / adjunto")
        content = post.get("content") or ""
        soup = BeautifulSoup(content, "html.parser")
        for tag in soup.find_all(("img", "source")):
            for attr in ("src", "data-src", "data-original", "data-full"):
                val = tag.get(attr)
                if val:
                    add_item(items, val, post_url, tag.get("alt") or "Imagen del contenido", "Contenido")
        for tag in soup.find_all("a", href=True):
            href = tag["href"]
            if any(x in urlparse(urljoin(post_url, href)).path.lower() for x in ("/data/", "/attachment/", "/attachments/")) or urlparse(href).path.lower().endswith(IMAGE_SUFFIXES):
                add_item(items, href, post_url, tag.get_text(" ", strip=True) or "Adjunto del contenido", "Adjunto del contenido")
        return True, f"API de Kemono consultada; añadió {len(items)-count_before} archivo(s) de imagen."
    except Exception as exc:
        return False, f"No se pudo consultar la API de Kemono ({type(exc).__name__}). Se usará el HTML visible."

@app.get("/", response_class=HTMLResponse)
async def home():
    with open(STATIC_DIR / "index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/manifest.webmanifest")
async def manifest():
    return JSONResponse({"name":"ImageNinja - Descargador de imágenes","short_name":"ImageNinja","start_url":"/","display":"standalone","background_color":"#0b1020","theme_color":"#0b1020","description":"Busca, selecciona y descarga imágenes de páginas web."}, media_type="application/manifest+json")

@app.get("/sw.js")
async def service_worker():
    with open(STATIC_DIR / "sw.js", "r", encoding="utf-8") as f:
        return Response(f.read(), media_type="application/javascript", headers={"Cache-Control":"no-cache"})

@app.post("/api/scan")
async def scan(body: ScanRequest):
    page_url = validate_url(body.url)
    items = []
    api_used = False
    api_message = ""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        parsed_kemono = parse_kemono_post_url(page_url)
        if parsed_kemono:
            api_used, api_message = await scan_kemono_api(client, page_url, items)
        try:
            data, ctype, final_url = await safe_get(client, page_url, max_bytes=MAX_PAGE_BYTES,
                                                     headers={**HEADERS, "Referer": "https://kemono.cr/"} if parsed_kemono else HEADERS)
        except HTTPException:
            if items:
                return {"page": page_url, "count": len(items), "images": items[:MAX_IMAGES],
                        "truncated": len(items)>MAX_IMAGES, "diagnostic": api_message or "Se encontraron archivos en la API aunque la página HTML no respondió."}
            raise
    if "html" not in ctype and not data.lstrip().startswith((b"<!doctype html", b"<html", b"<?xml")):
        if ctype.startswith("image/"):
            add_item(items, final_url, final_url, "Imagen enlazada", "URL directa")
            return {"page": final_url, "count": 1, "images": items, "truncated": False}
        if items:
            return {"page": final_url, "count": len(items), "images": items[:MAX_IMAGES], "truncated":len(items)>MAX_IMAGES, "diagnostic":api_message}
        raise HTTPException(415, "La URL no parece una página HTML ni una imagen.")

    soup = BeautifulSoup(data, "html.parser")
    image_attrs = ("src","data-src","data-original","data-lazy-src","data-original-src","data-full","data-image","data-url","data-file")
    for tag in soup.find_all(("img","source")):
        for attr in image_attrs:
            if tag.get(attr):
                val = tag.get(attr)
                kind = "Miniatura" if "/thumbnail/" in val else "Imagen de página"
                add_item(items, val, final_url, tag.get("alt") or "", kind)
        for attr in ("srcset","data-srcset"):
            srcset = tag.get(attr)
            if srcset:
                candidates = [part.strip().split()[0] for part in srcset.split(",") if part.strip()]
                if candidates:
                    add_item(items, candidates[-1], final_url, tag.get("alt") or "", "Imagen srcset")
    for tag in soup.find_all("meta"):
        prop = (tag.get("property") or tag.get("name") or "").lower()
        if prop in {"og:image","og:image:secure_url","twitter:image","twitter:image:src"}:
            add_item(items, tag.get("content"), final_url, prop, "Imagen meta")
    for tag in soup.find_all("a", href=True):
        href = tag.get("href","").strip()
        path = urlparse(urljoin(final_url, href)).path.lower()
        if path.endswith(IMAGE_SUFFIXES) or tag.has_attr("download") or any(x in path for x in ("/data/","/attachment/","/attachments/")):
            add_item(items, href, final_url, tag.get_text(" ",strip=True), "Adjunto/enlace")
    raw = html_lib.unescape(data.decode("utf-8", errors="ignore")).replace("\\/", "/")
    pattern = r"""(?i)(?:https?:)?//[^\\\"'\s<>]+?(?:/data/|/attachment/|/attachments/|/thumbnail/)[^\\\"'\s<>]*|/(?:data|attachment|attachments|thumbnail)/[^\\\"'\s<>]*"""
    for match in re.findall(pattern, raw):
        candidate = match.rstrip("),;")
        if candidate.startswith("//"):
            candidate = "https:" + candidate
        if candidate.startswith(("http://","https://","/")):
            add_item(items, candidate, final_url, "", "URL detectada en código")
    # Deduplicación adicional por URL exacta, conservando tipos distintos de ruta.
    unique = []
    seen = set()
    for item in items:
        if item["url"] not in seen:
            seen.add(item["url"])
            unique.append(item)
    images = unique[:MAX_IMAGES]
    diagnostic = api_message if parsed_kemono else ""
    if parsed_kemono and api_used:
        diagnostic = (diagnostic + " Se combinaron los archivos de la API con las imágenes del HTML.").strip()
    return {"page": final_url, "count": len(images), "images": images, "truncated": len(unique)>MAX_IMAGES, "diagnostic": diagnostic}

@app.post("/api/download")
async def download(body: DownloadRequest):
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        tasks = []
        for u in body.urls[:30]:
            data, ctype, final_url = await safe_get(client, u, max_bytes=MAX_IMAGE_BYTES,
                headers={**HEADERS, "Referer": "https://kemono.cr/"} if "kemono.cr" in (urlparse(u).hostname or "") else HEADERS)
            if not ctype.startswith("image/") and not data.startswith((b"\x89PNG",b"\xff\xd8\xff",b"GIF8",b"RIFF")):
                continue
            name = unquote(urlparse(final_url).path.rsplit("/",1)[-1]) or f"imagen_{len(tasks)+1}.jpg"
            name = "".join(c for c in name if c.isalnum() or c in "._-")[:100] or f"imagen_{len(tasks)+1}.jpg"
            if "." not in name:
                name += { "image/png":".png", "image/webp":".webp", "image/gif":".gif"}.get(ctype,".jpg")
            tasks.append((name,data,ctype or "application/octet-stream"))
    if not tasks:
        raise HTTPException(422,"No se pudieron obtener imágenes válidas. Puede que el servidor de archivos de origen no responda.")
    if len(tasks)==1:
        name,data,ctype=tasks[0]
        return Response(data,media_type=ctype,headers={"Content-Disposition":f'attachment; filename="{name}"'})
    mem=io.BytesIO()
    with zipfile.ZipFile(mem,"w",zipfile.ZIP_DEFLATED) as z:
        for i,(name,data,_) in enumerate(tasks,1):
            z.writestr(f"{i:02d}_{name}",data)
    mem.seek(0)
    return StreamingResponse(mem,media_type="application/zip",headers={"Content-Disposition":'attachment; filename="imageninja.zip"'})

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
