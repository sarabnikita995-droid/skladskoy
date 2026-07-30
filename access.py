from functools import wraps

from telegram import Update

import sheets

ADMIN_USERNAME_URL = "https://t.me/k0va1sky"
NO_ACCESS_TEXT = (
    "⛔ У вас нет доступа к этому боту.\n"
    "Ваш ID: {user_id}\n\n"
    f"Напишите администратору, чтобы вас добавили: {ADMIN_USERNAME_URL}"
)
ADMIN_ONLY_TEXT = (
    "⛔ Эта функция доступна только администратору.\n\n"
    f"По вопросам — {ADMIN_USERNAME_URL}"
)


def require_access(admin_only=False):
    def decorator(func):
        @wraps(func)
        async def wrapper(update: Update, context, *args, **kwargs):
            user_id = update.effective_user.id

            if not sheets.is_allowed(user_id):
                await update.message.reply_text(NO_ACCESS_TEXT.format(user_id=user_id))
                return

            if admin_only and not sheets.is_admin(user_id):
                await update.message.reply_text(ADMIN_ONLY_TEXT)
                return

            return await func(update, context, *args, **kwargs)
        return wrapper
    return decorator
