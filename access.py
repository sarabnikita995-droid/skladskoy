from functools import wraps

from telegram import Update

import sheets

ADMIN_USERNAME_URL = "https://t.me/k0va1sky"
NO_ACCESS_TEXT = (
    "⛔ У тебя нет прав на этого бота, потому что ты живешь в России.\n"
    "Ваш ID: {user_id}\n\n"
    f"Напишите администратору, чтобы вас добавили: {ADMIN_USERNAME_URL}"
)
ADMIN_ONLY_TEXT = (
    "⛔ Эта функция доступна только администратору.\n\n"
    f"По вопросам — {ADMIN_USERNAME_URL}"
)


async def notify_admins_new_user(context, user):
    """
    Шлёт всем админам уведомление о том, что пользователь без доступа
    пытается пользоваться ботом — вместе с его аккаунтом в Telegram.
    Чтобы не спамить админов, уведомляет по каждому такому пользователю
    только один раз за время работы процесса бота.
    """
    notified = context.bot_data.setdefault("notified_noaccess_users", set())
    if user.id in notified:
        return
    notified.add(user.id)

    username = f"@{user.username}" if user.username else "(без юзернейма)"
    text = (
        "👋 Пользователь без доступа хочет пользоваться ботом:\n"
        f"{user.first_name or ''} {username}\n"
        f"ID: {user.id}"
    )

    for row in sheets.list_users():
        if sheets.row_role(row) != "admin":
            continue
        admin_id = sheets.row_user_id(row)
        if not admin_id:
            continue
        try:
            await context.bot.send_message(admin_id, text)
        except Exception:
            pass


def require_access(admin_only=False):
    def decorator(func):
        @wraps(func)
        async def wrapper(update: Update, context, *args, **kwargs):
            user_id = update.effective_user.id

            if not sheets.is_allowed(user_id):
                await update.message.reply_text(NO_ACCESS_TEXT.format(user_id=user_id))
                await notify_admins_new_user(context, update.effective_user)
                return

            if admin_only and not sheets.is_admin(user_id):
                await update.message.reply_text(ADMIN_ONLY_TEXT)
                return

            return await func(update, context, *args, **kwargs)
        return wrapper
    return decorator
