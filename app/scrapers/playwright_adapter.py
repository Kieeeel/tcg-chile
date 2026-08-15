"""Adaptador para tiendas que cargan sus productos con JavaScript.

Usa Playwright con Chromium local. Es opcional: si la librería no está
instalada, la tienda registra un error claro y el resto del sistema sigue
funcionando normalmente.

    pip install playwright
    playwright install chromium
"""
from __future__ import annotations

from typing import Any, List, Optional

from app.scrapers.base import Category
from app.scrapers.html_adapter import HtmlStoreAdapter
from app.scrapers.parsing import clean_url, soup_of

_INSTALL_HINT = (
    "Playwright no está instalado. Ejecuta: pip install playwright && playwright install chromium"
)


class PlaywrightAdapter(HtmlStoreAdapter):
    """Igual que HtmlStoreAdapter, pero renderizando la página primero."""

    adapter_name = "playwright"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._browser: Any = None
        self._playwright: Any = None
        self._context: Any = None

    # -- ciclo de vida del navegador ----------------------------------------
    async def _ensure_browser(self) -> bool:
        if self._browser is not None:
            return True
        try:
            from playwright.async_api import async_playwright  # type: ignore
        except ImportError:
            self.report_error("network", self.base_url, _INSTALL_HINT)
            return False

        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=bool(self.config.get("headless", True))
            )
            self._context = await self._browser.new_context(
                user_agent=self.client._client.headers.get("User-Agent"),
                locale=self.config.get("locale", "es-CL"),
                viewport={"width": 1366, "height": 900},
            )
            return True
        except Exception as exc:  # noqa: BLE001
            self.report_error("network", self.base_url, f"No se pudo iniciar Chromium: {exc}")
            return False

    async def close(self) -> None:
        try:
            if self._context is not None:
                await self._context.close()
            if self._browser is not None:
                await self._browser.close()
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._context = self._browser = self._playwright = None

    # -- render --------------------------------------------------------------
    async def render(self, url: str) -> Optional[str]:
        if not await self._ensure_browser():
            return None

        page = await self._context.new_page()
        try:
            timeout = int(float(self.config.get("timeout_seconds", 30)) * 1000)
            await page.goto(url, wait_until=self.config.get("wait_until", "networkidle"),
                            timeout=timeout)

            wait_selector = self.config.get("wait_for_selector")
            if wait_selector:
                await page.wait_for_selector(wait_selector, timeout=timeout)

            for _ in range(int(self.config.get("scroll_times", 0))):
                await page.mouse.wheel(0, 2000)
                await page.wait_for_timeout(int(self.config.get("scroll_delay_ms", 600)))

            click_selector = self.config.get("load_more_selector")
            if click_selector:
                for _ in range(int(self.config.get("load_more_clicks", 5))):
                    button = await page.query_selector(click_selector)
                    if button is None:
                        break
                    await button.click()
                    await page.wait_for_timeout(int(self.config.get("load_more_delay_ms", 800)))

            return await page.content()
        except Exception as exc:  # noqa: BLE001
            self.report_error("network", url, f"Playwright: {type(exc).__name__}: {exc}")
            return None
        finally:
            await page.close()

    # -- reimplementamos las descargas para pasar por el navegador -----------
    async def _listing_items(self, url: str) -> tuple[List[Any], Optional[str], str]:
        html = await self.render(url)
        if not html:
            return [], None, url

        listing = self.config.get("listing") or {}
        soup = soup_of(html)
        items = soup.select(listing.get("item", "")) if listing.get("item") else []

        next_url = None
        pagination = self.config.get("pagination") or {}
        if pagination.get("mode") == "link" and pagination.get("next_selector"):
            from app.scrapers.parsing import absolute_url, select_attr

            next_url = absolute_url(url, select_attr(soup, pagination["next_selector"], "href"))
        return items, next_url, url

    async def parse_product(self, url: str, category: Optional[Category] = None):
        html = await self.render(url)
        if not html:
            return None
        return self.build_product(soup_of(html), clean_url(url), category)
