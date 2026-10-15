"""OCR de imagens, GIFs e vídeos para o filtro de palavrões.

- Imagem: lê a imagem inteira.
- GIF: lê 3 frames (início, meio, fim). Se o GIF for grande, lê mais frames.
- Vídeo: 3 frames (início, meio, fim). Precisa do ffmpeg instalado; sem ele, o vídeo é pulado.

Opcional e desligado por padrão: liga com a chave OCR_SPACE_API_KEY no .env e o ajuste `ocr_ativo` = 1.
Usa o plano grátis do OCR.space: 1 MB por arquivo, 500 requisições por dia por IP. Por isso:
- cada frame é reduzido para caber em 1 MB;
- resultados já lidos ficam em cache (imagem repostada não gasta requisição);
- há um teto diário local, bem abaixo de 500.
"""

import asyncio
import hashlib
import io
import json
import logging
import os
import shutil
import subprocess
import tempfile
from collections import OrderedDict
from datetime import datetime

import aiohttp

from ..data import store
from . import rest

log = logging.getLogger("ocr")

OCR_URL = "https://api.ocr.space/parse/image"
LIMITE_OCR_BYTES = 900 * 1024  # abaixo de 1 MB, com folga
LIMITE_DIA = 450  # teto local; o plano grátis permite 500 por dia por IP
LIMITE_DOWNLOAD = 25 * 1024 * 1024
CACHE_MAX = 2000
MB = 1024 * 1024

_cache: "OrderedDict[str, str]" = OrderedDict()
_avisos_unicos: set[str] = set()
_sessao: aiohttp.ClientSession | None = None

try:
    from PIL import Image  # opcional
except ImportError:  # pragma: no cover
    Image = None


def _aviso_unico(chave: str, mensagem: str) -> None:
    if chave not in _avisos_unicos:
        _avisos_unicos.add(chave)
        log.warning(mensagem)


def ativo() -> bool:
    """Liga só com ajuste ocr_ativo=1 e chave no ambiente."""
    if store.ajuste("ocr_ativo") != "1":
        return False
    if not os.getenv("OCR_SPACE_API_KEY", "").strip():
        _aviso_unico("sem_chave", "OCR ligado no painel, mas OCR_SPACE_API_KEY não está no .env")
        return False
    return True


def quantidade_de_frames(tamanho: int) -> int:
    """Mais frames para GIF e vídeo grandes. Mínimo 3 (início, meio, fim)."""
    if tamanho <= 2 * MB:
        return 3
    if tamanho <= 5 * MB:
        return 5
    return 7


def posicoes(total: int, n: int) -> list[int]:
    """n posições espalhadas do início ao fim, sempre incluindo o primeiro e o último."""
    if total <= 0:
        return []
    if total <= n or n < 2:
        return list(range(total))
    return [round(i * (total - 1) / (n - 1)) for i in range(n)]


def reduzir(imagem: "Image.Image") -> bytes:
    """PNG que cabe no limite do OCR: reduz a escala até caber, e se não couber, usa JPEG."""
    im = imagem.convert("RGB")
    escala = 1.0
    for _ in range(8):
        alvo = im if escala == 1.0 else im.resize(
            (max(1, int(im.width * escala)), max(1, int(im.height * escala)))
        )
        buf = io.BytesIO()
        alvo.save(buf, format="PNG", optimize=True)
        if buf.tell() <= LIMITE_OCR_BYTES:
            return buf.getvalue()
        escala *= 0.75
    for qualidade in (80, 60, 40):
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=qualidade)
        if buf.tell() <= LIMITE_OCR_BYTES:
            return buf.getvalue()
    return buf.getvalue()  # último recurso: o OCR vai recusar e o aviso aparece no log


def _frames_de_gif(dados: bytes) -> list[bytes]:
    if Image is None:
        _aviso_unico("sem_pillow", "Pillow não instalado: GIF não será lido")
        return []
    im = Image.open(io.BytesIO(dados))
    total = getattr(im, "n_frames", 1)
    saida = []
    for i in posicoes(total, quantidade_de_frames(len(dados))):
        im.seek(i)
        saida.append(reduzir(im.copy()))
    return saida


def _frames_de_imagem(dados: bytes) -> list[bytes]:
    if Image is None:
        _aviso_unico("sem_pillow", "Pillow não instalado: imagens não serão lidas")
        return []
    return [reduzir(Image.open(io.BytesIO(dados)))]


