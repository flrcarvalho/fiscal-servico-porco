import asyncio
import logging
import re
import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

BASE_URL     = "https://www.robos365.com.br"
LOGIN_URL    = f"{BASE_URL}/login.html"
LICENSES_URL = f"{BASE_URL}/tennis365/licenses.html"
TZ           = ZoneInfo("America/Sao_Paulo")

STATUS_MAP = {
    "aposta feita":              ("✅", "feita"),
    "odd derretida":             ("💩", "odd_derretida"),
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


def solve_turnstile(capsolver_key: str, page_url: str, site_key: str, timeout: int = 60) -> str:
    """Resolve Cloudflare Turnstile via CapSolver API."""
    # Cria tarefa
    resp = requests.post(
        "https://api.capsolver.com/createTask",
        json={
            "clientKey": capsolver_key,
            "task": {
                "type": "AntiTurnstileTaskProxyLess",
                "websiteURL": page_url,
                "websiteKey": site_key,
            }
        },
        timeout=30
    )
    data = resp.json()
    if data.get("errorId", 1) != 0:
        raise Exception(f"CapSolver createTask error: {data.get('errorDescription')}")
    
    task_id = data["taskId"]
    logger.info(f"CapSolver task criada: {task_id}")

    # Aguarda resolução
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        resp = requests.post(
            "https://api.capsolver.com/getTaskResult",
            json={"clientKey": capsolver_key, "taskId": task_id},
            timeout=30
        )
        result = resp.json()
        if result.get("status") == "ready":
            token = result["solution"]["token"]
            logger.info("CapSolver: token obtido!")
            return token
        if result.get("status") == "failed":
            raise Exception(f"CapSolver task falhou: {result.get('errorDescription')}")

    raise Exception("CapSolver timeout: não resolveu em 60s")


async def do_login_with_capsolver(page, email: str, password: str, capsolver_key: str) -> bool:
    """Faz login usando CapSolver para resolver o Turnstile."""
    try:
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)

        # Pega o sitekey do Turnstile
        site_key = await page.evaluate("""() => {
            const el = document.querySelector('[data-sitekey]') || 
                       document.querySelector('.cf-turnstile') ||
                       document.querySelector('iframe[src*="challenges.cloudflare"]');
            if (el) return el.getAttribute('data-sitekey') || el.src?.match(/k=([^&]+)/)?.[1] || null;
            return null;
        }""")

        if not site_key:
            # Tenta sitekey padrão do Cloudflare
            site_key = "0x4AAAAAAAf8m6nMXpvxJXtQ"
            logger.warning(f"Sitekey não encontrada, usando padrão: {site_key}")

        logger.info(f"Resolvendo Turnstile com CapSolver (sitekey: {site_key})...")

        # Resolve em thread separada (é síncrono)
        loop = asyncio.get_event_loop()
        token = await loop.run_in_executor(
            None, lambda: solve_turnstile(capsolver_key, LOGIN_URL, site_key)
        )

        # Injeta o token na página
        await page.evaluate(f"""(token) => {{
            // Tenta setar no campo hidden do Turnstile
            const inputs = document.querySelectorAll('input[name="cf-turnstile-response"]');
            inputs.forEach(i => i.value = token);
            
            // Tenta também via callback do Turnstile
            if (window.turnstile) {{
                window.turnstile.getResponse = () => token;
            }}
        }}""", token)

        await page.wait_for_timeout(500)

        # Preenche email e senha
        await page.fill('input[placeholder="E-mail"], input[type="email"]', email)
        await page.wait_for_timeout(300)
        await page.fill('input[placeholder="Senha"], input[type="password"]', password)
        await page.wait_for_timeout(300)

        # Clica em Login
        await page.click('button:has-text("Login"), input[type="submit"], button[type="submit"]')
        await page.wait_for_timeout(4000)

        if "login" not in page.url:
            logger.info("Login bem-sucedido!")
            return True

        logger.warning(f"Login falhou, URL atual: {page.url}")
        return False

    except Exception as e:
        logger.error(f"Erro no login com CapSolver: {e}")
        return False


