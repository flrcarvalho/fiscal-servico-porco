import asyncio
import logging
import re
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

logger = logging.getLogger(__name__)

LOGIN_URL = "https://www.robos365.com.br/login.html"
LICENSES_URL = "https://robos365.com.br/tennis365/licenses.html"

# Status que conhecemos
STATUS_MAP = {
    "aposta feita":        ("✅", "feita"),
    "odd derretida":       ("🔥", "odd_derretida"),
    "robô desligado":      ("📴", "robo_desligado"),
    "robo desligado":      ("📴", "robo_desligado"),
    "saldo insuficiente":  ("💸", "saldo_insuficiente"),
    "conta limitada":      ("🚫", "conta_limitada"),
    "valor acima do máximo":("⬆️", "valor_maximo"),
    "mercado suspenso":    ("⏸️", "mercado_suspenso"),
    "verificação de identidade": ("🪪", "verificacao"),
    "outros":              ("⚠️", "outros"),
}

def classify_status(text: str):
    t = text.lower().strip()
    for key, val in STATUS_MAP.items():
        if key in t:
            return val
    return ("❓", "desconhecido")

ALERT_STATUSES = {"robo_desligado", "saldo_insuficiente", "conta_limitada", "outros", "verificacao"}
SILENT_STATUSES = {"feita", "odd_derretida", "valor_maximo", "mercado_suspenso"}


async def do_login(page, email: str, password: str) -> bool:
    """Faz login e retorna True se bem-sucedido."""
    try:
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)

        # Preenche email
        await page.fill('input[placeholder="E-mail"], input[type="email"]', email)
        await page.wait_for_timeout(500)

        # Preenche senha
        await page.fill('input[placeholder="Senha"], input[type="password"]', password)
        await page.wait_for_timeout(500)

        # Clica login
        await page.click('button:has-text("Login"), input[type="submit"]')
        await page.wait_for_timeout(3000)

        # Verifica se logou
        current = page.url
        if "login" not in current:
            return True

        # Verifica Cloudflare Turnstile
        if await page.query_selector(".cf-turnstile, iframe[src*='challenges.cloudflare']"):
            logger.warning("Cloudflare challenge detectado, aguardando resolução...")
            await page.wait_for_timeout(8000)
            await page.click('button:has-text("Login"), input[type="submit"]')
            await page.wait_for_timeout(4000)

        return "login" not in page.url

    except Exception as e:
        logger.error(f"Erro no login: {e}")
        return False


async def get_robot_status(page) -> str:
    """Retorna LIGADO, DESLIGADO ou UNKNOWN lendo a badge na página de licença."""
    try:
        badge = await page.query_selector(".badge, span:has-text('LIGADO'), span:has-text('DESLIGADO')")
        if badge:
            txt = (await badge.inner_text()).strip().upper()
            if "LIGADO" in txt:
                return "LIGADO"
            if "DESLIGADO" in txt:
                return "DESLIGADO"
    except Exception:
        pass
    return "UNKNOWN"


async def get_bets(page) -> list[dict]:
    """
    Navega para Apostas Processadas e extrai todas as apostas visíveis.
    Retorna lista de dicts: {id, game, time, bet_amount, status_text, status_key, emoji}
    """
    bets = []
    try:
        # Clica em "Apostas Processadas" no menu lateral
        link = await page.query_selector('a:has-text("Apostas Processadas"), li:has-text("Apostas Processadas")')
        if link:
            await link.click()
            await page.wait_for_timeout(2000)

        # Cada aposta é um card/row
        rows = await page.query_selector_all(
            'div.card, tr, [class*="bet"], [class*="aposta"]'
        )

        # Fallback: tenta pegar pelo texto dos cards
        if not rows:
            rows = await page.query_selector_all('div:has(> div:has-text("vs"))')

        for row in rows:
            text = await row.inner_text()
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            if not lines or "vs" not in text.lower():
                continue

            # Extrai jogo (linha com "vs")
            game_line = next((l for l in lines if " vs " in l.lower()), "")
            if not game_line:
                continue

            # Extrai hora (ex: "28/05 - 21:05")
            time_match = re.search(r"\d{2}/\d{2}\s*[-–]\s*\d{2}:\d{2}", text)
            time_str = time_match.group(0).strip() if time_match else ""

            # Extrai valor da aposta
            bet_match = re.search(r"Aposta:\s*([\d.,]+)", text)
            bet_amount = bet_match.group(1) if bet_match else "-"

            # Extrai status (última parte relevante)
            status_text = ""
            for l in reversed(lines):
                l_low = l.lower()
                if any(k in l_low for k in STATUS_MAP.keys()):
                    status_text = l
                    break
            if not status_text:
                status_text = lines[-1] if lines else "desconhecido"

            emoji, status_key = classify_status(status_text)

            # ID único = jogo + hora
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


async def scrape_license(email: str, password: str, license_url: str = None) -> dict:
    """
    Faz login, entra na licença e retorna:
    {
        success: bool,
        robot_status: str,
        bets: list,
        logs: list,
        error: str
    }
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
            ]
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()

        try:
            logged = await do_login(page, email, password)
            if not logged:
                await browser.close()
                return {"success": False, "error": "Falha no login (credenciais ou Cloudflare)"}

            # Vai para licenças
            target = license_url or LICENSES_URL
            await page.goto(target, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)

            # Clica na primeira licença se não tiver URL específica
            if not license_url:
                btn = await page.query_selector('a:has-text("Ajustes"), button:has-text("Ajustes")')
                if btn:
                    await btn.click()
                    await page.wait_for_timeout(2000)

            robot_status = await get_robot_status(page)

            # Pega logs operacionais
            logs = []
            log_items = await page.query_selector_all('[class*="log"], li:has-text("desligado"), li:has-text("Verificação")')
            for item in log_items[:5]:
                logs.append((await item.inner_text()).strip())

            bets = await get_bets(page)

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
