from __future__ import annotations
import io, ipaddress, socket, zipfile
from urllib.parse import urljoin, urlparse, unquote
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import httpx
from bs4 import BeautifulSoup

app = FastAPI(title="ImageNinja", docs_url=None, redoc_url=None)
MAX_PAGE_BYTES = 4 * 1024 * 1024
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_IMAGES = 80
TIMEOUT = httpx.Timeout(15.0, connect=7.0)
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ImageNinja/0.1; image fetcher)"}
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
        # Resolve and reject private, loopback, link-local, reserved and multicast IPs.
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
    url = url.strip()
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
    with open("static/index.html", "r", encoding="utf-8") as f:
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
    with open("static/sw.js", "r", encoding="utf-8") as f:
        return Response(f.read(), media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})

@app.post("/api/scan")
async def scan(body: ScanRequest):
    page_url = validate_url(body.url)
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        data, ctype, final_url = await safe_get(client, page_url, max_bytes=MAX_PAGE_BYTES)
    if "html" not in ctype and not data.lstrip().startswith((b"<!doctype html", b"<html", b"<?xml")):
        if ctype.startswith("image/"):
            return {"page": final_url, "images": [{"url": final_url, "alt": "Imagen enlazada", "source": "URL directa"}]}
        raise HTTPException(415, "La URL no parece ser una página HTML ni una imagen.")
    soup = BeautifulSoup(data, "html.parser")
    found = []
    for tag in soup.find_all("img"):
        src = tag.get("src") or tag.get("data-src") or tag.get("data-original")
        unique_add(found, src, final_url)
        srcset = tag.get("srcset") or tag.get("data-srcset")
        if srcset:
            # Keep the largest declared candidate.
            candidates = [part.strip().split()[0] for part in srcset.split(",") if part.strip()]
            if candidates:
                unique_add(found, candidates[-1], final_url)
    for tag in soup.find_all("meta"):
        prop = (tag.get("property") or tag.get("name") or "").lower()
        if prop in {"og:image", "og:image:secure_url", "twitter:image", "twitter:image:src"}:
            unique_add(found, tag.get("content"), final_url)
    # Also accept direct image links, useful for simple galleries.
    for tag in soup.find_all("a", href=True):
        href = tag["href"].split("?")[0].lower()
        if href.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif")):
            unique_add(found, tag["href"], final_url)
    images = []
    for u in found[:MAX_IMAGES]:
        # Avoid returning obvious tracking pixels / empty URLs.
        alt = ""
        for tag in soup.find_all("img"):
            raw = tag.get("src") or tag.get("data-src") or tag.get("data-original")
            if raw and urljoin(final_url, raw) == u:
                alt = (tag.get("alt") or "").strip()[:180]
                break
        images.append({"url": u, "alt": alt or "Imagen encontrada", "source": urlparse(u).netloc})
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
            # Avoid duplicate names inside the archive.
            z.writestr(f"{i:02d}_{name}", data)
    mem.seek(0)
    return StreamingResponse(mem, media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="imageninja.zip"'})

app.mount("/static", StaticFiles(directory="static"), name="static")
