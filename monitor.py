import asyncio
import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from telegram import Bot
from telegram.error import TelegramError
from database import (
    get_all_active_licenses, update_monitor_state,
    get_monitor_state, get_cookies
)
from scraper import scrape_license, ALERT_STATUSES, SILENT_STATUSES

logger = logging.getLogger(__name__)

BOT_TOKEN      = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CAPSOLVER_KEY  = os.environ.get("CAPSOLVER_KEY", "")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL_SECONDS", "60"))
TZ             = ZoneInfo("America/Sao_Paulo")

ERROR_COOLDOWN_CYCLES = 5

STATUS_EMOJI = {
    "feita":              "✅",
    "odd_derretida":      "💩",
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

INCIDENT_LABELS = {
    "robo_desligado":     "Robô Desligado",
    "saldo_insuficiente": "Saldo Insuficiente",
    "conta_limitada":     "Conta Limitada",
    "verificacao":        "Verificação Necessária",
    "outros":             "Problema Detectado",
}


def now_brt():
    return datetime.now(TZ).strftime("%d/%m - %H:%M")

def today_str():
    return datetime.now(TZ).strftime("%Y-%m-%d")

def current_hour_minute():
    now = datetime.now(TZ)
    return now.hour, now.minute


# ── Incidents helpers ─────────────────────────────────────────

def load_incidents(raw: str) -> list:
    """Carrega lista de incidentes do JSON salvo no banco."""
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []

def save_incidents(incidents: list) -> str:
    return json.dumps(incidents, ensure_ascii=False)

def build_incidents_section(incidents: list) -> str:
    """Monta a seção 'Resumo das Cagadas' para o resumo do dia."""
    lines = ["", "*Resumo das Cagadas:*"]
    if not incidents:
        lines.append("_Nenhuma ocorrência hoje._")
    else:
        for inc in incidents:
            time_str  = inc.get("time", "")
            label     = inc.get("label", "Problema")
            resolved  = inc.get("resolved", False)
            line = f"{time_str} - {label}"
            if resolved:
                # Markdown strikethrough via ~~ não é suportado no Telegram
                # Usamos o caractere Unicode de tachado manualmente
                line = "~" + line + "~"  # Telegram MarkdownV2 strikethrough
            lines.append(line)
    return "\n".join(lines)


# ── Message builders ──────────────────────────────────────────

def build_summary_text(label, bets, robot_status, last_check, incidents):
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

    lines.append(build_incidents_section(incidents))
    return "\n".join(lines)


def build_alert_text(label, bet, alert_type):
    title = ALERT_TITLES.get(alert_type, "⚠️ ALERTA")
    emoji = STATUS_EMOJI.get(alert_type, "⚠️")
    lines = [
        f"🚨 *{title}*",
        f"📋 *{label}*",
        f"",
        f"{emoji} {bet['game']} — {bet['time']}",
        f"   _{bet['status_text']}_",
        f"",
        f"🕐 {now_brt()}",
    ]
    return "\n".join(lines)


# ── Telegram helpers ──────────────────────────────────────────

async def send_or_edit(bot, chat_id, message_id, text, alert=False):
    if message_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=int(message_id),
                text=text, parse_mode="Markdown"
            )
            return message_id
        except TelegramError as e:
            err_str = str(e).lower()
            if "message to edit not found" in err_str or "message can't be edited" in err_str:
                logger.warning(f"Mensagem {message_id} não encontrada, criando nova")
            else:
                logger.warning(f"Erro ao editar {message_id}: {e}")
                return message_id
        except Exception as e:
            logger.warning(f"Erro inesperado ao editar {message_id}: {e}")
            return message_id

    try:
        msg = await bot.send_message(
            chat_id=chat_id, text=text,
            parse_mode="Markdown",
            disable_notification=(not alert)
        )
        return str(msg.message_id)
    except Exception as e:
        logger.error(f"Erro ao enviar mensagem: {e}")
        return None


async def try_delete(bot, chat_id, message_id):
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=int(message_id))
    except Exception:
        pass


# ── Shift management ──────────────────────────────────────────

