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
    # кнопка "Очистить таблицу" доступна всем авторизованным — и стафу, и админу
    staff_buttons = [["✏️ Внести остаток"], ["🧹 Очистить таблицу"], ["❓ Помощь"]]
    admin_extra = [["📋 Все остатки", "🗑 Удалить товар"], ["👥 Управление доступом"]]
    buttons = staff_buttons[:2] + (admin_extra if sheets.is_admin(user_id) else []) + staff_buttons[2:]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)


async def start(update, context):
    await update.message.reply_text("Меню:", reply_markup=build_menu(update.effective_user.id))


@require_access()
async def help_cmd(update, context):
    await update.message.reply_text(
        "✏️ Внести остаток — обновить количество товара.\n"
        "🧹 Очистить таблицу — обнулить остатки у всех товаров (с подтверждением).\n"
        "👥 Управление доступом — только для админа."
    )


async def cancel(update, context):
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


# ---------- Все остатки (только для админа) ----------

@require_access(admin_only=True)
async def all_stock(update, context):
    groups = sheets.get_all_stock_grouped()
    if not groups:
        await update.message.reply_text("Список остатков пуст.")
        return

    blocks = []
    for category, items in groups:
        if not items:
            continue
        header = f"📦 {category}" if category else "📦 Без раздела"
        lines = [header] + [f"{name} — {qty}" for name, qty in items]
        blocks.append("\n".join(lines))

    # склеиваем блоки в сообщения так, чтобы не резать блок посередине
    # и не упереться в лимит Telegram на длину сообщения
    messages = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > 3500:
            if current:
                messages.append(current)
            current = block
        else:
            current = candidate
    if current:
        messages.append(current)

    for msg in messages:
        await update.message.reply_text(msg)


# ---------- Очистить таблицу (стаф и админ, с подтверждением) ----------

CLEAR_TABLE_CONFIRM = 20


@require_access()
async def clear_table_start(update, context):
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, очистить", callback_data="clear_table_yes"),
        InlineKeyboardButton("❌ Отмена", callback_data="clear_table_no"),
    ]])
    await update.message.reply_text(
        "⚠️ Это обнулит остатки («Остаток») у ВСЕХ товаров в таблице.\n"
        "Названия товаров и поставщики останутся без изменений.\n\n"
        "Продолжить?",
        reply_markup=kb,
    )
    return CLEAR_TABLE_CONFIRM


async def clear_table_confirm(update, context):
    query = update.callback_query
    await query.answer()

    if query.data == "clear_table_yes":
        count = sheets.clear_all_stock()
        await query.edit_message_text(f"✅ Остатки очищены ({count} товаров).")
    else:
        await query.edit_message_text("Отменено.")

    return ConversationHandler.END


