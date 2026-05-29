import asyncio
import logging
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ConversationHandler, CallbackQueryHandler,
    filters, ContextTypes
)
from database import (
    init_db, user_exists, create_user, use_invite,
    add_license, get_licenses, remove_license, create_invite,
    save_cookies, get_monitor_state, update_monitor_state as update_monitor_state_fn
)

logger = logging.getLogger(__name__)

ADMIN_ID  = os.environ.get("ADMIN_TELEGRAM_ID", "")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# Estados
AWAIT_INVITE      = 1
AWAIT_EMAIL       = 2
AWAIT_PASSWORD    = 3
AWAIT_LABEL       = 4
AWAIT_REMOVE_ID   = 5
AWAIT_COOKIE_LIC  = 6
AWAIT_CF_COOKIE   = 7
AWAIT_R365_COOKIE = 8


async def _trigger_first_scan(lid: int):
    from monitor import first_scan_license
    await first_scan_license(lid)


# ── /start ────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = str(update.effective_user.id)
    name = update.effective_user.first_name or "usuário"

    if user_exists(tid):
        licenses = get_licenses(tid)
        await update.message.reply_text(
            f"🐷 *Fiscal de Serviço Porco*\n\n"
            f"Oi {name}! Você já está cadastrado.\n"
            f"📋 Licenças monitoradas: *{len(licenses)}*\n\n"
            f"/add\\_licenca — Adicionar licença\n"
            f"/minhas\\_licencas — Ver licenças\n"
            f"/atualizar\\_cookies — Renovar cookies\n"
            f"/remover — Remover licença\n"
            f"/ok — Problema resolvido\n"
            f"/status — Ver status atual",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "🐷 *Fiscal de Serviço Porco*\n\nBem-vindo! Envie seu *código de convite*:",
        parse_mode="Markdown"
    )
    return AWAIT_INVITE


