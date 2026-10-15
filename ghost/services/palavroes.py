"""Filtro de palavrões por nível (baixo, médio, alto), com detecção de variações.

ALFABATO: tabela que leva cada caractere parecido com uma letra para a letra que ele imita.
- A maior parte vem do próprio Unicode: o nome oficial de cada caractere diz a letra
  (ex.: "MATHEMATICAL BOLD SMALL P" -> p, "CIRCLED LATIN SMALL LETTER U" -> u, "REGIONAL INDICATOR SYMBOL LETTER T" -> t).
- O resto é manual: letras gregas e cirílicas parecidas, letras com risco, números e símbolos comuns.

Na lista de palavras, qualquer caractere é aceito e comparado (letras com acento, cedilha, símbolos,
emojis, números). Pontuação comum (. , - _ * ' ") é tratada como separador, senão "p.u.t.a" não bateria.
Não cobre: imagens (sem OCR), mensagens editadas depois do envio, e caracteres que não se parecem com letra nenhuma.
"""

import functools
import logging
import re
import unicodedata
from pathlib import Path
from urllib.parse import unquote

log = logging.getLogger("palavroes")

NIVEIS = ("alto", "medio", "baixo")  # do mais grave para o mais leve
_SEPARADORES = set(".,-_*'\"`~^")  # pontuação comum: separador, não literal
_ESTILO = re.compile(r"(?:LETTER|SMALL|CAPITAL) ([A-Z])$")
_NOME_LATINO = ("LATIN", "MATHEMATICAL", "REGIONAL INDICATOR", "CIRCLED", "SQUARED", "PARENTHESIZED", "FULLWIDTH", "MODIFIER LETTER", "SYMBOL LETTER")
_NOME_EXCLUIDO = ("WITH", "GREEK", "CYRILLIC", "HEBREW", "ARABIC", "DIGIT", "NUMBER", "ROMAN", "BRAILLE", "FRACTION", "DOTLESS")

# Manual: o que o nome do caractere não resolve.
_MANUAL: dict[str, str] = {
    # Latino com risco, ligaduras e variantes (o nome não traz a letra direto)
    "ø": "o", "Ø": "o", "ł": "l", "Ł": "l", "đ": "d", "Đ": "d", "ħ": "h", "Ħ": "h",
    "ŧ": "t", "Ŧ": "t", "ı": "i", "ɨ": "i", "ƒ": "f", "ß": "ss", "æ": "ae", "Æ": "ae",
    "œ": "oe", "Œ": "oe", "þ": "th", "Þ": "th", "ð": "d", "Ð": "d", "ĸ": "k", "ŋ": "n", "Ŋ": "n",
    "ɛ": "e", "ɔ": "o", "ʋ": "v", "ɑ": "a", "ɡ": "g", "ɩ": "i", "ⅰ": "i", "ⅴ": "v", "ⅹ": "x",
    # Grego parecido com latino
    "α": "a", "β": "b", "γ": "y", "ε": "e", "ζ": "z", "η": "n", "ι": "i", "κ": "k", "μ": "u",
    "ν": "v", "ο": "o", "ρ": "p", "σ": "o", "τ": "t", "υ": "u", "φ": "o", "χ": "x", "ω": "w",
    "Α": "a", "Β": "b", "Ε": "e", "Ζ": "z", "Η": "h", "Ι": "i", "Κ": "k", "Μ": "m", "Ν": "n",
    "Ο": "o", "Ρ": "p", "Τ": "t", "Υ": "y", "Χ": "x", "ѵ": "v",
    # Cirílico parecido com latino
    "а": "a", "в": "b", "с": "c", "е": "e", "ё": "e", "і": "i", "ј": "j", "к": "k", "м": "m",
    "н": "h", "о": "o", "р": "p", "т": "t", "у": "y", "х": "x", "ѕ": "s", "һ": "h", "ԁ": "d",
    "ԛ": "q", "ԝ": "w", "ү": "y", "ұ": "y", "ҳ": "x", "ї": "i", "ѡ": "w", "ѣ": "e", "ԥ": "p",
    "э": "e", "я": "r", "ш": "w", "ч": "4", "г": "r", "п": "n", "д": "d", "л": "n", "ь": "b",
    # Números e símbolos (leet)
    "|": "l", "£": "l", "¥": "y", "ƒ": "f", "ª": "a", "º": "o",
}

