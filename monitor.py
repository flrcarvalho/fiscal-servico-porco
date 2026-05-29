import asyncio
import logging
import os
from datetime import datetime
from telegram import Bot
from telegram.error import TelegramError
from database import get_all_active_licenses, update_monitor_state, get_monitor_state
from scraper import scrape_license, ALERT_STATUSES, SILENT_STATUSES

logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL_SECONDS", "120"))  # 2 min padrão

# Emojis de status
STATUS_EMOJI = {
    "feita":           "✅",
    "odd_derretida":   "🔥",
    "robo_desligado":  "📴",
    "saldo_insuficiente": "💸",
    "conta_limitada":  "🚫",
    "valor_maximo":    "⬆️",
    "mercado_suspenso":"⏸️",
    "verificacao":     "🪪",
    "outros":          "⚠️",
    "desconhecido":    "❓",
}

ALERT_TITLES = {
    "robo_desligado":     "📴 ROBÔ DESLIGADO",
    "saldo_insuficiente": "💸 SALDO INSUFICIENTE",
    "conta_limitada":     "🚫 CONTA LIMITADA",
    "verificacao":        "🪪 VERIFICAÇÃO NECESSÁRIA",
    "outros":             "⚠️ PROBLEMA DETECTADO",
}


def build_summary_text(label: str, bets: list, robot_status: str, last_check: str) -> str:
    """Monta a mensagem de resumo editável."""
    robot_icon = "🟢" if robot_status == "LIGADO" else "🔴" if robot_status == "DESLIGADO" else "⚪"
    lines = [
        f"🐷 *Fiscal de Serviço Porco*",
        f"📋 *{label}*",
        f"Robô: {robot_icon} {robot_status}",
        f"🕐 Última checagem: {last_check}",
        f"",
        f"*Apostas do dia:*",
    ]

    if not bets:
        lines.append("_Nenhuma aposta registrada ainda._")
    else:
        for b in bets:
            emoji = STATUS_EMOJI.get(b["status_key"], "❓")
            lines.append(f"{emoji} {b['game']} — {b['time']}")

    return "\n".join(lines)


def build_alert_text(label: str, bets_with_problem: list, alert_type: str) -> str:
    """Monta a mensagem de alerta."""
    title = ALERT_TITLES.get(alert_type, "⚠️ ALERTA")
    lines = [
        f"🚨 *{title}*",
        f"📋 *{label}*",
        f"",
    ]
    for b in bets_with_problem:
        emoji = STATUS_EMOJI.get(b["status_key"], "❓")
        lines.append(f"{emoji} {b['game']} — {b['time']}")
        lines.append(f"   Status: {b['status_text']}")
        lines.append("")

    lines.append(f"🕐 {datetime.now().strftime('%d/%m - %H:%M')}")
    return "\n".join(lines)


async def send_or_edit(bot: Bot, chat_id: str, message_id: str | None, text: str, alert=False) -> str | None:
    """Envia nova mensagem ou edita existente. Retorna o message_id."""
    try:
        if message_id:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=int(message_id),
                text=text,
                parse_mode="Markdown"
            )
            return message_id
        else:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
                disable_notification=(not alert)
            )
            return str(msg.message_id)
    except TelegramError as e:
        logger.error(f"Telegram error: {e}")
        # Se mensagem não existe mais, envia nova
        try:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
                disable_notification=(not alert)
            )
            return str(msg.message_id)
        except Exception:
            return None


