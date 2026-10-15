"""Cópia externa dos backups e mídias para uma pasta do Google Drive.

O download dos anexos acontece no snapshot local. Este serviço só começa depois
que os arquivos foram gravados, usa uploads retomáveis e mantém um índice
atômico para enviar novamente apenas arquivos alterados.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import mimetypes
import os
import shutil
import tempfile
from pathlib import Path

from ..config import settings
from ..core.files import atomic_text

log = logging.getLogger("google_drive")

_task: asyncio.Task | None = None
_requested = False
_sync_lock = asyncio.Lock()
_folder_name = "Ghost Bot Backups"
_folder_mime = "application/vnd.google-apps.folder"
_scope = ["https://www.googleapis.com/auth/drive"]
_nao_comprimir = {
    ".7z", ".aac", ".avi", ".bz2", ".flac", ".gif", ".gz", ".jpeg",
    ".jpg", ".m4a", ".mkv", ".mov", ".mp3", ".mp4", ".ogg", ".pdf",
    ".png", ".rar", ".webm", ".webp", ".xz", ".zip",
}


def agendar_sincronizacao() -> None:
    """Agenda uma sincronização em segundo plano sem bloquear o loop do bot."""
    global _task, _requested
    if not settings.GOOGLE_DRIVE_ENABLED:
        return
    _requested = True
    if _task is None or _task.done():
        _task = asyncio.create_task(_worker(), name="google-drive-backup-sync")


async def _worker() -> None:
    global _requested
    while _requested:
        _requested = False
        try:
            await sincronizar_backups()
        except Exception:
            # A cópia local já foi gravada; falha remota fica visível e será
            # tentada de novo no próximo backup, sem invalidar o backup local.
            log.exception("sincronização do Google Drive falhou; backup local preservado")


async def sincronizar_backups() -> dict:
    """Sincroniza uma cópia dos arquivos de `backups/` com o Google Drive."""
    if not settings.GOOGLE_DRIVE_ENABLED:
        return {"enviados": 0, "ignorados": 0, "desativado": True}
    async with _sync_lock:
        return await asyncio.to_thread(_sincronizar_sincrono)


def _ler_estado() -> dict:
    path = Path(settings.GOOGLE_DRIVE_STATE_FILE)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(state, dict) and isinstance(state.get("files"), dict):
            return state
    except (OSError, ValueError, TypeError):
        pass
    return {"version": 1, "folder_id": "", "files": {}}


def _gravar_estado(state: dict) -> None:
    atomic_text(
        settings.GOOGLE_DRIVE_STATE_FILE,
        json.dumps(state, ensure_ascii=False, separators=(",", ":")),
    )
    try:
        os.chmod(settings.GOOGLE_DRIVE_STATE_FILE, 0o600)
    except OSError:
        log.debug("não foi possível restringir as permissões do índice do Drive")


def _cliente():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError(
            "dependências Google ausentes; instale requirements.txt para ativar o Drive"
        ) from exc

    credentials_path = Path(settings.GOOGLE_DRIVE_CREDENTIALS_FILE)
    token_path = Path(settings.GOOGLE_DRIVE_TOKEN_FILE)
    if not credentials_path.is_file():
        raise RuntimeError(
            f"credencial OAuth não encontrada: {credentials_path}; preencha o JSON de cliente"
        )
    if not token_path.is_file():
        raise RuntimeError(
            f"token OAuth não encontrado: {token_path}; execute python -m ghost.services.google_drive_auth"
        )

    creds = Credentials.from_authorized_user_file(str(token_path), _scope)
    if not creds.valid:
        if not creds.expired or not creds.refresh_token:
            raise RuntimeError("token OAuth inválido; gere outro com google_drive_auth")
        creds.refresh(Request())
        atomic_text(token_path, creds.to_json())
        try:
            os.chmod(token_path, 0o600)
        except OSError:
            log.debug("não foi possível restringir as permissões do token Google")
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _query_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _folder_id(service, state: dict) -> str:
    if settings.GOOGLE_DRIVE_FOLDER_ID:
        folder_id = settings.GOOGLE_DRIVE_FOLDER_ID
        meta = service.files().get(
            fileId=folder_id, fields="id,name,mimeType,trashed", supportsAllDrives=True
        ).execute(num_retries=5)
        if meta.get("trashed") or meta.get("mimeType") != _folder_mime:
            raise RuntimeError("GOOGLE_DRIVE_FOLDER_ID não aponta para uma pasta ativa")
        return folder_id

    cached = str(state.get("folder_id") or "")
    if cached:
        try:
            meta = service.files().get(
                fileId=cached, fields="id,mimeType,trashed", supportsAllDrives=True
            ).execute(num_retries=5)
            if not meta.get("trashed") and meta.get("mimeType") == _folder_mime:
                return cached
        except Exception as exc:
            log.info("pasta de backups salva no índice não está acessível; procurando outra: %s", exc)

    q = (
        f"'root' in parents and name='{_query_escape(_folder_name)}' "
        f"and mimeType='{_folder_mime}' and trashed=false"
    )
    response = service.files().list(
        q=q, spaces="drive", pageSize=100,
        fields="files(id,name,mimeType,modifiedTime)",
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute(num_retries=5)
    found = response.get("files", [])
    if found:
        found.sort(key=lambda x: x.get("modifiedTime", ""))
        return found[-1]["id"]
    folder = service.files().create(
        body={"name": _folder_name, "mimeType": _folder_mime},
        fields="id,name", supportsAllDrives=True,
    ).execute(num_retries=5)
    log.info("pasta de backups criada no Google Drive")
    return folder["id"]


def _arquivos_remotos(service, folder_id: str) -> dict[str, dict]:
    por_caminho: dict[str, dict] = {}
    page_token = None
    while True:
        resultado = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            spaces="drive", pageSize=1000,
            fields="nextPageToken,files(id,name,appProperties,md5Checksum,modifiedTime,mimeType)",
            pageToken=page_token, supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute(num_retries=5)
        for item in resultado.get("files", []):
            path = (item.get("appProperties") or {}).get("ghost_path")
            if not path:
                continue
            anterior = por_caminho.get(path)
            if anterior is None or item.get("modifiedTime", "") > anterior.get("modifiedTime", ""):
                por_caminho[path] = item
        page_token = resultado.get("nextPageToken")
        if not page_token:
            break
    return por_caminho


def _hashes(path: Path) -> tuple[str, str]:
    sha = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as stream:
        while bloco := stream.read(1024 * 1024):
            sha.update(bloco)
            md5.update(bloco)
    return sha.hexdigest(), md5.hexdigest()


def _preparar_upload(source: Path, temp_dir: Path, compress: bool) -> tuple[Path, bool]:
    if not compress or source.suffix.casefold() in _nao_comprimir or source.stat().st_size < 1024:
        return source, False
    target = temp_dir / f"{hashlib.sha256(str(source).encode()).hexdigest()}.gz"
    with source.open("rb") as original, target.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", filename="", compresslevel=6, mtime=0) as zipped:
            shutil.copyfileobj(original, zipped, length=1024 * 1024)
    if target.stat().st_size <= source.stat().st_size * 0.95:
        return target, True
    target.unlink(missing_ok=True)
    return source, False


def _subir(service, folder_id: str, rel: str, source: Path, file_id: str | None,
           sha: str, upload_path: Path, compressed: bool) -> dict:
    from googleapiclient.http import MediaFileUpload

    mime_type = "application/gzip" if compressed else (mimetypes.guess_type(rel)[0] or "application/octet-stream")
    metadata = {
        "name": rel + ".gz" if compressed else rel,
        "mimeType": mime_type,
        "appProperties": {
            "ghost_path": rel,
            "ghost_sha256": sha,
            "ghost_compressed": "1" if compressed else "0",
        },
    }
    media = MediaFileUpload(
        str(upload_path), mimetype=mime_type, resumable=True,
        chunksize=8 * 1024 * 1024,
    )
    if file_id:
        request = service.files().update(
            fileId=file_id, body=metadata, media_body=media,
            fields="id,name,appProperties,md5Checksum,size,modifiedTime",
            supportsAllDrives=True,
        )
    else:
        metadata["parents"] = [folder_id]
        request = service.files().create(
            body=metadata, media_body=media,
            fields="id,name,appProperties,md5Checksum,size,modifiedTime",
            supportsAllDrives=True,
        )
    response = None
    while response is None:
        _, response = request.next_chunk(num_retries=5)
    return response


def _baixar_arquivos_sincrono(caminhos: list[str]) -> dict[str, str | None]:
    root = Path(settings.BASE_DIR).resolve()
    backup_root = (root / "backups").resolve()
    destinos = {}
    for rel in dict.fromkeys(caminhos):
        destination = (root / rel).resolve()
        if not destination.is_relative_to(backup_root):
            raise ValueError("caminho de restauração fora da pasta de backups")
        destinos[rel] = str(destination) if destination.is_file() else None
    faltantes = [rel for rel, path in destinos.items() if path is None]
    if not faltantes:
        return destinos

    try:
        from googleapiclient.http import MediaIoBaseDownload
    except ImportError as exc:
        raise RuntimeError("instale as dependências Google para baixar a mídia do Drive") from exc

    state = _ler_estado()
    service = _cliente()
    folder_id = _folder_id(service, state)
    remotos = _arquivos_remotos(service, folder_id)
    (root / "data").mkdir(parents=True, exist_ok=True)
    for rel in faltantes:
        item = remotos.get(rel)
        if item is None:
            continue
        destination = Path(root / rel).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, raw_name = tempfile.mkstemp(prefix="drive-download-", dir=root / "data")
        os.close(fd)
        raw_path = Path(raw_name)
        fd, final_name = tempfile.mkstemp(prefix="drive-restore-", dir=destination.parent)
        os.close(fd)
        final_path = Path(final_name)
        try:
            with raw_path.open("wb") as output:
                request = service.files().get_media(fileId=item["id"], supportsAllDrives=True)
                downloader = MediaIoBaseDownload(output, request, chunksize=8 * 1024 * 1024)
                done = False
                while not done:
                    _, done = downloader.next_chunk(num_retries=5)
                output.flush()
                os.fsync(output.fileno())
            props = item.get("appProperties") or {}
            if props.get("ghost_compressed") == "1":
                with gzip.open(raw_path, "rb") as source, final_path.open("wb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
                    target.flush()
                    os.fsync(target.fileno())
            else:
                os.replace(raw_path, final_path)
            os.replace(final_path, destination)
            destinos[rel] = str(destination)
            state.setdefault("files", {})[rel] = {
                "id": item["id"],
                "sha256": props.get("ghost_sha256", ""),
                "upload_md5": item.get("md5Checksum", ""),
                "compressed": props.get("ghost_compressed") == "1",
            }
            _gravar_estado(state)
            log.info("arquivo de backup recuperado do Google Drive: %s", rel)
        finally:
            raw_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
    if not state.get("folder_id"):
        state["folder_id"] = folder_id
        _gravar_estado(state)
    return destinos


async def baixar_arquivos_backup(caminhos: list[str]) -> dict[str, str | None]:
    """Recupera em lote arquivos locais ausentes, descomprimindo-os se necessário."""
    if not settings.GOOGLE_DRIVE_ENABLED:
        return {path: None for path in caminhos}
    return await asyncio.to_thread(_baixar_arquivos_sincrono, caminhos)


def _sincronizar_sincrono() -> dict:
    root = Path(settings.BASE_DIR).resolve()
    backups = (root / "backups").resolve()
    if not backups.is_dir():
        return {"enviados": 0, "ignorados": 0}
    credentials_path = Path(settings.GOOGLE_DRIVE_CREDENTIALS_FILE)
    token_path = Path(settings.GOOGLE_DRIVE_TOKEN_FILE)
    if credentials_path.resolve() == token_path.resolve():
        raise RuntimeError("arquivo de credenciais e arquivo de token precisam ser diferentes")

    state = _ler_estado()
    service = _cliente()
    folder_id = _folder_id(service, state)
    state["folder_id"] = folder_id
    state.setdefault("files", {})
    remotos = _arquivos_remotos(service, folder_id)
    files = sorted(
        (p for p in backups.rglob("*") if p.is_file()),
        key=lambda p: p.relative_to(root).as_posix(),
    )
    enviados = ignorados = 0
    (root / "data").mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="google-drive-", dir=root / "data") as temp:
        temp_dir = Path(temp)
        for source in files:
            rel = source.relative_to(root).as_posix()
            sha, _ = _hashes(source)
            previous = state["files"].get(rel) or {}
            remote = remotos.get(rel)
            remote_id = (remote or {}).get("id") or previous.get("id")
            remote_hash = ((remote or {}).get("appProperties") or {}).get("ghost_sha256")
            prior_md5 = previous.get("upload_md5")
            remote_md5 = (remote or {}).get("md5Checksum")
            if (
                previous.get("sha256") == sha
                and remote_id
                and remote
                and remote_hash == sha
                and (not prior_md5 or not remote_md5 or prior_md5 == remote_md5)
            ):
                previous.update({"id": remote_id, "sha256": sha})
                state["files"][rel] = previous
                ignorados += 1
                continue

            upload_path, compressed = _preparar_upload(
                source, temp_dir, settings.GOOGLE_DRIVE_COMPRESS
            )
            _, upload_md5 = _hashes(upload_path)
            if (
                remote
                and remote_hash == sha
                and (remote.get("appProperties") or {}).get("ghost_compressed")
                    == ("1" if compressed else "0")
                and remote_md5 == upload_md5
            ):
                state["files"][rel] = {
                    "id": remote["id"], "sha256": sha,
                    "upload_md5": upload_md5, "compressed": compressed,
                }
                _gravar_estado(state)
                ignorados += 1
                continue

            response = _subir(
                service, folder_id, rel, source,
                remote_id if remote else None, sha,
                upload_path, compressed,
            )
            state["files"][rel] = {
                "id": response["id"], "sha256": sha,
                "upload_md5": response.get("md5Checksum") or upload_md5,
                "compressed": compressed,
                "original_size": source.stat().st_size,
                "stored_size": int(response.get("size") or upload_path.stat().st_size),
            }
            _gravar_estado(state)
            if remote_id and remote and remote_id != response["id"]:
                log.warning("Drive substituiu o ID remoto do arquivo %s", rel)
            enviados += 1
            log.info(
                "backup sincronizado no Google Drive: %s (%d → %d bytes%s)",
                rel, source.stat().st_size,
                int(response.get("size") or upload_path.stat().st_size),
                ", gzip sem perda" if compressed else "",
            )
    log.info("Google Drive: enviados=%d sem alteração=%d", enviados, ignorados)
    return {"enviados": enviados, "ignorados": ignorados, "pasta_id": folder_id}