async def start_shift(bot, lic, state, force=False):
    """
    Inicia o turno do dia:
    - Manda mensagem de bom dia
    - Manda resumo inicial (sem apostas ainda)
    - Marca goodmorning_sent=1
    force=True: usado no deploy, ignora horário
    """
    lid     = lic["id"]
    label   = lic["label"]
    chat_id = lic["user_telegram_id"]
    now     = now_brt()
    today   = today_str()

    # Mensagem de bom dia
    await bot.send_message(
        chat_id=chat_id,
        text=(
            "🐷 *Fiscal de Serviço Porco*\n\n"
            "Oi, bom dia! Eu sou seu Fiscal do Serviço Porco do Robotenis. "
            "Vou fiscalizar o serviço merda desse robô hoje para você! 🕵️"
        ),
        parse_mode="Markdown",
        disable_notification=True,
    )

    # Resumo inicial vazio
    summary_text = build_summary_text(label, [], "UNKNOWN", now, [])
    msg = await bot.send_message(
        chat_id=chat_id,
        text=summary_text,
        parse_mode="Markdown",
        disable_notification=True,
    )

    update_monitor_state(
        lid,
        summary_message_id=str(msg.message_id),
        summary_date=today,
        goodmorning_sent=1,
        goodnight_sent=0,
        daily_incidents=save_incidents([]),
        alert_message_id=None,
    )
    logger.info(f"[{label}] Turno iniciado (force={force})")


async def end_shift(bot, lic, state):
    """Envia mensagem de boa noite às 23:59."""
    lid     = lic["id"]
    label   = lic["label"]
    chat_id = lic["user_telegram_id"]

    await bot.send_message(
        chat_id=chat_id,
        text=(
            "🐷 *Fiscal de Serviço Porco*\n\n"
            "Encerrando meu turno do dia, muito cansado de fiscalizar serviço ruim. "
            "Boa noite! Meu colega já vai começar o turno dele. 😴"
        ),
        parse_mode="Markdown",
        disable_notification=True,
    )

    update_monitor_state(lid, goodnight_sent=1)
    logger.info(f"[{label}] Turno encerrado")


# ── Core process ──────────────────────────────────────────────

