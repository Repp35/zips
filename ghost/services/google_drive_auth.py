"""Gera o arquivo OAuth persistente indicado em GOOGLE_DRIVE_TOKEN_FILE."""

from __future__ import annotations

import os
from pathlib import Path

from ..config import settings
from ..core.files import atomic_text

SCOPE = ["https://www.googleapis.com/auth/drive"]


def main() -> int:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Dependências ausentes. Instale requirements.txt primeiro.")
        return 2

    client_file = Path(settings.GOOGLE_DRIVE_CREDENTIALS_FILE)
    token_file = Path(settings.GOOGLE_DRIVE_TOKEN_FILE)
    if not client_file.is_file():
        print(f"Arquivo OAuth não encontrado: {client_file}")
        print("Baixe o JSON OAuth de aplicativo para computador ou preencha o modelo.")
        return 2
    if client_file.resolve() == token_file.resolve():
        print("O arquivo de credenciais e o arquivo de token precisam ser diferentes.")
        return 2

    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(client_file), SCOPE)
        credentials = flow.run_local_server(
            host="127.0.0.1",
            port=0,
            open_browser=True,
            access_type="offline",
            prompt="consent",
            success_message="Autorização concluída. Você pode fechar esta página.",
        )
        atomic_text(token_file, credentials.to_json())
        try:
            os.chmod(token_file, 0o600)
        except OSError:
            pass
    except Exception as exc:
        print(f"Não foi possível autorizar o Google Drive: {type(exc).__name__}: {exc}")
        return 1

    print(f"Token salvo em: {token_file}")
    print("O bot poderá sincronizar backups com a conta Google autorizada.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
