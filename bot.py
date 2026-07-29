import os

from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters,
)

import sheets
from access import require_access

load_dotenv()
BOT_TOKEN = os.environ["BOT_TOKEN"]

# ---------- Меню и базовые команды ----------

def build_menu(user_id: int) -> ReplyKeyboardMarkup:
    staff_buttons = [["✏️ Внести остаток"], ["❓ Помощь"]]
    admin_extra = [["📋 Все остатки", "🗑 Удалить товар"], ["👥 Управление доступом"]]
    buttons = staff_buttons[:1] + (admin_extra if sheets.is_admin(user_id) else []) + staff_buttons[1:]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

async def start(update, context):
    await update.message.reply_text("Меню:", reply_markup=build_menu(update.effective_user.id))

async def help_cmd(update, context):
    await update.message.reply_text(
        "✏️ Внести остаток — обновить количество товара.\n"
        "👥 Управление доступом — только для админа."
    )

async def cancel(update, context):
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END

# ---------- Диалог внесения остатка ----------

WAITING_STOCK_INPUT = 1
STOCK_CONFIRM = 2

@require_access()
async def start_stock_entry(update, context):
    await update.message.reply_text(
        "Введите: название_товара количество\nПример: Молоко 12"
    )
    return WAITING_STOCK_INPUT

@require_access()
async def save_stock(update, context):
    text = update.message.text.strip()
    try:
        *name_parts, qty_raw = text.rsplit(" ", 1)
        name = " ".join(name_parts).strip()
        qty = float(qty_raw.replace(",", "."))
        if not name:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Не понял формат. Пример: Молоко 12")
        return WAITING_STOCK_INPUT

    row, matched_name, exact = sheets.find_product_match(name)
    context.user_data["stock_qty"] = qty
    context.user_data["stock_name"] = name

    if row and exact:
        # точное совпадение — пишем сразу, без переспроса
        sheets.update_stock(row, matched_name, qty, update.effective_user.first_name)
        await update.message.reply_text(f"✅ «{matched_name}»: остаток обновлён на {qty}")
        return ConversationHandler.END

    if row and not exact:
        # похоже на опечатку — уточняем
        context.user_data["stock_row"] = row
        context.user_data["stock_matched_name"] = matched_name
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Да, это он", callback_data="stock_confirm_fuzzy"),
            InlineKeyboardButton("❌ Нет, другой товар", callback_data="stock_confirm_no"),
        ]])
        await update.message.reply_text(
            f"Не нашёл точное совпадение. Вы имели в виду «{matched_name}»?", reply_markup=kb
        )
        return STOCK_CONFIRM

    # совсем не нашли — предлагаем добавить как новый товар
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("➕ Добавить как новый", callback_data="stock_confirm_new"),
        InlineKeyboardButton("❌ Отмена", callback_data="stock_confirm_no"),
    ]])
    await update.message.reply_text(
        f"Товар «{name}» не найден в таблице. Добавить его как новую строку?", reply_markup=kb
    )
    return STOCK_CONFIRM

async def confirm_stock(update, context):
    query = update.callback_query
    await query.answer()
    qty = context.user_data.get("stock_qty")
    user = update.effective_user.first_name

    if query.data == "stock_confirm_fuzzy":
        row = context.user_data["stock_row"]
        matched_name = context.user_data["stock_matched_name"]
        sheets.update_stock(row, matched_name, qty, user)
        await query.edit_message_text(f"✅ «{matched_name}»: остаток обновлён на {qty}")
    elif query.data == "stock_confirm_new":
        name = context.user_data["stock_name"]
        sheets.update_stock(None, name, qty, user)
        await query.edit_message_text(f"✅ «{name}» добавлен как новая строка, остаток {qty}")
    else:
        await query.edit_message_text("Отменено. Нажмите «✏️ Внести остаток» ещё раз.")

    context.user_data.clear()
    return ConversationHandler.END

