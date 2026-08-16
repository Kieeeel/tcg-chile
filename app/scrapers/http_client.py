"""Cliente HTTP con rate limiting, reintentos y peticiones condicionales.

Solo se conecta a las tiendas configuradas por el usuario: no hay telemetría
ni envío de datos a ningún servicio de terceros.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

from app import settings
from app.db.database import get_connection, transaction


@dataclass
class FetchResult:
    url: str
    status_code: int
    text: str
    from_cache: bool = False
    not_modified: bool = False
    content_hash: str = ""
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and (200 <= self.status_code < 300 or self.not_modified)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


class HttpClient:
    """Un cliente por tienda: mantiene su propio ritmo de peticiones."""

    def __init__(
        self,
        store_code: str,
        *,
        concurrency: Optional[int] = None,
        min_delay: Optional[float] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> None:
        scraping = settings.get("scraping", {}) or {}
        self.store_code = store_code
        self.min_delay = (
            min_delay if min_delay is not None else float(scraping.get("min_delay_seconds", 0.8))
        )
        self.max_retries = int(scraping.get("max_retries", 3))
        self.backoff = float(scraping.get("backoff_factor", 1.8))
        self.conditional = bool(scraping.get("conditional_requests", True))

        self._semaphore = asyncio.Semaphore(
            concurrency or int(scraping.get("concurrency_per_store", 4))
        )
        self._last_request = 0.0
        self._pace_lock = asyncio.Lock()

        default_headers = {
            "User-Agent": scraping.get("user_agent", "Mozilla/5.0"),
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
        }
        default_headers.update(headers or {})

        self._client = httpx.AsyncClient(
            headers=default_headers,
            timeout=timeout or float(scraping.get("request_timeout_seconds", 25)),
            follow_redirects=True,
            http2=False,
        )

        self.requests_made = 0
        self.requests_cached = 0

    async def __aenter__(self) -> "HttpClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    # -- ritmo -------------------------------------------------------------
    async def _pace(self) -> None:
        """Garantiza una separación mínima entre peticiones a la misma tienda."""
        async with self._pace_lock:
            elapsed = time.monotonic() - self._last_request
            if elapsed < self.min_delay:
                await asyncio.sleep(self.min_delay - elapsed)
            self._last_request = time.monotonic()

    # -- caché condicional --------------------------------------------------
    def _cache_entry(self, url: str) -> Optional[Dict[str, Any]]:
        if not self.conditional:
            return None
        with get_connection() as conn:
            row = conn.execute(
                "SELECT etag, last_modified, content_hash FROM http_cache WHERE url = ?",
                (url,),
            ).fetchone()
        return dict(row) if row else None

    def _store_cache(self, url: str, response: httpx.Response, content_hash: str) -> None:
        if not self.conditional:
            return
        try:
            with transaction() as conn:
                conn.execute(
                    """INSERT INTO http_cache (url, etag, last_modified, content_hash,
                                               status_code, fetched_at)
                       VALUES (?, ?, ?, ?, ?, datetime('now'))
                       ON CONFLICT(url) DO UPDATE SET
                           etag = excluded.etag,
                           last_modified = excluded.last_modified,
                           content_hash = excluded.content_hash,
                           status_code = excluded.status_code,
                           fetched_at = excluded.fetched_at""",
                    (
                        url,
                        response.headers.get("etag"),
                        response.headers.get("last-modified"),
                        content_hash,
                        response.status_code,
                    ),
                )
        except Exception:
            pass  # la caché es una optimización, nunca un bloqueo

    # -- petición -----------------------------------------------------------
    async def get(self, url: str, *, use_cache: bool = True) -> FetchResult:
        cached = self._cache_entry(url) if use_cache else None
        headers: Dict[str, str] = {}
        if cached:
            if cached.get("etag"):
                headers["If-None-Match"] = cached["etag"]
            if cached.get("last_modified"):
                headers["If-Modified-Since"] = cached["last_modified"]

        delay = self.min_delay
        last_error: Optional[str] = None

        for attempt in range(self.max_retries + 1):
            async with self._semaphore:
                await self._pace()
                try:
                    response = await self._client.get(url, headers=headers)
                except httpx.TimeoutException:
                    last_error = "timeout"
                except httpx.HTTPError as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                else:
                    self.requests_made += 1

                    if response.status_code == 304:
                        self.requests_cached += 1
                        return FetchResult(
                            url=url,
                            status_code=304,
                            text="",
                            from_cache=True,
                            not_modified=True,
                            content_hash=(cached or {}).get("content_hash", ""),
                        )

                    # Un 403 puede ser dos cosas muy distintas: un cortafuegos
                    # que rechaza esta petición concreta por ir demasiado
                    # seguida —transitorio, se arregla esperando— o un desafío
                    # antibots de Cloudflare, que es una decisión de la tienda
                    # y no cambia por insistir. Solo se reintenta el primero;
                    # ante el desafío se abandona a la primera.
                    desafio = "challenge" in (response.headers.get("cf-mitigated") or "")
                    reintentable = (
                        response.status_code == 429
                        or response.status_code >= 500
                        or (response.status_code == 403 and not desafio)
                    )

                    if reintentable:
                        last_error = f"HTTP {response.status_code}"
                        retry_after = response.headers.get("retry-after")
                        if retry_after and retry_after.isdigit():
                            delay = max(delay, float(retry_after))
                    elif response.status_code >= 400:
                        if desafio:
                            return FetchResult(
                                url=url,
                                status_code=response.status_code,
                                text="",
                                error="Cloudflare exige verificación de navegador",
                            )
                        return FetchResult(
                            url=url,
                            status_code=response.status_code,
                            text="",
                            error=f"HTTP {response.status_code}",
                        )
                    else:
                        text = response.text
                        digest = _hash(text)
                        self._store_cache(url, response, digest)
                        unchanged = bool(cached and cached.get("content_hash") == digest)
                        if unchanged:
                            self.requests_cached += 1
                        return FetchResult(
                            url=url,
                            status_code=response.status_code,
                            text=text,
                            from_cache=unchanged,
                            content_hash=digest,
                        )

            if attempt < self.max_retries:
                await asyncio.sleep(delay)
                delay *= self.backoff

        return FetchResult(url=url, status_code=0, text="", error=last_error or "error desconocido")

    async def get_json(self, url: str, *, use_cache: bool = True) -> tuple[FetchResult, Any]:
        result = await self.get(url, use_cache=use_cache)
        if not result.ok or not result.text:
            return result, None
        import json

        try:
            return result, json.loads(result.text)
        except json.JSONDecodeError as exc:
            result.error = f"JSON inválido: {exc}"
            return result, None