async def scrape_license(email: str, password: str,
                         cf_clearance: str = "", r365_cookie: str = "",
                         license_url: str = "",
                         capsolver_key: str = "") -> dict:
    """
    Acessa o site. Tenta primeiro com cookies salvos.
    Se falhar (sessão expirada), faz login novo via CapSolver.
    """
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

        # Injeta cookies salvos se existirem
        if cf_clearance and r365_cookie:
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
            # Tenta acessar direto
            target = license_url or LICENSES_URL
            await page.goto(target, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)

            # Verifica se está logado — pela URL ou pelo conteúdo da página
            page_text = await page.inner_text("body")
            session_expired = (
                "login" in page.url or
                "Informe seus dados" in page_text or
                "E-mail" in page_text and "Senha" in page_text and "Login" in page_text
            )
            if session_expired:
                logger.info("Sessão expirada, fazendo novo login via CapSolver...")
                if not capsolver_key:
                    await browser.close()
                    return {"success": False, "error": "Sessão expirada e CapSolver não configurado. Use /atualizar_cookies."}

                logged = await do_login_with_capsolver(page, email, password, capsolver_key)
                if not logged:
                    await browser.close()
                    return {"success": False, "error": "Falha no login automático. Verifique email/senha."}

                # Salva novos cookies após login
                new_cookies = await context.cookies()
                cf_new  = next((c["value"] for c in new_cookies if c["name"] == "cf_clearance"), "")
                r365_new = next((c["value"] for c in new_cookies if c["name"] == "R365"), "")

                # Vai para licenças após login
                await page.goto(LICENSES_URL, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2000)
            else:
                cf_new = cf_clearance
                r365_new = r365_cookie

            # Entra na licença
            if not license_url:
                btn = await page.query_selector('a:has-text("Ajustes"), button:has-text("Ajustes")')
                if btn:
                    await btn.click()
                    await page.wait_for_timeout(2000)

            current_url  = page.url
            robot_status = await _get_robot_status(page)
            bets         = await _get_bets_fresh(page)

            # Se não achou apostas e status UNKNOWN, pode ser sessão inválida
            # Tenta re-login se tiver CapSolver
            if robot_status == "UNKNOWN" and not bets and capsolver_key:
                logger.info("Sem dados — tentando re-login via CapSolver...")
                logged = await do_login_with_capsolver(page, email, password, capsolver_key)
                if logged:
                    new_cookies = await context.cookies()
                    cf_new   = next((c["value"] for c in new_cookies if c["name"] == "cf_clearance"), cf_new)
                    r365_new = next((c["value"] for c in new_cookies if c["name"] == "R365"), r365_new)
                    await page.goto(LICENSES_URL, wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(2000)
                    btn2 = await page.query_selector('a:has-text("Ajustes"), button:has-text("Ajustes")')
                    if btn2:
                        await btn2.click()
                        await page.wait_for_timeout(2000)
                    robot_status = await _get_robot_status(page)
                    bets         = await _get_bets_fresh(page)
                    current_url  = page.url

            await browser.close()
            return {
                "success": True,
                "robot_status": robot_status,
                "bets": bets,
                "license_url": current_url,
                "new_cf_clearance": cf_new,
                "new_r365_cookie": r365_new,
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
    bets = []
    try:
        # Sai para Visão Geral e volta para forçar atualização
        overview = await page.query_selector('a:has-text("Visão Geral"), li:has-text("Visão Geral")')
        if overview:
            await overview.click()
            await page.wait_for_timeout(1000)

        link = await page.query_selector('a:has-text("Apostas Processadas"), li:has-text("Apostas Processadas")')
        if link:
            await link.click()
            await page.wait_for_timeout(2000)

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
            time_str   = time_match.group(0).strip() if time_match else ""

            bet_match  = re.search(r"Aposta:\s*([\d.,]+)", text)
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

        return bets[:10]

    except Exception as e:
        logger.error(f"Erro ao extrair apostas: {e}")
        return []