clear_table_conv = ConversationHandler(
    entry_points=[MessageHandler(filters.Regex("^🧹 Очистить таблицу$"), clear_table_start)],
    states={
        CLEAR_TABLE_CONFIRM: [CallbackQueryHandler(clear_table_confirm, pattern="^clear_table_")],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
)


# ---------- Диалог внесения остатка ----------
#
# Можно прислать сразу несколько позиций, по одной в строке:
#   Молоко 12
#   Эспрессо 4
# После обработки бот остаётся в этом же режиме — кнопку "Внести остаток"
# заново нажимать не нужно, пока не напишешь "Готово" или не пройдёт
# STOCK_ENTRY_TIMEOUT секунд без сообщений (тогда режим закроется сам).

WAITING_STOCK_INPUT = 1
STOCK_CONFIRM = 2
STOCK_ENTRY_TIMEOUT = 300  # секунд простоя, после которых режим внесения остатка закрывается сам

FINISH_WORDS = {"готово", "стоп", "конец", "меню", "отмена", "✅ готово"}
STOCK_ENTRY_KB = ReplyKeyboardMarkup([["✅ Готово"]], resize_keyboard=True)


def _parse_stock_line(line: str):
    # "Молоко 12" -> ("Молоко", 12.0); бросает ValueError, если формат не тот
    *name_parts, qty_raw = line.strip().rsplit(" ", 1)
    name = " ".join(name_parts).strip()
    qty = float(qty_raw.replace(",", "."))
    if not name:
        raise ValueError
    return name, qty


def _looks_like_stock_input(text: str) -> bool:
    # хотя бы одна строка похожа на "название количество" — тогда не нужно
    # нажимать кнопку "Внести остаток", бот распознает формат сам
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            _parse_stock_line(line)
            return True
        except ValueError:
            continue
    return False


class _StockLikeFilter(filters.MessageFilter):
    def filter(self, message):
        return _looks_like_stock_input(message.text or "")


stock_like_filter = _StockLikeFilter()


@require_access()
async def start_stock_entry(update, context):
    context.user_data.pop("stock_pending", None)
    context.user_data.pop("stock_current", None)
    await update.message.reply_text(
        "Присылай остатки — можно сразу несколько строк, по одной позиции на строку.\n"
        "Пример:\nМолоко 12\nЭспрессо 4\n\n"
        "Когда закончишь — напиши «Готово» или нажми кнопку ниже.",
        reply_markup=STOCK_ENTRY_KB,
    )
    return WAITING_STOCK_INPUT


async def _ask_next_pending(update, context):
    """Берёт следующую позицию из очереди на подтверждение и спрашивает про неё."""
    pending = context.user_data.get("stock_pending", [])
    if not pending:
        results = context.user_data.pop("stock_results", [])
        if results:
            await update.effective_message.reply_text(
                "\n".join(results) + "\n\nМожно прислать ещё позиции или написать «Готово».",
                reply_markup=STOCK_ENTRY_KB,
            )
        return WAITING_STOCK_INPUT

    item = pending.pop(0)
    context.user_data["stock_current"] = item

    if item["type"] == "fuzzy":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Да, это он", callback_data="stock_confirm_fuzzy"),
            InlineKeyboardButton("❌ Нет, другой товар", callback_data="stock_confirm_no"),
        ]])
        await update.effective_message.reply_text(
            f"Не нашёл точное совпадение для «{item['orig']}». Вы имели в виду «{item['matched_name']}»?",
            reply_markup=kb,
        )
    else:  # "new"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("➕ Добавить как новый", callback_data="stock_confirm_new"),
            InlineKeyboardButton("❌ Пропустить", callback_data="stock_confirm_no"),
        ]])
        await update.effective_message.reply_text(
            f"Товар «{item['name']}» не найден в таблице. Добавить его как новую строку?",
            reply_markup=kb,
        )
    return STOCK_CONFIRM


@require_access()
async def save_stock(update, context):
    text = update.message.text.strip()
    if text.lower() in FINISH_WORDS:
        return await finish_stock_entry(update, context)

    user = update.effective_user.first_name
    results = []
    pending = []

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            name, qty = _parse_stock_line(line)
        except ValueError:
            results.append(f"⚠️ Не понял строку: «{line}» (пример: Молоко 12)")
            continue

        row, matched_name, exact = sheets.find_product_match(name)
        if row and exact:
            sheets.update_stock(row, matched_name, qty, user)
            results.append(f"✅ «{matched_name}»: остаток обновлён на {qty}")
        elif row and not exact:
            pending.append({"type": "fuzzy", "row": row, "matched_name": matched_name, "qty": qty, "orig": name})
        else:
            pending.append({"type": "new", "name": name, "qty": qty})

    context.user_data["stock_results"] = results
    context.user_data["stock_pending"] = pending
    return await _ask_next_pending(update, context)


