import asyncio
import logging
import re
from datetime import datetime
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

LICENSES_URL = "https://www.robos365.com.br/tennis365/licenses.html"

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

def _today_prefix() -> str:
    """Retorna o prefixo de hoje no formato do site: '28/05'"""
    return datetime.now().strftime("%d/%m")


async def scrape_license(email: str, password: str,
                         cf_clearance: str = "", r365_cookie: str = "") -> dict:
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
            await page.goto(LICENSES_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)

            if "login" in page.url:
                await browser.close()
                return {"success": False, "error": "Sessão expirada. Use /atualizar_cookies no bot para renovar."}

            # Clica em Ajustes da primeira licença
            btn = await page.query_selector('a:has-text("Ajustes"), button:has-text("Ajustes")')
            if btn:
                await btn.click()
                await page.wait_for_timeout(2000)

            robot_status = await _get_robot_status(page)
            logs = await _get_logs(page)
            bets = await _get_bets(page)

            await browser.close()
            return {
                "success": True,
                "robot_status": robot_status,
                "bets": bets,
                "logs": logs,
                "error": None,
            }

        except Exception as e:
            logger.error(f"Erro no scrape: {e}")
            await browser.close()
            return {"success": False, "error": str(e)}


async def _get_robot_status(page) -> str:
    try:
        # Seletor exato: span.badge.bg-success ou span.badge.bg-danger
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


async def _get_logs(page) -> list:
    logs = []
    try:
        items = await page.query_selector_all(
            'li:has-text("desligado"), li:has-text("Verificação"), [class*="log-item"]'
        )
        for item in items[:5]:
            logs.append((await item.inner_text()).strip())
    except Exception:
        pass
    return logs


async def _get_bets(page) -> list:
    bets = []
    today = _today_prefix()  # ex: "28/05"

    try:
        # Clica em Apostas Processadas
        link = await page.query_selector(
            'a:has-text("Apostas Processadas"), li:has-text("Apostas Processadas")'
        )
        if link:
            await link.click()
            await page.wait_for_timeout(2000)

        # Muda a data de início para hoje
        today_full = datetime.now().strftime("%Y-%m-%d")
        date_inputs = await page.query_selector_all('input[type="date"]')
        if len(date_inputs) >= 1:
            await date_inputs[0].fill(today_full)
        if len(date_inputs) >= 2:
            await date_inputs[1].fill(today_full)

        # Clica no botão de busca
        search_btn = await page.query_selector('button[type="submit"], button.btn-primary, button:has(svg)')
        if search_btn:
            await search_btn.click()
            await page.wait_for_timeout(2000)

        rows = await page.query_selector_all('div:has(> div:has-text("vs"))')
        if not rows:
            rows = await page.query_selector_all('div.card, tr')

        for row in rows:
            text = await row.inner_text()

            # Filtra só apostas de hoje
            if today not in text:
                continue

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

    except Exception as e:
        logger.error(f"Erro ao extrair apostas: {e}")

    return bets
