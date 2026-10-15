"""Configurações do bot. Segredos e IDs do servidor vêm do ambiente (.env); padrões ficam em config.json."""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

# O pacote fica em ``ghost/``; dados de execução e o .env ficam na raiz do
# projeto, ao lado de main.py. Não dependa do diretório de onde o comando foi
# executado (systemd, Docker e painéis costumam usar outro cwd).
PACKAGE_DIR = Path(__file__).resolve().parent.parent
BASE_DIR = PACKAGE_DIR.parent
load_dotenv(BASE_DIR / ".env")
_CFG = json.loads(Path(__file__).with_name("config.json").read_text(encoding="utf-8"))


def _env_int(name: str, default: int = 0) -> int:
    valor = os.getenv(name, "").strip()
    return int(valor) if valor.isdigit() else default


def _cfg_int(section: str, key: str, default: int, minimo: int = 1, maximo: int = 86400) -> int:
    """Lê limites operacionais com fallback e faixa segura para configuração editável."""
    try:
        valor = int(_CFG.get(section, {}).get(key, default))
    except (TypeError, ValueError, AttributeError):
        valor = default
    return max(minimo, min(maximo, valor))


def _cfg_float(section: str, key: str, default: float, minimo: float = 0.0, maximo: float = 60.0) -> float:
    try:
        valor = float(_CFG.get(section, {}).get(key, default))
    except (TypeError, ValueError, AttributeError):
        valor = default
    return max(minimo, min(maximo, valor))


# Segredos e identificadores sensíveis: sempre do ambiente (.env), nunca no código.
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
# Ghost Agent: provedor gratuito (groq, openrouter, gemini ou custom). A chave é do provedor escolhido.
AGENT_PROVIDER = os.getenv("AGENT_PROVIDER", "groq").strip().lower() or "groq"
AGENT_API_KEY = os.getenv("AGENT_API_KEY", "").strip()
AGENT_MODEL = os.getenv("AGENT_MODEL", "").strip()  # vazio = modelo padrão do provedor
AGENT_BASE_URL = os.getenv("AGENT_BASE_URL", "").strip()  # só para AGENT_PROVIDER=custom
# O nome documentado é AGENT_CHANNEL_ID; AGENT_CANAL_ID era usado por versões antigas.
AGENT_CANAL_ID = _env_int("AGENT_CHANNEL_ID") or _env_int("AGENT_CANAL_ID")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")
PAYER_EMAIL = os.getenv("PAYER_EMAIL", "pagador@ghost.com")


def _env_path(name: str, default: Path) -> str:
    """Retorna caminho absoluto; caminhos relativos são relativos ao projeto."""
    valor = os.getenv(name, "").strip()
    if not valor:
        return str(default)
    if valor == ":memory:":
        return valor
    caminho = Path(valor).expanduser()
    return str(caminho if caminho.is_absolute() else BASE_DIR / caminho)


GOOGLE_DRIVE_ENABLED = os.getenv("GOOGLE_DRIVE_ENABLED", "false").strip().lower() in {
    "1", "true", "yes", "sim", "on"
}
GOOGLE_DRIVE_CREDENTIALS_FILE = _env_path(
    "GOOGLE_DRIVE_CREDENTIALS_FILE", BASE_DIR / "google-drive-credentials.json"
)
GOOGLE_DRIVE_TOKEN_FILE = _env_path(
    "GOOGLE_DRIVE_TOKEN_FILE", BASE_DIR / "data" / "google-drive-token.json"
)
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()
GOOGLE_DRIVE_COMPRESS = os.getenv("GOOGLE_DRIVE_COMPRESS", "true").strip().lower() in {
    "1", "true", "yes", "sim", "on"
}
GOOGLE_DRIVE_STATE_FILE = _env_path(
    "GOOGLE_DRIVE_STATE_FILE", BASE_DIR / "data" / "google-drive-state.json"
)


DB_PATH = _env_path("DB_PATH", BASE_DIR / "data" / "banco.db")
DIARIO_ARQUIVO = _CFG["diario"]["arquivo"]
ADMIN_ID = _env_int("ADMIN_ID")
GUILD_ID = _env_int("GUILD_ID")

PREFIX = _CFG["prefixo"]
NOME_LOJA_PADRAO = _CFG["nome_loja_padrao"]