async def confirm_stock(update, context):
    query = update.callback_query
    await query.answer()

    item = context.user_data.pop("stock_current", None)
    user = update.effective_user.first_name
    results = context.user_data.setdefault("stock_results", [])

    if item is None:
        await query.edit_message_text("Что-то пошло не так, попробуй ещё раз.")
        return await _ask_next_pending(update, context)

    if query.data == "stock_confirm_fuzzy":
        sheets.update_stock(item["row"], item["matched_name"], item["qty"], user)
        results.append(f"✅ «{item['matched_name']}»: остаток обновлён на {item['qty']}")
        await query.edit_message_text(f"✅ «{item['matched_name']}»: остаток обновлён на {item['qty']}")
    elif query.data == "stock_confirm_new":
        sheets.update_stock(None, item["name"], item["qty"], user)
        results.append(f"✅ «{item['name']}» добавлен как новая строка, остаток {item['qty']}")
        await query.edit_message_text(f"✅ «{item['name']}» добавлен как новая строка, остаток {item['qty']}")
    else:
        label = item.get("orig") or item.get("name")
        results.append(f"❌ «{label}» пропущен")
        await query.edit_message_text(f"❌ «{label}» пропущен.")

    return await _ask_next_pending(update, context)


async def finish_stock_entry(update, context):
    context.user_data.clear()
    await update.message.reply_text(
        "Готово, вышли из режима внесения остатков.",
        reply_markup=build_menu(update.effective_user.id),
    )
    return ConversationHandler.END


async def stock_entry_timeout(update, context):
    context.user_data.clear()
    if update.effective_chat:
        await context.bot.send_message(
            update.effective_chat.id,
            "Режим внесения остатков закрылся сам — прошло много времени без сообщений.",
            reply_markup=build_menu(update.effective_chat.id),
        )
    return ConversationHandler.END


stock_conv = ConversationHandler(
    entry_points=[
        MessageHandler(filters.Regex("^✏️ Внести остаток$"), start_stock_entry),
        # прямой ввод "Товар количество" без нажатия кнопки — сразу обрабатывается как save_stock
        MessageHandler(stock_like_filter, save_stock),
    ],
    states={
        WAITING_STOCK_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_stock)],
        STOCK_CONFIRM: [CallbackQueryHandler(confirm_stock, pattern="^stock_confirm_")],
        ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, stock_entry_timeout)],
    },
    fallbacks=[
        CommandHandler("cancel", cancel),
        MessageHandler(filters.Regex("(?i)^(✅ )?(готово|стоп|конец|меню|отмена)$"), finish_stock_entry),
    ],
    conversation_timeout=STOCK_ENTRY_TIMEOUT,
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
    text = "\n".join(
        f"{sheets.row_user_id(r)} — {sheets.row_name(r)} ({sheets.row_role(r)})" for r in users
    ) or "Список пуст."
    await query.message.reply_text(text)


async def access_add_start(update, context):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text(
        "Пришли ID и имя через пробел.\nПример: 222222222 Иван"
    )
    return ADD_USER_INPUT


NEW_USER_WELCOME_TEXT = "🎉 Молодец, теперь ты можешь этим пользоваться!"


async def access_add_save(update, context):
    try:
        user_id_str, name = update.message.text.strip().split(" ", 1)
        new_id = int(user_id_str)
        sheets.add_user(new_id, name.strip(), role="staff")
        await update.message.reply_text(f"✅ Добавлен: {name.strip()} ({user_id_str})")

        try:
            await context.bot.send_message(
                new_id,
                NEW_USER_WELCOME_TEXT,
                reply_markup=build_menu(new_id),
            )
        except Exception:
            await update.message.reply_text(
                "⚠️ Не получилось отправить уведомление пользователю — "
                "скорее всего, он ещё ни разу не запускал бота (/start)."
            )
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
    users = {sheets.row_user_id(r): sheets.row_name(r) for r in sheets.list_users()}
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
    app.add_handler(MessageHandler(filters.Regex("^📋 Все остатки$"), all_stock))
    app.add_handler(MessageHandler(filters.Regex("^👥 Управление доступом$"), access_menu))
    app.add_handler(CallbackQueryHandler(access_list, pattern="^access_list$"))

    # диалоги регистрируются до общих текстовых хендлеров,
    # чтобы они первыми перехватывали ввод внутри диалога
    app.add_handler(stock_conv)
    app.add_handler(clear_table_conv)
    app.add_handler(add_user_conv)
    app.add_handler(remove_user_conv)

    app.run_polling()


if __name__ == "__main__":
    main()