def _frames_de_video(dados: bytes, n: int) -> list[bytes]:
    """Usa o ffmpeg, se existir. Sem ele, o vídeo é pulado (opcional)."""
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        _aviso_unico("sem_ffmpeg", "ffmpeg não instalado: vídeos não serão lidos")
        return []
    with tempfile.NamedTemporaryFile(suffix=".mp4") as arq:
        arq.write(dados)
        arq.flush()
        try:
            duracao = float(
                subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", arq.name],
                    capture_output=True, text=True, timeout=20, check=True,
                ).stdout.strip()
            )
        except (subprocess.SubprocessError, ValueError):
            return []
        saida = []
        for i in range(n):
            t = 0.0 if n == 1 else max(0.0, (duracao - 0.2) * i / (n - 1))
            try:
                quadro = subprocess.run(
                    ["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", arq.name, "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                    capture_output=True, timeout=30, check=True,
                ).stdout
            except subprocess.SubprocessError:
                continue
            if quadro and Image is not None:
                saida.append(reduzir(Image.open(io.BytesIO(quadro))))
        return saida


def amostrar(dados: bytes, tipo: str, nome: str) -> list[bytes]:
    """Frames a serem lidos de um anexo. Chamada síncrona: rodar com asyncio.to_thread."""
    tipo = (tipo or "").lower()
    nome = (nome or "").lower()
    if tipo == "image/gif" or nome.endswith(".gif"):
        return _frames_de_gif(dados)
    if tipo.startswith("image/"):
        return _frames_de_imagem(dados)
    if tipo.startswith("video/") or nome.endswith((".mp4", ".mov", ".webm")):
        return _frames_de_video(dados, 3)
    return []


def _orcamento_do_dia() -> dict:
    return json.loads(store.get_config("ocr:uso") or "{}")


def _consumir_orcamento() -> bool:
    hoje = datetime.now().strftime("%Y-%m-%d")
    uso = _orcamento_do_dia()
    if uso.get("dia") != hoje:
        uso = {"dia": hoje, "n": 0}
    if uso["n"] >= LIMITE_DIA:
        _aviso_unico(f"limite_{hoje}", f"limite diário de OCR atingido ({LIMITE_DIA}); imagens restantes não foram lidas")
        return False
    uso["n"] += 1
    store.set_config("ocr:uso", json.dumps(uso))
    return True


async def _chamar_api(png: bytes) -> str | None:
    """Uma requisição ao OCR.space. Devolve o texto, ou None se falhar."""
    global _sessao
    if _sessao is None or _sessao.closed:
        _sessao = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40))
    form = aiohttp.FormData()
    form.add_field("file", png, filename="frame.png", content_type="image/png")
    form.add_field("language", "por")
    form.add_field("OCREngine", "2")
    form.add_field("isOverlayRequired", "false")
    try:
        async with _sessao.post(OCR_URL, data=form, headers={"apikey": os.getenv("OCR_SPACE_API_KEY", "").strip()}) as resp:
            dados = json.loads(await resp.text() or "{}")
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        log.warning("OCR: falha de rede: %s", exc)
        return None
    if dados.get("IsErroredOnProcessing"):
        log.warning("OCR recusou um frame: %s", dados.get("ErrorMessage"))
        return None
    partes = [r.get("ParsedText", "") for r in dados.get("ParsedResults") or []]
    return "\n".join(p for p in partes if p).strip()


async def ler_frame(png: bytes) -> str | None:
    """Texto de um frame, com cache por conteúdo e teto diário."""
    chave = hashlib.sha256(png).hexdigest()
    if chave in _cache:
        _cache.move_to_end(chave)
        return _cache[chave]
    if not _consumir_orcamento():
        return None
    texto = await _chamar_api(png)
    if texto is not None:
        _cache[chave] = texto
        if len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)
    return texto


async def textos_de_anexos(anexos) -> list[str]:
    """Textos lidos de imagens, GIFs e vídeos de uma mensagem. Lista vazia se o OCR estiver desligado."""
    if not ativo():
        return []
    textos: list[str] = []
    for a in anexos or []:
        tipo = getattr(a, "content_type", None) or ""
        nome = getattr(a, "filename", None) or ""
        if not (tipo.startswith(("image/", "video/")) or nome.lower().endswith((".gif", ".mp4", ".mov", ".webm", ".png", ".jpg", ".jpeg", ".webp"))):
            continue
        dados = await rest.baixar_bytes(a.url, LIMITE_DOWNLOAD)
        if dados is None:
            continue
        frames = await asyncio.to_thread(amostrar, dados, tipo, nome)
        for frame in frames:
            texto = await ler_frame(frame)
            if texto:
                textos.append(texto)
    return textos
