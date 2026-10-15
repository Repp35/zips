"""Escrita no .env. Toda troca de valor que vive no .env passa aqui: grava no arquivo e já aplica em memória,
para valer sem reiniciar. (O GUILD_ID é a exceção: os comandos são sincronizados por servidor no boot.)"""

import logging
import os

from dotenv import dotenv_values, set_key

from ..config import settings

log = logging.getLogger("envs")


def valores_existentes(sufixo: str = "") -> dict[str, str]:
    """Lê somente chaves realmente presentes no arquivo, sem expor valores nos logs."""
    caminho = settings.BASE_DIR / ".env"
    valores = dotenv_values(caminho, interpolate=False)
    return {
        chave: str(valor).strip()
        for chave, valor in valores.items()
        if valor is not None and chave.endswith(sufixo)
    }


def gravar_se_existente(chave: str, valor: str | int) -> bool:
    """Atualiza uma chave do .env já existente; nunca cria uma chave nova."""
    if chave not in valores_existentes():
        return False
    gravar(chave, valor)
    return True


def gravar(chave: str, valor: str | int) -> None:
    texto = str(valor)
    if chave not in {"MP_ACCESS_TOKEN", "ADMIN_ID", "GUILD_ID"} and not chave.endswith(
        "_CHANNEL_ID"
    ):
        raise ValueError("chave de ambiente não editável")
    set_key(str(settings.BASE_DIR / ".env"), chave, texto)
    os.chmod(settings.BASE_DIR / ".env", 0o600)
    os.environ[chave] = texto
    if chave != "GUILD_ID" and hasattr(settings, chave):
        setattr(settings, chave, int(texto) if chave.endswith("_ID") else texto)
    log.info(".env: %s atualizado", chave)
