import asyncio
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

BASE_URL     = "https://www.robos365.com.br/tennis365"
LICENSES_URL = f"{BASE_URL}/licenses.html"
TZ = ZoneInfo("America/Sao_Paulo")

STATUS_MAP = {
    "aposta feita":              ("✅", "feita"),
    "odd derretida":             ("🔥", "odd_derretida"),
    "robô desligado":            ("📴", "robo_desligado"),
    "robo desligado":            ("📴", "robo_desligado"),
    "saldo insuficiente":        ("💸", "saldo_insuficiente"),
    "conta limitada":            ("🚫", "conta_limitada"),
    "valor acima do máximo":     ("⬆️", "valor_maximo"),
    "mercado suspenso":          ("⏸️", "mercado_suspenso"),
    "verificação de identidade": ("🪪", "verificacao"),
    "outros":                    ("⚠️", "outros"),
}

def classify_status(text: str):
    t = text.lower().strip()
    for key, val in STATUS_MAP.items():
        if key in t:
            return val
    return ("❓", "desconhecido")

ALERT_STATUSES  = {"robo_desligado", "saldo_insuficiente", "conta_limitada", "outros", "verificacao"}
SILENT_STATUSES = {"feita", "odd_derretida", "valor_maximo", "mercado_suspenso"}

def now_brt():
    return datetime.now(TZ)


async def scrape_license(email: str, password: str,
                         cf_clearance: str = "", r365_cookie: str = "",
                         license_url: str = "") -> dict:
    if not cf_clearance or not r365_cookie:
        return {"success": False, "error": "Cookies não configurados. Use /atualizar_cookies no bot."}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )

        await context.add_cookies([
            {
                "name": "cf_clearance",
                "value": cf_clearance,
                "domain": ".robos365.com.br",
                "path": "/",
                "httpOnly": False,
                "secure": True,
                "sameSite": "None",
            },
            {
                "name": "R365",
                "value": r365_cookie,
                "domain": ".www.robos365.com.br",
                "path": "/",
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            },
        ])

        page = await context.new_page()

        try:
            # Vai para licenças
            await page.goto(LICENSES_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)

            if "login" in page.url:
                await browser.close()
                return {"success": False, "error": "Sessão expirada. Use /atualizar_cookies no bot para renovar."}

            # Se tiver URL específica da licença, vai direto
            if license_url:
                await page.goto(license_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2000)
            else:
                # Clica em Ajustes da primeira licença
                btn = await page.query_selector('a:has-text("Ajustes"), button:has-text("Ajustes")')
                if btn:
                    await btn.click()
                    await page.wait_for_timeout(2000)

            # Salva URL da licença para uso futuro
            current_url = page.url

            # Lê status do robô na Visão Geral
            robot_status = await _get_robot_status(page)

            # Navega para Apostas Processadas
            # Sai da página e volta para forçar atualização
            bets = await _get_bets_fresh(page)

            await browser.close()
            return {
                "success": True,
                "robot_status": robot_status,
                "bets": bets,
                "license_url": current_url,
                "error": None,
            }

        except Exception as e:
            logger.error(f"Erro no scrape: {e}")
            try:
                await browser.close()
            except Exception:
                pass
            return {"success": False, "error": str(e)}


async def _get_robot_status(page) -> str:
    try:
        badges = await page.query_selector_all("span.badge")
        for badge in badges:
            txt = (await badge.inner_text()).strip().upper()
            if "LIGADO" in txt:
                return "LIGADO"
            if "DESLIGADO" in txt:
                return "DESLIGADO"
    except Exception:
        pass
    return "UNKNOWN"


async def _get_bets_fresh(page) -> list:
    """
    Navega para Apostas Processadas saindo e voltando
    para forçar o site a atualizar os dados.
    Retorna as últimas 10 apostas.
    """
    bets = []
    try:
        # Clica em Visão Geral primeiro (sai da aba de apostas)
        overview = await page.query_selector('a:has-text("Visão Geral"), li:has-text("Visão Geral")')
        if overview:
            await overview.click()
            await page.wait_for_timeout(1000)

        # Agora vai para Apostas Processadas (forçando recarregar os dados)
        link = await page.query_selector('a:has-text("Apostas Processadas"), li:has-text("Apostas Processadas")')
        if link:
            await link.click()
            await page.wait_for_timeout(2000)

        # Pega as linhas com o seletor exato descoberto via DevTools
        rows = await page.query_selector_all("div.row.g-0.sh-lg-15")
        if not rows:
            rows = await page.query_selector_all("div.p-card")

        for row in rows:
            text = await row.inner_text()
            if " vs " not in text.lower():
                continue

            lines = [l.strip() for l in text.split("\n") if l.strip()]
            game_line = next((l for l in lines if " vs " in l.lower()), "")
            if not game_line:
                continue

            time_match = re.search(r"\d{2}/\d{2}\s*[-–]\s*\d{2}:\d{2}", text)
            time_str = time_match.group(0).strip() if time_match else ""

            bet_match = re.search(r"Aposta:\s*([\d.,]+)", text)
            bet_amount = bet_match.group(1) if bet_match else "-"

            status_text = ""
            for l in reversed(lines):
                if any(k in l.lower() for k in STATUS_MAP.keys()):
                    status_text = l
                    break
            if not status_text:
                status_text = lines[-1] if lines else "desconhecido"

            emoji, status_key = classify_status(status_text)
            bet_id = f"{game_line}|{time_str}"

            bets.append({
                "id": bet_id,
                "game": game_line,
                "time": time_str,
                "bet_amount": bet_amount,
                "status_text": status_text,
                "status_key": status_key,
                "emoji": emoji,
            })

        # Sempre retorna as 10 mais recentes (já vêm em ordem do mais novo)
        return bets[:10]

    except Exception as e:
        logger.error(f"Erro ao extrair apostas: {e}")
        return []
