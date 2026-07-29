from functools import wraps
from telegram import Update
import sheets

def require_access(admin_only=False):
    def decorator(func):
        @wraps(func)
        async def wrapper(update: Update, context, *args, **kwargs):
            user_id = update.effective_user.id
            if admin_only and not sheets.is_admin(user_id):
                await update.message.reply_text("⛔ Доступно только администратору.")
                return
            if not sheets.is_allowed(user_id):
                await update.message.reply_text(
                    f"⛔ у тебя нет прав! ты в Росси живешь Иди покури .\nВаш ID: {user_id}\n"
                    "Отправьте его администратору, чтобы он вас добавил."
                )
                return
            return await func(update, context, *args, **kwargs)
        return wrapper
    return decorator
    