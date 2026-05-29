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
    init_db, user_exists, create_user, use_invite, invite_exists,
    add_license, get_licenses, remove_license, create_invite
)

logger = logging.getLogger(__name__)

ADMIN_ID = os.environ.get("ADMIN_TELEGRAM_ID", "")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# ── Estados da conversa ──────────────────────────────────────
AWAIT_INVITE = 1
AWAIT_EMAIL = 2
AWAIT_PASSWORD = 3
AWAIT_LABEL = 4
AWAIT_REMOVE_ID = 5

# ── /start ───────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = str(update.effective_user.id)
    name = update.effective_user.first_name or "usuário"

    if user_exists(tid):
        licenses = get_licenses(tid)
        count = len(licenses)
        await update.message.reply_text(
            f"🐷 *Fiscal de Serviço Porco*\n\n"
            f"Oi {name}! Você já está cadastrado.\n"
            f"📋 Licenças monitoradas: *{count}*\n\n"
            f"Comandos:\n"
            f"/add\\_licenca — Adicionar nova licença\n"
            f"/minhas\\_licencas — Ver suas licenças\n"
            f"/remover — Remover uma licença\n"
            f"/status — Ver status atual\n",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "🐷 *Fiscal de Serviço Porco*\n\n"
        "Bem-vindo! Para começar, envie seu *código de convite*:",
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
        "✅ *Cadastro realizado!*\n\n"
        "Agora vamos adicionar sua primeira licença do Robôs365.\n\n"
        "📧 Digite seu *e-mail* da conta:",
        parse_mode="Markdown"
    )
    return AWAIT_EMAIL


# ── /add_licenca ─────────────────────────────────────────────
async def add_licenca_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = str(update.effective_user.id)
    if not user_exists(tid):
        await update.message.reply_text("❌ Você não está cadastrado. Use /start")
        return ConversationHandler.END

    await update.message.reply_text(
        "➕ *Nova licença*\n\n📧 Digite seu e-mail da conta Robôs365:",
        parse_mode="Markdown"
    )
    return AWAIT_EMAIL


async def receive_email(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["email"] = update.message.text.strip()
    await update.message.reply_text("🔑 Agora digite sua *senha*:", parse_mode="Markdown")
    return AWAIT_PASSWORD


async def receive_password(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["password"] = update.message.text.strip()
    await update.message.reply_text(
        "🏷️ Dê um *nome* para essa licença (ex: Conta Principal, Conta 2).\n"
        "Ou envie /pular para usar o e-mail como nome:",
        parse_mode="Markdown"
    )
    return AWAIT_LABEL


async def _trigger_first_scan(lid: int):
    """Dispara o primeiro scan em background sem bloquear o bot."""
    from monitor import first_scan_license
    await first_scan_license(lid)


async def receive_label(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = str(update.effective_user.id)
    label = update.message.text.strip()
    if label.startswith("/pular") or label == "-":
        label = ctx.user_data.get("email", "")

    email = ctx.user_data.get("email")
    password = ctx.user_data.get("password")

    lid = add_license(tid, email, password, label)

    await update.message.reply_text(
        f"🏷️ *{label}* cadastrada!\n\n"
        f"🔍 Fazendo o primeiro scan agora...",
        parse_mode="Markdown"
    )
    ctx.user_data.clear()

    # Dispara primeiro scan sem bloquear
    asyncio.create_task(_trigger_first_scan(lid))

    return ConversationHandler.END


async def skip_label(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = str(update.effective_user.id)
    email = ctx.user_data.get("email")
    password = ctx.user_data.get("password")
    lid = add_license(tid, email, password, email)

    await update.message.reply_text(
        f"📧 *{email}* cadastrada!\n\n"
        f"🔍 Fazendo o primeiro scan agora...",
        parse_mode="Markdown"
    )
    ctx.user_data.clear()

    asyncio.create_task(_trigger_first_scan(lid))

    return ConversationHandler.END


# ── /minhas_licencas ─────────────────────────────────────────
async def minhas_licencas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = str(update.effective_user.id)
    if not user_exists(tid):
        await update.message.reply_text("❌ Use /start para se cadastrar.")
        return

    licenses = get_licenses(tid)
    if not licenses:
        await update.message.reply_text("Você não tem licenças cadastradas. Use /add\\_licenca", parse_mode="Markdown")
        return

    msg = "📋 *Suas licenças:*\n\n"
    for lic in licenses:
        msg += f"🔹 *#{lic['id']}* — {lic['label']}\n   📧 {lic['email']}\n\n"

    await update.message.reply_text(msg, parse_mode="Markdown")


# ── /remover ─────────────────────────────────────────────────
async def remover_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = str(update.effective_user.id)
    if not user_exists(tid):
        await update.message.reply_text("❌ Use /start para se cadastrar.")
        return ConversationHandler.END

    licenses = get_licenses(tid)
    if not licenses:
        await update.message.reply_text("Você não tem licenças para remover.")
        return ConversationHandler.END

    buttons = [
        [InlineKeyboardButton(f"#{lic['id']} — {lic['label']}", callback_data=f"remove_{lic['id']}")]
        for lic in licenses
    ]
    buttons.append([InlineKeyboardButton("❌ Cancelar", callback_data="remove_cancel")])
    await update.message.reply_text(
        "Qual licença deseja remover?",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
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


# ── /status ──────────────────────────────────────────────────
async def status_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = str(update.effective_user.id)
    if not user_exists(tid):
        await update.message.reply_text("❌ Use /start para se cadastrar.")
        return

    from database import get_monitor_state
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
        msg += f"{icon} *{lic['label']}*\n   Robô: {robot} | Última checagem: {last}\n\n"

    await update.message.reply_text(msg, parse_mode="Markdown")


# ── /cancelar ────────────────────────────────────────────────
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
    msg = f"🎟️ *{n} código(s) gerado(s):*\n\n"
    for c in codes:
        msg += f"`{c}`\n"
    await update.message.reply_text(msg, parse_mode="Markdown")


# ── Fallback ─────────────────────────────────────────────────
async def unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Não entendi. Use /start para começar ou /add\\_licenca para adicionar uma licença.",
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
        ],
        states={
            AWAIT_INVITE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_invite)],
            AWAIT_EMAIL:     [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_email)],
            AWAIT_PASSWORD:  [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_password)],
            AWAIT_LABEL:     [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_label),
                CommandHandler("pular", skip_label),
            ],
            AWAIT_REMOVE_ID: [CallbackQueryHandler(remove_callback, pattern="^remove_")],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        per_chat=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("minhas_licencas", minhas_licencas))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("gerar_convite", gerar_convite))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown))

    return app