SALES_CHANNEL_ID = _env_int("SALES_CHANNEL_ID")
LOGS_CHANNEL_ID = _env_int("LOGS_CHANNEL_ID")
FA_CHANNEL_ID = _env_int("FA_CHANNEL_ID")
NFA_CHANNEL_ID = _env_int("NFA_CHANNEL_ID")
SUPORTE_CHANNEL_ID = _env_int("SUPORTE_CHANNEL_ID")
NITRO_CHANNEL_ID = _env_int("NITRO_CHANNEL_ID")
CAPCUT_CHANNEL_ID = _env_int("CAPCUT_CHANNEL_ID")
SPOTIFY_CHANNEL_ID = _env_int("SPOTIFY_CHANNEL_ID")
PEDIDO_RESTOCK_CHANNEL_ID = _env_int("PEDIDO_RESTOCK_CHANNEL_ID")
AVISO_RESTOCK_CHANNEL_ID = _env_int("AVISO_RESTOCK_CHANNEL_ID")

CARGO_MEMBRO = _CFG["cargos"]["membro"]
CARGO_EQUIPE = _CFG["cargos"].get("equipe", "equipe")
CARGO_BOTS = _CFG["cargos"].get("bots", "bots")
CARGO_CLIENTE = _CFG["cargos"]["cliente"]
CARGOS_NIVEIS = _CFG[
    "cargos_niveis"
]  # o valor 0 é o cargo de cliente e nunca é removido

EDITAVEIS = _CFG["editaveis"]  # padrões das chaves que o banco pode sobrepor

MP_TIMEOUT_S = _CFG["mercado_pago_timeout_s"]

EFEMERA_S = _CFG[
    "efemera_s"
]  # o Discord só permite apagar mensagens de interação em até 900s
ENTREGA_DM_FECHA_S = _CFG["entrega_fecha_dm_s"]
RESERVA_MAX_MINUTOS = _CFG["reserva_max_minutos"]
RESTOCK_PEDIDOS_DIA = _CFG["restock_pedidos_por_dia"]

CORES = {nome: int(hexa.lstrip("#"), 16) for nome, hexa in _CFG["cores"].items()}

NUKE_JANELA_S = _CFG["antinuke"]["janela_s"]
NUKE_CREATE_LIMITE = _CFG["antinuke"]["limite_criacao"]
NUKE_DELETE_LIMITE = _CFG["antinuke"]["limite_exclusao"]
NUKE_BLOQUEIO_S = _CFG["antinuke"].get(
    "bloqueio_s", 30
)  # sem criar canal por esse tempo, o bloqueio termina

# Anti-raid: por autor, (máximo permitido, janela em segundos)
RAID_LIMITES = {tipo: tuple(par) for tipo, par in _CFG["raid"]["limites"].items()}
RAID_MENCOES_MAX = _CFG["raid"]["mencoes_max"]
RAID_AUDIT_JANELA_S = _CFG["raid"]["audit_janela_s"]
RAID_COOLDOWN_S = _CFG["raid"]["cooldown_s"]
RAID_REINCIDENCIA_S = _CFG["raid"]["reincidencia_s"]
RAID_ATENCAO_COMANDO_S = _cfg_int("raid", "atencao_comando_s", 30)
RAID_ATENCAO_APLICATIVO_S = _cfg_int("raid", "atencao_aplicativo_s", 180)
RAID_CANAIS_DUPLICADOS_MIN = _cfg_int("raid", "canais_duplicados_min", 3, 2, 100)
RAID_INSPECAO_INTERVALO_S = _cfg_int("raid", "inspecao_intervalo_s", 30, 10, 3600)
RAID_SCAN_CANAIS_OFFLINE_MAX = _cfg_int("raid", "scan_canais_offline_max", 60, 10, 500)
RAID_TOPICOS_DUPLICADOS_MIN = _cfg_int("raid", "topicos_duplicados_min", 3, 2, 100)
RAID_SPAM_OFFLINE_MENSAGENS_MIN = _cfg_int("raid", "spam_offline_mensagens_min", 12, 2, 1000)
RAID_SPAM_OFFLINE_CANAIS_MIN = _cfg_int("raid", "spam_offline_canais_min", 3, 2, 100)
RAID_SPAM_OFFLINE_AUTOR_MIN = _cfg_int("raid", "spam_offline_mensagens_autor_min", 20, 2, 1000)
RAID_AMOSTRA_MENSAGENS_OFFLINE = _cfg_int("raid", "amostra_mensagens_offline", 50, 1, 100)
RAID_HISTORICO_SALVO_MIN = _cfg_int("raid", "historico_salvo_min", 8, 1, 1000)
RAID_HISTORICO_SALVO_MAX = _cfg_int("raid", "historico_salvo_max", 50, 1, 1000)
RAID_HISTORICOS_ESVAZIADOS_MIN = _cfg_int("raid", "historicos_esvaziados_min", 4, 2, 100)
RAID_MENSAGEM_MIN_CARACTERES = _cfg_int("raid", "mensagem_min_caracteres", 12, 1, 500)
RAID_OFFLINE_BACKUP_MIN_CANAIS = _cfg_int("raid", "offline_backup_min_canais", 4, 1, 1000)
RAID_OFFLINE_AUSENTES_GRAVE_MIN = _cfg_int("raid", "offline_ausentes_grave_min", 3, 1, 1000)
RAID_OFFLINE_AUSENTES_GRAVE_PERCENTUAL = _cfg_int("raid", "offline_ausentes_grave_percentual", 34, 1, 100)
RAID_OFFLINE_CANAIS_EXTRAS_SINAL = _cfg_int("raid", "offline_canais_extras_sinal", 12, 1, 1000)
RAID_OFFLINE_CARGOS_AUSENTES_SINAL = _cfg_int("raid", "offline_cargos_ausentes_sinal", 4, 1, 1000)
RAID_OFFLINE_CARGOS_ALTERADOS_SINAL = _cfg_int("raid", "offline_cargos_alterados_sinal", 4, 1, 1000)
RAID_OFFLINE_CANAIS_ALTERADOS_SINAL = _cfg_int("raid", "offline_canais_alterados_sinal", 6, 1, 1000)
RAID_OFFLINE_DEVASTACAO_MIN_CANAIS = _cfg_int("raid", "offline_devastacao_min_canais", 5, 1, 1000)
RAID_OFFLINE_DEVASTACAO_AUSENTES_MIN = _cfg_int("raid", "offline_devastacao_ausentes_min", 5, 1, 1000)
RAID_OFFLINE_DEVASTACAO_PERCENTUAL = _cfg_int("raid", "offline_devastacao_percentual", 80, 1, 100)
RAID_OFFLINE_AUDITORIA_AUTOR_MIN = _cfg_int("raid", "offline_auditoria_autor_min", 5, 1, 100)

