import asyncio
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from telegram import Bot
from telegram.error import TelegramError
from database import get_all_active_licenses, update_monitor_state, get_monitor_state, get_cookies
from scraper import scrape_license, ALERT_STATUSES

logger = logging.getLogger(__name__)

BOT_TOKEN      = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL_SECONDS", "60"))
TZ             = ZoneInfo("America/Sao_Paulo")

STATUS_EMOJI = {
    "feita":              "✅",
    "odd_derretida":      "🔥",
    "robo_desligado":     "📴",
    "saldo_insuficiente": "💸",
    "conta_limitada":     "🚫",
    "valor_maximo":       "⬆️",
    "mercado_suspenso":   "⏸️",
    "verificacao":        "🪪",
    "outros":             "⚠️",
    "desconhecido":       "❓",
}

ALERT_TITLES = {
    "robo_desligado":     "📴 ROBÔ DESLIGADO",
    "saldo_insuficiente": "💸 SALDO INSUFICIENTE",
    "conta_limitada":     "🚫 CONTA LIMITADA",
    "verificacao":        "🪪 VERIFICAÇÃO NECESSÁRIA",
    "outros":             "⚠️ PROBLEMA DETECTADO",
}

def now_brt():
    return datetime.now(TZ).strftime("%d/%m - %H:%M")


def build_summary_text(label, bets, robot_status, last_check):
    robot_icon = "🟢" if robot_status == "LIGADO" else "🔴" if robot_status == "DESLIGADO" else "⚪"
    lines = [
        f"🐷 *Fiscal de Serviço Porco*",
        f"📋 *{label}*",
        f"Robô: {robot_icon} {robot_status}",
        f"🕐 Atualizado: {last_check}",
        f"",
        f"*Últimas apostas:*",
    ]
    if not bets:
        lines.append("_Nenhuma aposta encontrada._")
    else:
        for b in bets:
            emoji = STATUS_EMOJI.get(b["status_key"], "❓")
            lines.append(f"{emoji} {b['game']} — {b['time']}")
    return "\n".join(lines)


def build_alert_text(label, bets_with_problem, alert_type):
    title = ALERT_TITLES.get(alert_type, "⚠️ ALERTA")
    lines = [f"🚨 *{title}*", f"📋 *{label}*", ""]
    for b in bets_with_problem:
        emoji = STATUS_EMOJI.get(b["status_key"], "❓")
        lines.append(f"{emoji} {b['game']} — {b['time']}")
        lines.append(f"   _{b['status_text']}_")
        lines.append("")
    lines.append(f"🕐 {now_brt()}")
    return "\n".join(lines)


async def send_or_edit(bot, chat_id, message_id, text, alert=False):
    try:
        if message_id:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=int(message_id),
                text=text, parse_mode="Markdown"
            )
            return message_id
        else:
            msg = await bot.send_message(
                chat_id=chat_id, text=text,
                parse_mode="Markdown",
                disable_notification=(not alert)
            )
            return str(msg.message_id)
    except TelegramError:
        try:
            msg = await bot.send_message(
                chat_id=chat_id, text=text,
                parse_mode="Markdown",
                disable_notification=(not alert)
            )
            return str(msg.message_id)
        except Exception:
            return None