async def process_license(bot: Bot, lic: dict, first_scan: bool = False):
    """Processa uma licença: faz scrape e atualiza Telegram."""
    lid = lic["id"]
    label = lic["label"]
    email = lic["email"]
    password = lic["password"]
    chat_id = lic["user_telegram_id"]

    state = get_monitor_state(lid)
    prev_robot_status = state.get("robot_status", "UNKNOWN")
    prev_summary_msg_id = state.get("summary_message_id")
    prev_alert_msg_id = state.get("alert_message_id")
    seen_bets = set((state.get("last_bet_id") or "").split("||")) - {""}

    now = datetime.now().strftime("%d/%m - %H:%M")
    logger.info(f"[{label}] {'🔍 Primeiro scan!' if first_scan else 'Checagem de rotina...'}")

    # ── Se for primeiro scan, avisa que está conectando ───
    connecting_msg = None
    if first_scan:
        connecting_msg = await bot.send_message(
            chat_id=chat_id,
            text=(
                f"🐷 *Fiscal de Serviço Porco*\n"
                f"📋 *{label}*\n\n"
                f"⏳ Conectando ao Robôs365 pela primeira vez...\n"
                f"Isso pode levar até 30 segundos."
            ),
            parse_mode="Markdown"
        )

    result = await asyncio.get_event_loop().run_in_executor(
        None, lambda: asyncio.run(scrape_license(email, password))
    )

    # Remove msg de "conectando" se existia
    if connecting_msg:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=connecting_msg.message_id)
        except Exception:
            pass

    if not result["success"]:
        logger.error(f"[{label}] Scrape falhou: {result['error']}")
        err_text = (
            f"🚨 *ERRO NO FISCAL*\n"
            f"📋 *{label}*\n\n"
            f"Não foi possível acessar o site:\n`{result['error']}`\n\n"
            f"🕐 {now}"
        )
        if first_scan:
            err_text = (
                f"❌ *Falha na conexão inicial*\n"
                f"📋 *{label}*\n\n"
                f"Não consegui fazer login. Verifique seu e-mail e senha.\n\n"
                f"`{result['error']}`\n\n"
                f"Use /remover e /add\\_licenca para corrigir as credenciais."
            )
        await send_or_edit(bot, chat_id, None, err_text, alert=True)
        return

    robot_status = result["robot_status"]
    bets = result["bets"]

    # ── Detecta novas apostas ──────────────────────────────
    # No primeiro scan, todas as apostas são "vistas" — não gera alertas retroativos
    if first_scan:
        new_bets = []
    else:
        new_bets = [b for b in bets if b["id"] not in seen_bets]

    all_bet_ids = "||".join(b["id"] for b in bets)

    # ── Atualiza mensagem de resumo ────────────────────────
    # No primeiro scan: envia COM notificação para o usuário saber que funcionou
    summary_text = build_summary_text(label, bets, robot_status, now)
    if first_scan:
        # Mensagem inicial especial com confirmação
        robot_icon = "🟢" if robot_status == "LIGADO" else "🔴" if robot_status == "DESLIGADO" else "⚪"
        confirm_lines = [
            f"✅ *Fiscal conectado com sucesso!*",
            f"📋 *{label}*",
            f"",
            f"Robô: {robot_icon} {robot_status}",
            f"📊 Apostas encontradas hoje: *{len(bets)}*",
            f"🕐 {now}",
            f"",
            f"A partir de agora vou monitorar essa licença e te avisar se algo mudar. 🐷",
        ]
        confirm_text = "\n".join(confirm_lines)
        await bot.send_message(
            chat_id=chat_id,
            text=confirm_text,
            parse_mode="Markdown"
        )

    # Envia/edita resumo completo (silencioso nos próximos ciclos)
    new_summary_id = await send_or_edit(
        bot, chat_id, prev_summary_msg_id if not first_scan else None,
        summary_text,
        alert=False  # resumo sempre silencioso
    )

    # ── No primeiro scan: se robô já estava desligado, avisa ──
    if first_scan and robot_status == "DESLIGADO":
        off_bets = [b for b in bets if b["status_key"] == "robo_desligado"]
        alert_text = build_alert_text(
            label,
            off_bets or [{"game": "—", "time": now, "status_text": "Robô Desligado", "status_key": "robo_desligado"}],
            "robo_desligado"
        )
        new_alert_id = await send_or_edit(bot, chat_id, None, alert_text, alert=True)
        update_monitor_state(lid, alert_message_id=new_alert_id)

    # ── Ciclos normais: detecta problemas nas novas apostas ─
    if not first_scan:
        problems = [b for b in new_bets if b["status_key"] in ALERT_STATUSES]
        by_type: dict[str, list] = {}
        for b in problems:
            by_type.setdefault(b["status_key"], []).append(b)

        robot_just_turned_off = (prev_robot_status == "LIGADO" and robot_status == "DESLIGADO")
        robot_still_off = (prev_robot_status == "DESLIGADO" and robot_status == "DESLIGADO")
        robot_turned_on = (prev_robot_status == "DESLIGADO" and robot_status == "LIGADO")

        if robot_turned_on:
            await bot.send_message(
                chat_id=chat_id,
                text=f"✅ *ROBÔ LIGADO NOVAMENTE*\n📋 *{label}*\n\n🕐 {now}",
                parse_mode="Markdown"
            )
            update_monitor_state(lid, alert_message_id=None)
            prev_alert_msg_id = None

        if robot_just_turned_off or robot_still_off:
            off_bets = [b for b in bets if b["status_key"] == "robo_desligado"]
            alert_text = build_alert_text(
                label,
                off_bets or [{"game": "—", "time": now, "status_text": "Robô Desligado", "status_key": "robo_desligado"}],
                "robo_desligado"
            )
            new_alert_id = await send_or_edit(bot, chat_id, prev_alert_msg_id, alert_text, alert=robot_just_turned_off)
            update_monitor_state(lid, alert_message_id=new_alert_id)

        for alert_type, bet_list in by_type.items():
            if alert_type == "robo_desligado":
                continue
            alert_text = build_alert_text(label, bet_list, alert_type)
            await bot.send_message(
                chat_id=chat_id,
                text=alert_text,
                parse_mode="Markdown"
            )

        logger.info(f"[{label}] ✓ Robô: {robot_status} | Apostas: {len(bets)} | Novas: {len(new_bets)} | Problemas: {len(problems)}")
    else:
        logger.info(f"[{label}] ✓ Primeiro scan concluído. Robô: {robot_status} | Apostas: {len(bets)}")

    # ── Persiste estado ───────────────────────────────────
    update_monitor_state(
        lid,
        last_bet_id=all_bet_ids,
        robot_status=robot_status,
        summary_message_id=new_summary_id,
        last_check=now,
    )


async def first_scan_license(license_id: int):
    """Chamado pelo bot logo após cadastro da licença para fazer o scan inicial."""
    from database import get_all_active_licenses
    bot = Bot(token=BOT_TOKEN)
    licenses = get_all_active_licenses()
    lic = next((l for l in licenses if l["id"] == license_id), None)
    if not lic:
        logger.error(f"Licença {license_id} não encontrada para primeiro scan")
        return
    await process_license(bot, lic, first_scan=True)


async def monitor_loop():
    """Loop principal de monitoramento."""
    bot = Bot(token=BOT_TOKEN)
    logger.info(f"🐷 Fiscal de Serviço Porco iniciado. Intervalo: {CHECK_INTERVAL}s")

    while True:
        licenses = get_all_active_licenses()
        logger.info(f"Monitorando {len(licenses)} licença(s)...")

        tasks = [process_license(bot, lic) for lic in licenses]
        await asyncio.gather(*tasks, return_exceptions=True)

        await asyncio.sleep(CHECK_INTERVAL)
