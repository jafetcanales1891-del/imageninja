from __future__ import annotations
import io, ipaddress, socket, zipfile, re, html as html_lib
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
MAX_PAGE_BYTES = 4 * 1024 * 1024
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_IMAGES = 80
TIMEOUT = httpx.Timeout(15.0, connect=7.0)
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ImageNinja/0.2; image fetcher)"}
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp", "image/avif"}

class ScanRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)

class DownloadRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=30)

def validate_url(url: str) -> str:
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            raise ValueError()
        if p.username or p.password:
            raise ValueError()
        host = p.hostname.rstrip(".").lower()
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            raise ValueError()
        try:
            records = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80), type=socket.SOCK_STREAM)
            for rec in records:
                ip = ipaddress.ip_address(rec[4][0])
                if not ip.is_global:
                    raise ValueError()
        except socket.gaierror:
            raise ValueError()
        return url
    except Exception:
        raise HTTPException(400, "URL no válida o destino no permitido.")

async def safe_get(client: httpx.AsyncClient, url: str, *, max_bytes: int, stream: bool = False):
    current = validate_url(url)
    for _ in range(4):
        async with client.stream("GET", current, headers=HEADERS, follow_redirects=False) as resp:
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("location")
                if not loc:
                    raise HTTPException(502, "Redirección sin destino.")
                current = validate_url(urljoin(current, loc))
                continue
            if resp.status_code >= 400:
                raise HTTPException(502, f"La página respondió HTTP {resp.status_code}.")
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

def unique_add(out, url, page_url):
    if not url:
        return
    url = html_lib.unescape(str(url).strip()).replace("\\/", "/")
    if url.startswith(("data:", "blob:", "javascript:")):
        return
    full = urljoin(page_url, url)
    p = urlparse(full)
    if p.scheme not in ("http", "https") or not p.hostname:
        return
    if full not in out:
        out.append(full)

@app.get("/", response_class=HTMLResponse)
async def home():
    with open(STATIC_DIR / "index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/manifest.webmanifest")
async def manifest():
    return JSONResponse({
        "name": "ImageNinja - Descargador de imágenes",
        "short_name": "ImageNinja",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0b1020",
        "theme_color": "#0b1020",
        "description": "Busca, selecciona y descarga imágenes de páginas web."
    }, media_type="application/manifest+json")

@app.get("/sw.js")
async def service_worker():
    with open(STATIC_DIR / "sw.js", "r", encoding="utf-8") as f:
        return Response(f.read(), media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})

@app.post("/api/scan")
async def scan(body: ScanRequest):
    page_url = validate_url(body.url)
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        data, ctype, final_url = await safe_get(client, page_url, max_bytes=MAX_PAGE_BYTES)
    if "html" not in ctype and not data.lstrip().startswith((b"<!doctype html", b"<html", b"<?xml")):
        if ctype.startswith("image/"):
            return {"page": final_url, "count": 1, "images": [{"url": final_url, "alt": "Imagen enlazada", "source": "URL directa"}]}
        raise HTTPException(415, "La URL no parece ser una página HTML ni una imagen.")

    soup = BeautifulSoup(data, "html.parser")
    found = []
    image_suffixes = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif", ".jfif")
    image_attrs = ("src", "data-src", "data-original", "data-lazy-src", "data-original-src",
                   "data-full", "data-image", "data-url", "data-file")

    # Imágenes normales y atributos usados por galerías con carga diferida.
    for tag in soup.find_all(("img", "source")):
        for attr in image_attrs:
            unique_add(found, tag.get(attr), final_url)
        for attr in ("srcset", "data-srcset"):
            srcset = tag.get(attr)
            if srcset:
                candidates = [part.strip().split()[0] for part in srcset.split(",") if part.strip()]
                if candidates:
                    unique_add(found, candidates[-1], final_url)

    for tag in soup.find_all("meta"):
        prop = (tag.get("property") or tag.get("name") or "").lower()
        if prop in {"og:image", "og:image:secure_url", "twitter:image", "twitter:image:src"}:
            unique_add(found, tag.get("content"), final_url)

    # Kemono y sitios similares pueden publicar el archivo original como enlace
    # de descarga en vez de usar un <img> directamente.
    for tag in soup.find_all("a", href=True):
        href = html_lib.unescape(tag.get("href", "").strip())
        path = urlparse(urljoin(final_url, href)).path.lower()
        is_media_path = any(part in path for part in ("/data/", "/attachment/", "/attachments/"))
        if path.endswith(image_suffixes) or tag.has_attr("download") or is_media_path:
            unique_add(found, href, final_url)

    # Algunos sitios insertan las URLs de adjuntos dentro de JSON o scripts.
    raw_html = html_lib.unescape(data.decode("utf-8", errors="ignore")).replace("\\/", "/")
    pattern = r"""(?i)(?:https?:)?//[^\\"'\\s<>]+?(?:/data/|/attachment/|/attachments/|/thumbnail/)[^\\"'\\s<>]*|/(?:data|attachment|attachments|thumbnail)/[^\\"'\\s<>]*"""
    for match in re.findall(pattern, raw_html):
        candidate = match.rstrip("),;")
        if candidate.startswith("//"):
            candidate = "https:" + candidate
        if candidate.startswith(("http://", "https://", "/")):
            unique_add(found, candidate, final_url)

    # Si también está el original, no mostrar una miniatura duplicada.
    originals = {u.replace("/thumbnail/", "/data/") for u in found if "/thumbnail/" not in u}
    found = [u for u in found if "/thumbnail/" not in u or u.replace("/thumbnail/", "/data/") not in originals]

    images = []
    for u in found[:MAX_IMAGES]:
        alt = ""
        for tag in soup.find_all("img"):
            raw = next((tag.get(attr) for attr in image_attrs if tag.get(attr)), None)
            if raw and urljoin(final_url, raw) == u:
                alt = (tag.get("alt") or "").strip()[:180]
                break
        filename = unquote(urlparse(u).path.rsplit("/", 1)[-1])
        label = alt or filename or "Imagen encontrada"
        images.append({"url": u, "alt": label[:180], "source": urlparse(u).netloc})
    return {"page": final_url, "count": len(images), "images": images, "truncated": len(found) > MAX_IMAGES}

@app.post("/api/download")
async def download(body: DownloadRequest):
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        tasks = []
        for u in body.urls[:30]:
            data, ctype, final_url = await safe_get(client, u, max_bytes=MAX_IMAGE_BYTES)
            if not ctype.startswith("image/") and not data.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"RIFF")):
                continue
            if ctype and ctype not in IMAGE_TYPES and not ctype.startswith("image/"):
                continue
            name = unquote(urlparse(final_url).path.rsplit("/", 1)[-1]) or f"imagen_{len(tasks)+1}.jpg"
            name = "".join(c for c in name if c.isalnum() or c in "._-")[:100] or f"imagen_{len(tasks)+1}.jpg"
            if "." not in name:
                ext = {"image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(ctype, ".jpg")
                name += ext
            tasks.append((name, data, ctype or "application/octet-stream"))
    if not tasks:
        raise HTTPException(422, "No se pudieron obtener imágenes válidas.")
    if len(tasks) == 1:
        name, data, ctype = tasks[0]
        return Response(data, media_type=ctype, headers={"Content-Disposition": f'attachment; filename="{name}"'})
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
        for i, (name, data, _) in enumerate(tasks, 1):
            z.writestr(f"{i:02d}_{name}", data)
    mem.seek(0)
    return StreamingResponse(mem, media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="imageninja.zip"'})

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