async def process_license(bot: Bot, lic: dict, first_scan: bool = False):
    lid      = lic["id"]
    label    = lic["label"]
    email    = lic["email"]
    password = lic["password"]
    chat_id  = lic["user_telegram_id"]

    state              = get_monitor_state(lid)
    prev_robot_status  = state.get("robot_status", "UNKNOWN")
    prev_summary_id    = state.get("summary_message_id")
    prev_alert_id      = state.get("alert_message_id")
    seen_ids           = set((state.get("last_bet_id") or "").split("||")) - {""}
    license_url        = state.get("license_url", "")

    now = now_brt()

    # Msg de "conectando" só no primeiro scan
    connecting_msg = None
    if first_scan:
        connecting_msg = await bot.send_message(
            chat_id=chat_id,
            text=f"🐷 *Fiscal de Serviço Porco*\n📋 *{label}*\n\n⏳ Conectando... aguarde até 30s.",
            parse_mode="Markdown"
        )

    cookies = get_cookies(lid)
    cf   = cookies.get("cf_clearance", "")
    r365 = cookies.get("r365_cookie", "")

    result = await scrape_license(email, password, cf, r365, license_url)

    if connecting_msg:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=connecting_msg.message_id)
        except Exception:
            pass

    if not result["success"]:
        err = result["error"]
        logger.error(f"[{label}] Falhou: {err}")
        if "expirada" in err or "Cookies não" in err:
            await bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ *Sessão expirada* — *{label}*\n\nUse /atualizar\\_cookies para renovar.",
                parse_mode="Markdown"
            )
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=f"🚨 *ERRO* — *{label}*\n\n`{err}`\n\n🕐 {now}",
                parse_mode="Markdown"
            )
        return

    robot_status  = result["robot_status"]
    bets          = result["bets"]
    new_license_url = result.get("license_url", license_url)

    # IDs das apostas atuais
    all_ids = "||".join(b["id"] for b in bets)

    # Apostas que ainda não vimos
    new_bets = [b for b in bets if b["id"] not in seen_ids]

    # ── Primeiro scan ─────────────────────────────────────
    if first_scan:
        robot_icon = "🟢" if robot_status == "LIGADO" else "🔴"
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ *Fiscal conectado!*\n"
                f"📋 *{label}*\n\n"
                f"Robô: {robot_icon} {robot_status}\n"
                f"📊 Últimas {len(bets)} apostas carregadas\n"
                f"🕐 {now}\n\n"
                f"Monitorando a partir de agora. 🐷"
            ),
            parse_mode="Markdown"
        )

    # ── Resumo (sempre edita silenciosamente) ──────────────
    summary_text   = build_summary_text(label, bets, robot_status, now)
    new_summary_id = await send_or_edit(
        bot, chat_id,
        None if first_scan else prev_summary_id,
        summary_text, alert=False
    )

    # ── Alertas só para apostas NOVAS com problema ─────────
    if not first_scan and new_bets:
        problems = [b for b in new_bets if b["status_key"] in ALERT_STATUSES]
        by_type: dict = {}
        for b in problems:
            by_type.setdefault(b["status_key"], []).append(b)

        # Robô desligado / voltou
        robot_just_off  = prev_robot_status == "LIGADO"    and robot_status == "DESLIGADO"
        robot_still_off = prev_robot_status == "DESLIGADO" and robot_status == "DESLIGADO"
        robot_back_on   = prev_robot_status == "DESLIGADO" and robot_status == "LIGADO"

        if robot_back_on:
            await bot.send_message(
                chat_id=chat_id,
                text=f"✅ *ROBÔ LIGADO NOVAMENTE*\n📋 *{label}*\n\n🕐 {now}",
                parse_mode="Markdown"
            )
            update_monitor_state(lid, alert_message_id=None)
            prev_alert_id = None

        if robot_just_off or robot_still_off:
            off_bets = [b for b in bets if b["status_key"] == "robo_desligado"]
            alert_text = build_alert_text(
                label,
                off_bets or [{"game": "—", "time": now, "status_text": "Robô Desligado", "status_key": "robo_desligado"}],
                "robo_desligado"
            )
            new_alert_id = await send_or_edit(bot, chat_id, prev_alert_id, alert_text, alert=robot_just_off)
            update_monitor_state(lid, alert_message_id=new_alert_id)

        for alert_type, bet_list in by_type.items():
            if alert_type == "robo_desligado":
                continue
            await bot.send_message(
                chat_id=chat_id,
                text=build_alert_text(label, bet_list, alert_type),
                parse_mode="Markdown"
            )

    # ── Persiste estado ────────────────────────────────────
    update_monitor_state(
        lid,
        last_bet_id=all_ids,
        robot_status=robot_status,
        summary_message_id=new_summary_id,
        last_check=now,
        license_url=new_license_url,
    )

    logger.info(f"[{label}] ✓ Robô: {robot_status} | Apostas: {len(bets)} | Novas: {len(new_bets)}")


async def first_scan_license(license_id: int):
    bot      = Bot(token=BOT_TOKEN)
    licenses = get_all_active_licenses()
    lic      = next((l for l in licenses if l["id"] == license_id), None)
    if lic:
        await process_license(bot, lic, first_scan=True)


async def monitor_loop():
    bot = Bot(token=BOT_TOKEN)
    logger.info(f"🐷 Fiscal iniciado. Intervalo: {CHECK_INTERVAL}s")
    while True:
        licenses = get_all_active_licenses()
        logger.info(f"Monitorando {len(licenses)} licença(s)...")
        await asyncio.gather(*[process_license(bot, lic) for lic in licenses], return_exceptions=True)
        await asyncio.sleep(CHECK_INTERVAL)