# Números e símbolos só viram letra quando estão colados numa letra ("pu7a", "put@").
# Assim "10 anos", "4 pessoas" e "joao@gmail" continuam como estão.
_LEET_CONTEXTO = {
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "6": "g", "7": "t", "8": "b", "9": "g",
    "@": "a", "$": "s", "!": "i", "+": "t", "€": "e", "¢": "c", "¡": "i", "×": "x",
}

# Domínio de link é ignorado: só o caminho (e o nome do arquivo) é checado. "puta.com.br" não pune.
_DOMINIO = re.compile(
    r"(?:https?://)?(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\."
    r"(?:com|net|org|br|io|gg|me|tv|co|gov|edu|app|dev|xyz|site|link|ly|gl|be|ru|uk|us|de|fr|es|it|pt|jp|cn|ai)\b",
    re.IGNORECASE,
)


@functools.lru_cache(maxsize=1)
def _alfabato() -> dict[str, str]:
    """Gerado pelo nome oficial de cada caractere Unicode, mais a tabela manual."""
    tabela: dict[str, str] = {}
    for cp in range(0x80, 0x1FFFF):
        if 0xD800 <= cp <= 0xDFFF:
            continue
        ch = chr(cp)
        nome = unicodedata.name(ch, "")
        if not nome or any(x in nome for x in _NOME_EXCLUIDO):
            continue
        if not any(x in nome for x in _NOME_LATINO):
            continue
        m = _ESTILO.search(nome)
        if m:
            tabela[ch] = m.group(1).lower()
    tabela.update(_MANUAL)
    # Maiúscula e minúscula do mesmo caractere de tabela já saem minúsculas (normalização faz lower)
    return {k: v for k, v in tabela.items() if k != v}


@functools.lru_cache(maxsize=1)
def _trans():
    return str.maketrans(
        {k: v for k, v in _alfabato().items() if len(k) == 1 and k not in _LEET_CONTEXTO}
    )


def _normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c)).lower()
    saida: list[str] = []
    for i, c in enumerate(texto):
        if c in _LEET_CONTEXTO:
            prev = saida[-1] if saida else ""
            nxt = texto[i + 1] if i + 1 < len(texto) else ""
            if prev.isalpha() or nxt.isalpha():
                saida.append(_LEET_CONTEXTO[c])
                continue
        saida.append(c)
    return "".join(saida).translate(_trans())


def _tokens(palavra: str) -> list[str]:
    """Caracteres da palavra da lista. Espaço e pontuação comum viram separador. O resto é literal."""
    saida = []
    for c in _normalizar(palavra):
        if c.isspace() or c in _SEPARADORES:
            continue
        saida.append(c)
    return saida


def _padrao(palavra: str) -> str | None:
    tokens = _tokens(palavra)
    if not tokens:
        return None
    if len(tokens) <= 3:
        # Palavra curta: só como palavra inteira, sem separador no meio. Evita "c u" pegar "cu".
        return "".join(re.escape(t) + "+" for t in tokens)
    # Cada caractere aceita repetição; entre eles aceita qualquer separador (espaço, pontuação, zero-width).
    return r"[\W_]*".join(re.escape(t) + "+" for t in tokens)


def carregar(caminho: Path) -> dict[str, set[str]]:
    """Lê o arquivo de palavras. Seções [baixo], [medio], [alto] e [permitidas]; uma por linha; # comenta.
    [permitidas]: palavras inocentes que nunca são punidas, mesmo se baterem com um termo da lista."""
    niveis: dict[str, set[str]] = {n: set() for n in (*NIVEIS, "permitidas")}
    atual = None
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        l = linha.strip()
        if not l or l.startswith("#"):
            continue
        if l.startswith("[") and l.endswith("]"):
            nome = _normalizar(l[1:-1]).strip()
            atual = nome if nome in niveis else None
            continue
        if atual:
            niveis[atual].add(l)
    return niveis