async def process_license(bot: Bot, lic: dict, first_scan: bool = False):
    lid      = lic["id"]
    label    = lic["label"]
    email    = lic["email"]
    password = lic["password"]
    chat_id  = lic["user_telegram_id"]

    state             = get_monitor_state(lid)
    prev_robot_status = state.get("robot_status", "UNKNOWN")
    prev_summary_id   = state.get("summary_message_id")
    prev_alert_id     = state.get("alert_message_id")
    seen_ids          = set((state.get("last_bet_id") or "").split("||")) - {""}
    license_url       = state.get("license_url", "")
    summary_date      = state.get("summary_date", "")
    error_cooldown    = int(state.get("error_cooldown", 0) or 0)
    goodmorning_sent  = int(state.get("goodmorning_sent", 0) or 0)
    goodnight_sent    = int(state.get("goodnight_sent", 0) or 0)
    incidents         = load_incidents(state.get("daily_incidents", "") or "")

    now   = now_brt()
    today = today_str()
    hour, minute = current_hour_minute()

    # ── Cooldown de erros ──────────────────────────────────
    if error_cooldown > 0 and not first_scan:
        logger.info(f"[{label}] Cooldown ({error_cooldown} ciclos restantes), pulando")
        update_monitor_state(lid, error_cooldown=error_cooldown - 1)
        return

    # ── Virada de dia: reseta flags para novo turno ────────
    if summary_date != today and not first_scan:
        logger.info(f"[{label}] Novo dia detectado, resetando turno")
        await try_delete(bot, chat_id, prev_summary_id)
        prev_summary_id = None
        incidents = []
        update_monitor_state(
            lid,
            summary_message_id=None,
            summary_date=today,
            goodmorning_sent=0,
            goodnight_sent=0,
            daily_incidents=save_incidents([]),
        )
        goodmorning_sent = 0
        goodnight_sent   = 0

    # ── 23:59 — Boa noite ──────────────────────────────────
    if hour == 23 and minute >= 59 and not goodnight_sent and not first_scan:
        await end_shift(bot, lic, state)
        return

    # ── 00:01 — Bom dia ────────────────────────────────────
    if hour == 0 and minute >= 1 and not goodmorning_sent and not first_scan:
        await start_shift(bot, lic, state)
        # Recarrega estado após start_shift
        state           = get_monitor_state(lid)
        prev_summary_id = state.get("summary_message_id")
        goodmorning_sent = 1
        incidents = []

    # ── Se ainda não mandou bom dia hoje e não é first_scan,
    #    só prossegue — o resumo vai ser criado/editado normalmente ──

    # ── Scrape ────────────────────────────────────────────
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

    result = await scrape_license(email, password, cf, r365, license_url, CAPSOLVER_KEY)

    if connecting_msg:
        await try_delete(bot, chat_id, connecting_msg.message_id)

    if not result["success"]:
        err = result["error"]
        logger.error(f"[{label}] Falhou: {err}")

        if "Timeout" in err or "timeout" in err or "TimeoutError" in err:
            logger.warning(f"[{label}] Timeout, tentando no próximo ciclo")
            return

        if "login" in err.lower() or "email/senha" in err.lower() or "expirada" in err.lower() or "Cookies não" in err:
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
        update_monitor_state(lid, error_cooldown=ERROR_COOLDOWN_CYCLES)
        return

    if error_cooldown > 0:
        update_monitor_state(lid, error_cooldown=0)

    robot_status    = result["robot_status"]
    bets            = result["bets"]
    new_license_url = result.get("license_url", license_url)
    all_ids         = "||".join(b["id"] for b in bets)

    new_bets = [] if first_scan else [b for b in bets if b["id"] not in seen_ids]

    # ── First scan ─────────────────────────────────────────
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

    # ── Resumo (edita sempre) ──────────────────────────────
    # Recarrega incidents do banco (pode ter sido atualizado)
    fresh_state = get_monitor_state(lid)
    incidents   = load_incidents(fresh_state.get("daily_incidents", "") or "")
    prev_summary_id = fresh_state.get("summary_message_id") or prev_summary_id

    summary_text   = build_summary_text(label, bets, robot_status, now, incidents)
    new_summary_id = await send_or_edit(
        bot, chat_id,
        None if first_scan else prev_summary_id,
        summary_text, alert=False
    )

    # ── Alertas ────────────────────────────────────────────
    if not first_scan and bets:
        last_bet     = bets[0]
        last_status  = last_bet["status_key"]
        prev_last_id = (state.get("last_bet_id") or "").split("||")[0] if state.get("last_bet_id") else ""
        last_changed = last_bet["id"] != prev_last_id

        if last_changed:
            if last_status in ALERT_STATUSES:
                # Nova cagada — registra no histórico
                inc_label = INCIDENT_LABELS.get(last_status, "Problema")
                incident  = {
                    "time":     datetime.now(TZ).strftime("%H:%M"),
                    "label":    inc_label,
                    "status":   last_status,
                    "resolved": False,
                }
                incidents.append(incident)

                # Manda alerta (substitui o anterior se existir)
                alert_text   = build_alert_text(label, last_bet, last_status)
                new_alert_id = await send_or_edit(
                    bot, chat_id, prev_alert_id, alert_text, alert=True
                )
                update_monitor_state(
                    lid,
                    alert_message_id=new_alert_id,
                    daily_incidents=save_incidents(incidents),
                )
                prev_alert_id = new_alert_id

                # Atualiza resumo com nova cagada
                summary_text   = build_summary_text(label, bets, robot_status, now, incidents)
                new_summary_id = await send_or_edit(bot, chat_id, new_summary_id, summary_text, alert=False)

            elif last_status in SILENT_STATUSES:
                if prev_alert_id:
                    # Apaga o alerta
                    await try_delete(bot, chat_id, prev_alert_id)
                    update_monitor_state(lid, alert_message_id=None)
                    prev_alert_id = None

                    # Marca última cagada aberta como resolvida
                    for inc in reversed(incidents):
                        if not inc.get("resolved"):
                            inc["resolved"] = True
                            break

                    # Atualiza resumo com cagada tachada
                    summary_text   = build_summary_text(label, bets, robot_status, now, incidents)
                    new_summary_id = await send_or_edit(bot, chat_id, new_summary_id, summary_text, alert=False)

                    update_monitor_state(lid, daily_incidents=save_incidents(incidents))

    # Salva cookies se renovados
    new_cf   = result.get("new_cf_clearance", "")
    new_r365 = result.get("new_r365_cookie", "")
    if new_cf and new_r365 and (new_cf != cf or new_r365 != r365):
        from database import save_cookies
        save_cookies(lid, new_cf, new_r365)
        logger.info(f"[{label}] Cookies renovados automaticamente")

    # ── Persiste estado ────────────────────────────────────
    update_monitor_state(
        lid,
        last_bet_id=all_ids,
        robot_status=robot_status,
        summary_message_id=new_summary_id,
        summary_date=today,
        last_check=now,
        license_url=new_license_url,
        daily_incidents=save_incidents(incidents),
    )

    logger.info(f"[{label}] ✓ Robô: {robot_status} | Apostas: {len(bets)} | Novas: {len(new_bets)}")