SPAM_MAX_MSGS = _CFG["spam"]["max_msgs"]
SPAM_JANELA_S = _CFG["spam"]["janela_s"]
SPAM_SEQUENCIA_MAX_MSGS = _cfg_int("spam", "sequencia_max_msgs", 5, 2, 1000)
SPAM_SEQUENCIA_INTERVALO_S = _cfg_float("spam", "sequencia_intervalo_s", 3.0, 0.1, 3600.0)
SPAM_TIMEOUT_S = _cfg_int("spam", "timeout_s", 60, 60, 28 * 24 * 3600)
MAX_THREAD_MSGS = _CFG["spam"]["max_msgs_thread"]
SPAM_MULTICANAL_MENSAGENS_MIN = _cfg_int("spam", "multicanal_mensagens_min", 6, 2, 1000)
SPAM_MULTICANAL_CANAIS_MIN = _cfg_int("spam", "multicanal_canais_min", 3, 2, 100)
SPAM_MULTICANAL_JANELA_S = _cfg_int("spam", "multicanal_janela_s", 30, 1, 3600)
SPAM_MULTICANAL_TEXTO_MIN = _cfg_int("spam", "multicanal_texto_min_caracteres", 8, 1, 500)
PENALIDADE_JANELA_S = _cfg_int("penalidades", "janela_ocorrencias_dias", 30, 1, 365) * 86400
PENALIDADE_TIMEOUT_MEDIO_S = _cfg_int("penalidades", "timeout_medio_s", 7 * 86400, 60, 28 * 86400)
PENALIDADE_TIMEOUT_GRAVE_S = _cfg_int("penalidades", "timeout_grave_s", 28 * 86400, 86400, 28 * 86400)

BACKUP_MANTER = _cfg_int("backup", "manter", 7, 1, 365)
BACKUP_RAID_JANELA_S = _cfg_int("backup", "janela_raid_s", 300)
BACKUP_DEBOUNCE_S = _cfg_int("backup", "debounce_s", 5, 0, 3600)
BACKUP_INTERVALO_S = _cfg_int("backup", "intervalo_s", 3600, 60, 86400)
BACKUP_PAUSA_ENTRE_CANAIS_S = _cfg_float("backup", "pausa_entre_canais_s", 0.6, 0.0, 30.0)
INCIDENTE_SILENCIO_S = _cfg_int("incidente", "silencio_s", 20)
INCIDENTE_RETROSPECTIVA_HORAS = _cfg_int("incidente", "retrospectiva_horas", 3, 1, 24)
INCIDENTE_CONCORRENCIA_EXCLUSOES = _cfg_int("incidente", "concorrencia_exclusoes", 2, 1, 10)