class Filtro:
    def __init__(self, palavras: dict[str, set[str]]) -> None:
        self._regex: dict[str, re.Pattern] = {}
        self._suspeito: dict[str, re.Pattern] = {}
        self.total = 0
        self._permitidas = {re.sub(r"[^a-z]", "", _normalizar(w)) for w in palavras.get("permitidas", ())} - {""}
        for nivel in NIVEIS:
            padroes = sorted(
                {p for p in (_padrao(w) for w in palavras.get(nivel, ())) if p},
                key=len,
                reverse=True,
            )
            self.total += len(padroes)
            if padroes:
                self._regex[nivel] = re.compile(
                    r"(?<![a-z])(?:" + "|".join(padroes) + r")(?![a-z])"
                )
            # Versão sensível (sem limite de palavra): pega o termo colado em outras letras.
            # Nunca pune sozinha: só gera pedido de revisão para o dono.
            if padroes:
                self._suspeito[nivel] = re.compile("|".join(padroes))

    def _permitida_no_contexto(self, t: str, ini: int, fim: int) -> bool:
        a = ini
        while a > 0 and "a" <= t[a - 1] <= "z":
            a -= 1
        b = fim
        while b < len(t) and "a" <= t[b] <= "z":
            b += 1
        return t[a:b] in self._permitidas

    def suspeitos(self, texto: str) -> list[tuple[str, str]]:
        """Trechos que podem ser palavrão mas não têm certeza: (nível, trecho). Não pune ninguém."""
        if not texto:
            return []
        t = _normalizar(_DOMINIO.sub(" ", unquote(texto)))
        achados: list[tuple[str, str]] = []
        for nivel in NIVEIS:
            regex = self._suspeito.get(nivel)
            if regex is None:
                continue
            for m in regex.finditer(t):
                trecho = m.group(0)
                if self._permitida_no_contexto(t, m.start(), m.end()):
                    continue
                achados.append((nivel, trecho))
        return achados

    def achar(self, texto: str) -> tuple[str, str] | None:
        """Maior nível encontrado no texto: (nível, trecho). None se não houver."""
        if not texto:
            return None
        t = _normalizar(_DOMINIO.sub(" ", unquote(texto)))
        for nivel in NIVEIS:
            regex = self._regex.get(nivel)
            if regex is None:
                continue
            for m in regex.finditer(t):
                if not self._permitida_no_contexto(t, m.start(), m.end()):
                    return nivel, m.group(0)
        return None


_cache: dict = {"caminho": None, "mtime": None, "filtro": None}


def obter(caminho: Path) -> Filtro:
    """Filtro atual. Recarrega sozinho quando o arquivo muda, sem reiniciar o bot."""
    try:
        mtime = caminho.stat().st_mtime
    except OSError:
        if _cache["filtro"] is None or _cache["caminho"] != caminho:
            log.warning("arquivo de palavras não encontrado: %s", caminho)
            _cache.update(caminho=caminho, mtime=None, filtro=Filtro({}))
        return _cache["filtro"]
    if _cache["caminho"] != caminho or _cache["mtime"] != mtime:
        _cache.update(caminho=caminho, mtime=mtime, filtro=Filtro(carregar(caminho)))
        log.info("palavras carregadas: %d padrões", _cache["filtro"].total)
    return _cache["filtro"]


def melhor(filtro: Filtro, textos: list[str]) -> tuple[str, str] | None:
    """Maior nível entre todos os textos (mensagem, nomes de anexo, links)."""
    melhor_achado = None
    for texto in textos:
        achado = filtro.achar(texto)
        if achado is None:
            continue
        if melhor_achado is None or NIVEIS.index(achado[0]) < NIVEIS.index(melhor_achado[0]):
            melhor_achado = achado
    return melhor_achado


def tamanho_alfabato() -> int:
    return len(_alfabato())


def melhor_suspeito(filtro: Filtro, textos: list[str]) -> tuple[str, str] | None:
    """Trecho suspeito de maior nível (sem certeza). Só para pedir revisão."""
    melhor_achado = None
    for texto in textos:
        for nivel, trecho in filtro.suspeitos(texto):
            if melhor_achado is None or NIVEIS.index(nivel) < NIVEIS.index(melhor_achado[0]):
                melhor_achado = (nivel, trecho)
    return melhor_achado


def adicionar_permitida(caminho: Path, palavra: str) -> bool:
    """Acrescenta a palavra à seção [permitidas] do arquivo (cria a seção se não existir)."""
    texto = caminho.read_text(encoding="utf-8") if caminho.exists() else ""
    linhas = texto.splitlines()
    chave = _normalizar(palavra)
    if any(_normalizar(l) == chave for l in linhas if l.strip() and not l.strip().startswith(("#", "["))):
        return False
    if "[permitidas]" in [l.strip().lower() for l in linhas]:
        i = [l.strip().lower() for l in linhas].index("[permitidas]")
        linhas.insert(i + 1, palavra)
    else:
        linhas += ["", "[permitidas]", palavra]
    tmp = caminho.with_suffix(".tmp")
    tmp.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    tmp.replace(caminho)
    return True
