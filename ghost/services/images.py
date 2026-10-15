"""Downloads HTTPS limitados, com verificação DNS no próprio conector."""

import ipaddress
import socket
from urllib.parse import urljoin, urlsplit
import aiohttp


class PublicResolver(aiohttp.resolver.DefaultResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        addresses = await super().resolve(host, port, family)
        if not addresses or any(
            not ipaddress.ip_address(a["host"]).is_global for a in addresses
        ):
            raise OSError("endereço não público")
        return addresses


def validate_url(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise RuntimeError("use uma URL HTTPS pública, sem credenciais e na porta 443")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return
    if not address.is_global:
        raise RuntimeError("endereço não público")


async def download(url, limit, mime_types):
    connector = aiohttp.TCPConnector(resolver=PublicResolver())
    async with aiohttp.ClientSession(
        connector=connector, timeout=aiohttp.ClientTimeout(total=30)
    ) as http:
        for _ in range(5):
            validate_url(url)
            async with http.get(url, allow_redirects=False) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    url = urljoin(url, resp.headers.get("Location", ""))
                    continue
                if resp.status != 200:
                    raise RuntimeError(f"o link respondeu HTTP {resp.status}")
                mime = (
                    resp.headers.get("Content-Type", "").split(";")[0].lower().strip()
                )
                if mime not in mime_types:
                    raise RuntimeError("formato de imagem não permitido")
                if resp.content_length and resp.content_length > limit:
                    raise RuntimeError("imagem maior que o limite")
                result = bytearray()
                async for chunk in resp.content.iter_chunked(16384):
                    result.extend(chunk)
                    if len(result) > limit:
                        raise RuntimeError("imagem maior que o limite")
                return bytes(result), mime
        raise RuntimeError("redirecionamentos demais")