# ── Public API ────────────────────────────────────────────────

async def first_scan_license(license_id: int):
    bot      = Bot(token=BOT_TOKEN)
    licenses = get_all_active_licenses()
    lic      = next((l for l in licenses if l["id"] == license_id), None)
    if lic:
        await process_license(bot, lic, first_scan=True)


async def deploy_reset(bot: Bot):
    """
    Chamado pelo /deploy_reset (admin).
    Para cada licença ativa:
    1. Apaga todas as mensagens de hoje do bot (summary + alert)
    2. Reseta estado do turno
    3. Inicia novo turno imediatamente
    """
    licenses = get_all_active_licenses()
    for lic in licenses:
        lid     = lic["id"]
        chat_id = lic["user_telegram_id"]
        state   = get_monitor_state(lid)

        # Apaga mensagens existentes
        await try_delete(bot, chat_id, state.get("summary_message_id"))
        await try_delete(bot, chat_id, state.get("alert_message_id"))

        # Reseta estado
        update_monitor_state(
            lid,
            summary_message_id=None,
            alert_message_id=None,
            summary_date="",
            goodmorning_sent=0,
            goodnight_sent=0,
            daily_incidents=save_incidents([]),
            error_cooldown=0,
        )

        # Avisa
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "🔄 *Atualização!*\n\n"
                "Oi, fui atualizado! Vou apagar todas as mensagens de hoje do chat "
                "e começar de novo o meu turno. 🐷"
            ),
            parse_mode="Markdown",
        )

        # Inicia turno
        fresh_lic = next((l for l in get_all_active_licenses() if l["id"] == lid), lic)
        await start_shift(bot, fresh_lic, {}, force=True)
        logger.info(f"[{lic['label']}] Deploy reset concluído")


async def _safe_process(bot, lic):
    try:
        await asyncio.wait_for(process_license(bot, lic), timeout=180)
    except asyncio.TimeoutError:
        logger.error(f"[{lic['label']}] Timeout de 180s — pulando ciclo")
    except Exception as e:
        logger.error(f"[{lic['label']}] Erro inesperado: {e}")


async def monitor_loop():
    bot = Bot(token=BOT_TOKEN)
    logger.info(f"🐷 Fiscal iniciado. Intervalo: {CHECK_INTERVAL}s")
    cycle = 0
    while True:
        cycle += 1
        licenses = get_all_active_licenses()
        logger.info(f"Ciclo #{cycle} — Monitorando {len(licenses)} licença(s)...")
        await asyncio.gather(*[_safe_process(bot, lic) for lic in licenses])
        logger.info(f"Ciclo #{cycle} concluído. Aguardando {CHECK_INTERVAL}s...")
        await asyncio.sleep(CHECK_INTERVAL)