stock_conv = ConversationHandler(
    entry_points=[MessageHandler(filters.Regex("^✏️ Внести остаток$"), start_stock_entry)],
    states={
        WAITING_STOCK_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_stock)],
        STOCK_CONFIRM: [CallbackQueryHandler(confirm_stock, pattern="^stock_confirm_")],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
)

# ---------- Управление доступом ----------

ADD_USER_INPUT = 10
REMOVE_USER_INPUT = 11
REMOVE_USER_CONFIRM = 12

@require_access(admin_only=True)
async def access_menu(update, context):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить", callback_data="access_add")],
        [InlineKeyboardButton("➖ Удалить", callback_data="access_remove")],
        [InlineKeyboardButton("📋 Список", callback_data="access_list")],
    ])
    await update.message.reply_text("Управление доступом:", reply_markup=kb)

async def access_list(update, context):
    query = update.callback_query
    await query.answer()
    users = sheets.list_users()
    text = "\n".join(f"{r['user_id']} — {r['Имя']} ({r['Роль']})" for r in users) or "Список пуст."
    await query.message.reply_text(text)

async def access_add_start(update, context):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text(
        "Пришли ID и имя через пробел.\nПример: 222222222 Иван"
    )
    return ADD_USER_INPUT

async def access_add_save(update, context):
    try:
        user_id_str, name = update.message.text.strip().split(" ", 1)
        sheets.add_user(int(user_id_str), name.strip(), role="staff")
        await update.message.reply_text(f"✅ Добавлен: {name.strip()} ({user_id_str})")
    except ValueError:
        await update.message.reply_text("Не понял формат. Пример: 222222222 Иван")
        return ADD_USER_INPUT
    return ConversationHandler.END

async def access_remove_start(update, context):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text(
        "Пришли ID пользователя, которого нужно удалить."
    )
    return REMOVE_USER_INPUT

async def access_remove_ask_confirm(update, context):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("ID должен быть числом. Пример: 222222222")
        return REMOVE_USER_INPUT

    user_id = int(text)
    users = {int(r["user_id"]): r["Имя"] for r in sheets.list_users()}
    if user_id not in users:
        await update.message.reply_text("Такого ID нет в списке доступа.")
        return ConversationHandler.END

    context.user_data["remove_id"] = user_id
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, удалить", callback_data="remove_confirm_yes"),
        InlineKeyboardButton("❌ Отмена", callback_data="remove_confirm_no"),
    ]])
    await update.message.reply_text(
        f"Удалить «{users[user_id]}» ({user_id}) из списка доступа?", reply_markup=kb
    )
    return REMOVE_USER_CONFIRM

async def access_remove_confirm(update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "remove_confirm_yes":
        user_id = context.user_data.pop("remove_id", None)
        if user_id and sheets.remove_user(user_id):
            await query.edit_message_text(f"✅ Пользователь {user_id} удалён.")
        else:
            await query.edit_message_text("Не получилось удалить — ID не найден.")
    else:
        await query.edit_message_text("Отменено.")
    return ConversationHandler.END

add_user_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(access_add_start, pattern="^access_add$")],
    states={
        ADD_USER_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, access_add_save)],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
)

remove_user_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(access_remove_start, pattern="^access_remove$")],
    states={
        REMOVE_USER_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, access_remove_ask_confirm)],
        REMOVE_USER_CONFIRM: [CallbackQueryHandler(access_remove_confirm, pattern="^remove_confirm_")],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
)

# ---------- Запуск ----------

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^❓ Помощь$"), help_cmd))
    app.add_handler(MessageHandler(filters.Regex("^👥 Управление доступом$"), access_menu))
    app.add_handler(CallbackQueryHandler(access_list, pattern="^access_list$"))

    # диалоги регистрируются до общих текстовых хендлеров,
    # чтобы они первыми перехватывали ввод внутри диалога
    app.add_handler(stock_conv)
    app.add_handler(add_user_conv)
    app.add_handler(remove_user_conv)

    app.run_polling()

if __name__ == "__main__":
    main()