async def receive_invite(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = str(update.effective_user.id)
    code = update.message.text.strip()
    if not use_invite(code, tid):
        await update.message.reply_text("❌ Código inválido ou já usado. Tente novamente:")
        return AWAIT_INVITE
    create_user(tid, update.effective_user.username)
    await update.message.reply_text(
        "✅ *Cadastro realizado!*\n\nAgora vamos adicionar sua primeira licença.\n\n📧 Digite seu *e-mail* da conta Robôs365:",
        parse_mode="Markdown"
    )
    return AWAIT_EMAIL


# ── /add_licenca ──────────────────────────────────────────────
async def add_licenca_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = str(update.effective_user.id)
    if not user_exists(tid):
        await update.message.reply_text("❌ Use /start para se cadastrar.")
        return ConversationHandler.END
    await update.message.reply_text("➕ *Nova licença*\n\n📧 Digite seu e-mail da conta Robôs365:", parse_mode="Markdown")
    return AWAIT_EMAIL


async def receive_email(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["email"] = update.message.text.strip()
    await update.message.reply_text("🔑 Agora digite sua *senha*:", parse_mode="Markdown")
    return AWAIT_PASSWORD


async def receive_password(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["password"] = update.message.text.strip()
    await update.message.reply_text(
        "🏷️ Dê um *nome* para essa licença (ex: Conta Principal).\nOu envie /pular para usar o e-mail:",
        parse_mode="Markdown"
    )
    return AWAIT_LABEL


async def receive_label(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = str(update.effective_user.id)
    label = update.message.text.strip()
    if label.startswith("/pular"):
        label = ctx.user_data.get("email", "")
    lid = add_license(tid, ctx.user_data["email"], ctx.user_data["password"], label)
    ctx.user_data.clear()
    await update.message.reply_text(
        f"🏷️ *{label}* cadastrada!\n\n"
        f"⚠️ Agora preciso dos *cookies* da sua sessão para acessar o site.\n\n"
        f"Use /atualizar\\_cookies para configurar.",
        parse_mode="Markdown"
    )
    asyncio.create_task(_trigger_first_scan(lid))
    return ConversationHandler.END


async def skip_label(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = str(update.effective_user.id)
    email = ctx.user_data.get("email")
    lid = add_license(tid, email, ctx.user_data.get("password"), email)
    ctx.user_data.clear()
    await update.message.reply_text(
        f"📧 *{email}* cadastrada!\n\n"
        f"⚠️ Use /atualizar\\_cookies para configurar os cookies de acesso.",
        parse_mode="Markdown"
    )
    asyncio.create_task(_trigger_first_scan(lid))
    return ConversationHandler.END


# ── /atualizar_cookies ────────────────────────────────────────
async def atualizar_cookies_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = str(update.effective_user.id)
    if not user_exists(tid):
        await update.message.reply_text("❌ Use /start para se cadastrar.")
        return ConversationHandler.END

    licenses = get_licenses(tid)
    if not licenses:
        await update.message.reply_text("Você não tem licenças. Use /add\\_licenca", parse_mode="Markdown")
        return ConversationHandler.END

    if len(licenses) == 1:
        ctx.user_data["cookie_license_id"] = licenses[0]["id"]
        ctx.user_data["cookie_label"] = licenses[0]["label"]
        await update.message.reply_text(
            f"🍪 *Atualizar cookies — {licenses[0]['label']}*\n\n"
            f"*Como pegar os cookies:*\n"
            f"1. Acesse robos365.com.br e faça login\n"
            f"2. Aperte F12 → aba *Application*\n"
            f"3. Cookies → https://www.robos365.com.br\n"
            f"4. Copie o valor de *cf\\_clearance*\n\n"
            f"Envie o valor do *cf\\_clearance* agora:",
            parse_mode="Markdown"
        )
        return AWAIT_CF_COOKIE

    buttons = [
        [InlineKeyboardButton(f"#{lic['id']} — {lic['label']}", callback_data=f"cklic_{lic['id']}")]
        for lic in licenses
    ]
    buttons.append([InlineKeyboardButton("❌ Cancelar", callback_data="cklic_cancel")])
    await update.message.reply_text(
        "🍪 Para qual licença deseja atualizar os cookies?",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return AWAIT_COOKIE_LIC


async def cookie_lic_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cklic_cancel":
        await query.edit_message_text("Cancelado.")
        return ConversationHandler.END
    lid = int(query.data.replace("cklic_", ""))
    licenses = get_licenses(str(query.from_user.id))
    lic = next((l for l in licenses if l["id"] == lid), None)
    ctx.user_data["cookie_license_id"] = lid
    ctx.user_data["cookie_label"] = lic["label"] if lic else str(lid)
    await query.edit_message_text(
        f"🍪 *Atualizar cookies — {ctx.user_data['cookie_label']}*\n\n"
        f"*Como pegar:*\n"
        f"1. Acesse robos365.com.br e faça login\n"
        f"2. F12 → *Application* → Cookies → https://www.robos365.com.br\n"
        f"3. Copie o valor de *cf\\_clearance*\n\n"
        f"Envie o valor do *cf\\_clearance* agora:",
        parse_mode="Markdown"
    )
    return AWAIT_CF_COOKIE


async def receive_cf_cookie(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["cf_clearance"] = update.message.text.strip()
    await update.message.reply_text(
        "✅ cf\\_clearance salvo!\n\n"
        "Agora envie o valor do cookie *R365*\n"
        "_(mesma tela do DevTools, linha R365)_",
        parse_mode="Markdown"
    )
    return AWAIT_R365_COOKIE


async def receive_r365_cookie(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = str(update.effective_user.id)
    lid = ctx.user_data.get("cookie_license_id")
    label = ctx.user_data.get("cookie_label", "")
    cf = ctx.user_data.get("cf_clearance", "")
    r365 = update.message.text.strip()

    save_cookies(lid, cf, r365)
    ctx.user_data.clear()

    await update.message.reply_text(
        f"✅ *Cookies atualizados!*\n📋 *{label}*\n\n"
        f"🔍 Fazendo scan agora para confirmar...",
        parse_mode="Markdown"
    )

    asyncio.create_task(_trigger_first_scan(lid))

    return ConversationHandler.END


# ── /minhas_licencas ──────────────────────────────────────────
async def minhas_licencas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = str(update.effective_user.id)
    if not user_exists(tid):
        await update.message.reply_text("❌ Use /start para se cadastrar.")
        return
    licenses = get_licenses(tid)
    if not licenses:
        await update.message.reply_text("Nenhuma licença. Use /add\\_licenca", parse_mode="Markdown")
        return
    msg = "📋 *Suas licenças:*\n\n"
    for lic in licenses:
        tem_cookie = "🍪" if lic.get("cf_clearance") else "⚠️ sem cookie"
        msg += f"🔹 *#{lic['id']}* — {lic['label']} {tem_cookie}\n   📧 {lic['email']}\n\n"
    await update.message.reply_text(msg, parse_mode="Markdown")


# ── /remover ──────────────────────────────────────────────────
async def remover_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = str(update.effective_user.id)
    if not user_exists(tid):
        await update.message.reply_text("❌ Use /start para se cadastrar.")
        return ConversationHandler.END
    licenses = get_licenses(tid)
    if not licenses:
        await update.message.reply_text("Nenhuma licença para remover.")
        return ConversationHandler.END
    buttons = [
        [InlineKeyboardButton(f"#{lic['id']} — {lic['label']}", callback_data=f"remove_{lic['id']}")]
        for lic in licenses
    ]
    buttons.append([InlineKeyboardButton("❌ Cancelar", callback_data="remove_cancel")])
    await update.message.reply_text("Qual licença deseja remover?", reply_markup=InlineKeyboardMarkup(buttons))
    return AWAIT_REMOVE_ID


async def remove_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tid = str(query.from_user.id)
    if query.data == "remove_cancel":
        await query.edit_message_text("Cancelado.")
        return ConversationHandler.END
    lid = int(query.data.replace("remove_", ""))
    remove_license(lid, tid)
    await query.edit_message_text(f"✅ Licença #{lid} removida.")
    return ConversationHandler.END


# ── /status ───────────────────────────────────────────────────
async def status_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = str(update.effective_user.id)
    if not user_exists(tid):
        await update.message.reply_text("❌ Use /start para se cadastrar.")
        return
    licenses = get_licenses(tid)
    if not licenses:
        await update.message.reply_text("Nenhuma licença cadastrada.")
        return
    msg = "📊 *Status das licenças:*\n\n"
    for lic in licenses:
        state = get_monitor_state(lic["id"])
        robot = state.get("robot_status", "?")
        last = state.get("last_check", "nunca")
        icon = "🟢" if robot == "LIGADO" else "🔴" if robot == "DESLIGADO" else "⚪"
        cookie_ok = "🍪" if lic.get("cf_clearance") else "⚠️ cookie ausente"
        msg += f"{icon} *{lic['label']}* {cookie_ok}\n   Robô: {robot} | Checagem: {last}\n\n"
    await update.message.reply_text(msg, parse_mode="Markdown")


# ── /cancelar ─────────────────────────────────────────────────
async def cancelar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("Operação cancelada.")
    return ConversationHandler.END


# ── Admin: /gerar_convite ─────────────────────────────────────
async def gerar_convite(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = str(update.effective_user.id)
    if tid != str(ADMIN_ID):
        await update.message.reply_text("❌ Sem permissão.")
        return
    n = 1
    if ctx.args:
        try:
            n = max(1, min(int(ctx.args[0]), 20))
        except ValueError:
            pass
    codes = create_invite(n)
    msg = f"🎟️ *{n} código(s) gerado(s):*\n\n" + "\n".join(f"`{c}`" for c in codes)
    await update.message.reply_text(msg, parse_mode="Markdown")



async def ok_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = str(update.effective_user.id)
    if not user_exists(tid):
        await update.message.reply_text("Nao cadastrado.")
        return
    from telegram import Bot as TGBot
    from database import get_monitor_state
    import os, datetime
    from zoneinfo import ZoneInfo
    tgbot = TGBot(token=os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    licenses = get_licenses(tid)
    now = datetime.datetime.now(ZoneInfo("America/Sao_Paulo")).strftime("%d/%m - %H:%M")
    for lic in licenses:
        state = get_monitor_state(lic["id"])
        alert_msg_id = state.get("alert_message_id")
        summary_msg_id = state.get("summary_message_id")
        if alert_msg_id:
            try:
                await tgbot.delete_message(chat_id=tid, message_id=int(alert_msg_id))
            except Exception:
                pass
        if summary_msg_id:
            try:
                robot = state.get("robot_status", "?")
                r_icon = "\U0001f7e2" if robot == "LIGADO" else "\U0001f534" if robot == "DESLIGADO" else "\u26aa"
                last = state.get("last_check", now)
                label = lic["label"]
                txt = ("\U0001f437 *Fiscal de Servi\u00e7o Porco*\n"
                       "\U0001f4cb *" + label + "*\n"
                       "Rob\u00f4: " + r_icon + " " + robot + "\n"
                       "\U0001f550 Atualizado: " + last + "\n\n"
                       "\u2705 Alerta confirmado \u2014 " + now)
                await tgbot.edit_message_text(
                    chat_id=tid,
                    message_id=int(summary_msg_id),
                    text=txt,
                    parse_mode="Markdown"
                )
            except Exception:
                pass
        update_monitor_state_fn(lic["id"], alert_message_id=None)
    await update.message.reply_text("OK! Alertas resetados. Monitorando normalmente. \U0001f437")

async def unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Não entendi. Use /start para ver os comandos disponíveis.",
        parse_mode="Markdown"
    )


def build_app():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("add_licenca", add_licenca_cmd),
            CommandHandler("remover", remover_cmd),
            CommandHandler("atualizar_cookies", atualizar_cookies_cmd),
        ],
        states={
            AWAIT_INVITE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_invite)],
            AWAIT_EMAIL:       [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_email)],
            AWAIT_PASSWORD:    [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_password)],
            AWAIT_LABEL:       [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_label),
                CommandHandler("pular", skip_label),
            ],
            AWAIT_REMOVE_ID:   [CallbackQueryHandler(remove_callback, pattern="^remove_")],
            AWAIT_COOKIE_LIC:  [CallbackQueryHandler(cookie_lic_callback, pattern="^cklic_")],
            AWAIT_CF_COOKIE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_cf_cookie)],
            AWAIT_R365_COOKIE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_r365_cookie)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        per_chat=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("minhas_licencas", minhas_licencas))
    app.add_handler(CommandHandler("ok", ok_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("gerar_convite", gerar_convite))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown))

    return app
