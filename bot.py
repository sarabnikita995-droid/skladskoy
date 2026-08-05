import os
import sys

from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters,
)

import sheets
from access import require_access, notify_admins_new_user

load_dotenv()
BOT_TOKEN = os.environ["BOT_TOKEN"]

# ---------- Меню и базовые команды ----------

def build_menu(user_id: int) -> ReplyKeyboardMarkup:
    # "Очистить таблицу" и "Добавить позицию" доступны всем авторизованным —
    # и стафу, и админу (поведение "Добавить позицию" различается по роли)
    staff_buttons = [
        ["✏️ Внести остаток", "➕ Добавить позицию"],
        ["🧹 Очистить таблицу"],
        ["❓ Помощь"],
    ]
    admin_extra = [
        ["📋 Все остатки", "🗑 Удалить товар"],
        ["👥 Управление доступом", "📜 Действия"],
        ["🔁 Рестарт", "↩️ Откат очистки"],
    ]
    buttons = staff_buttons[:2] + (admin_extra if sheets.is_admin(user_id) else []) + staff_buttons[2:]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)


async def start(update, context):
    user = update.effective_user
    if not sheets.is_allowed(user.id):
        await notify_admins_new_user(context, user)
    await update.message.reply_text("Меню:", reply_markup=build_menu(user.id))


@require_access()
async def help_cmd(update, context):
    await update.message.reply_text(
        "✏️ Внести остаток — обновить количество товара.\n"
        "➕ Добавить позицию — добавить новый товар (у стафа — отправить запрос админу).\n"
        "🧹 Очистить таблицу — обнулить остатки у всех товаров (с подтверждением).\n"
        "👥 Управление доступом, 📜 Действия, 🔁 Рестарт, ↩️ Откат очистки — только для админа."
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


# ---------- Действия (журнал, только для админа) ----------

@require_access(admin_only=True)
async def actions_log(update, context):
    entries = sheets.get_recent_actions(30)
    if not entries:
        await update.message.reply_text("Журнал действий пуст.")
        return

    lines = []
    for row in entries:
        if len(row) < 4:
            continue
        dt, user_id, name, action = row[0], row[1], row[2], row[3]
        lines.append(f"{dt} — {name} ({user_id}): {action}")

    text = "\n".join(lines) or "Журнал действий пуст."
    for i in range(0, len(text), 3500):
        await update.message.reply_text(text[i:i + 3500])


# ---------- Рестарт бота (только для админа) ----------

@require_access(admin_only=True)
async def restart_bot(update, context):
    sheets.log_action(update.effective_user.id, update.effective_user.first_name, "перезапустил бота")
    await update.message.reply_text("♻️ Перезапускаю бота...")
    # запоминаем чат через переменную окружения — после перезапуска (execv
    # заменяет процесс, но сохраняет окружение) main() увидит её и пришлёт
    # подтверждение, что бот снова на связи
    os.environ["RESTART_CHAT_ID"] = str(update.effective_chat.id)
    os.execv(sys.executable, [sys.executable] + sys.argv)


# ---------- Откат последней очистки таблицы (только для админа) ----------

@require_access(admin_only=True)
async def undo_clear(update, context):
    count = sheets.undo_last_clear()
    if count is None:
        await update.message.reply_text(
            "Нечего откатывать — недавней очистки не было (или она уже откачена)."
        )
        return
    sheets.log_action(
        update.effective_user.id,
        update.effective_user.first_name,
        f"откатил очистку таблицы ({count} товаров)",
    )
    await update.message.reply_text(f"↩️ Остатки восстановлены ({count} товаров).")


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
        sheets.log_action(
            update.effective_user.id,
            update.effective_user.first_name,
            f"очистил остатки у {count} товаров",
        )
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


# ---------- Добавить позицию ----------
#
# Кнопка одна для всех, но поведение разное по роли:
# - админ вводит "Название; Поставщик; Остаток" и позиция добавляется сразу в таблицу
# - стаф просто пишет название — бот пересылает запрос всем админам

ADD_ITEM_INPUT = 21


@require_access()
async def add_item_start(update, context):
    if sheets.is_admin(update.effective_user.id):
        await update.message.reply_text(
            "Пришли новую позицию в формате:\n"
            "Название; Поставщик; Остаток\n"
            "Остаток можно не указывать.\n"
            "Пример: Печенье; Озон; 10"
        )
    else:
        await update.message.reply_text(
            "Напиши название позиции, которую нужно добавить в таблицу — "
            "перешлю запрос администратору."
        )
    return ADD_ITEM_INPUT


async def add_item_save(update, context):
    text = update.message.text.strip()
    user = update.effective_user

    if sheets.is_admin(user.id):
        parts = [p.strip() for p in text.split(";")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            await update.message.reply_text(
                "Не понял формат. Пример: Печенье; Озон; 10 (остаток можно не указывать)"
            )
            return ADD_ITEM_INPUT

        name, supplier = parts[0], parts[1]
        qty = parts[2] if len(parts) > 2 and parts[2] else ""
        sheets.add_item(name, supplier, qty)
        sheets.log_action(
            user.id, user.first_name,
            f"добавил новую позицию «{name}» ({supplier})" + (f", остаток {qty}" if qty else ""),
        )
        await update.message.reply_text(f"✅ Позиция «{name}» добавлена.", reply_markup=build_menu(user.id))
        return ConversationHandler.END

    # стаф — не добавляем сами, а пересылаем запрос админам
    item_name = text
    sheets.log_action(user.id, user.first_name, f"запросил добавление позиции «{item_name}»")

    username = f"@{user.username}" if user.username else "(без юзернейма)"
    notify_text = (
        "📩 Запрос на добавление позиции\n"
        f"От: {user.first_name} {username} (ID {user.id})\n"
        f"Позиция: {item_name}"
    )
    for row in sheets.list_users():
        if sheets.row_role(row) != "admin":
            continue
        admin_id = sheets.row_user_id(row)
        if not admin_id:
            continue
        try:
            await context.bot.send_message(admin_id, notify_text)
        except Exception:
            pass

    await update.message.reply_text("✅ Запрос отправлен администратору.", reply_markup=build_menu(user.id))
    return ConversationHandler.END


add_item_conv = ConversationHandler(
    entry_points=[MessageHandler(filters.Regex("^➕ Добавить позицию$"), add_item_start)],
    states={
        ADD_ITEM_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_item_save)],
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


async def _prompt_existing(update, context, item):
    """Спрашивает, что делать с товаром, у которого уже есть остаток: заменить или суммировать."""
    context.user_data["stock_current"] = item
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔁 Заменить", callback_data="stock_confirm_replace"),
            InlineKeyboardButton("➕ Суммировать", callback_data="stock_confirm_sum"),
        ],
        [InlineKeyboardButton("❌ Пропустить", callback_data="stock_confirm_no")],
    ])
    await update.effective_message.reply_text(
        f"У «{item['matched_name']}» уже есть остаток {item['current_qty']}.\n"
        f"Что сделать с новым значением {item['qty']}?",
        reply_markup=kb,
    )
    return STOCK_CONFIRM


async def _prompt_similar(update, context, item):
    """Показывает похожие позиции, когда точного совпадения не нашлось."""
    context.user_data["stock_current"] = item
    candidates = item["candidates"]
    buttons = [
        [InlineKeyboardButton(f"✅ {c}", callback_data=f"stock_pick_{i}")]
        for i, c in enumerate(candidates)
    ]
    buttons.append([InlineKeyboardButton("➕ Добавить как новый", callback_data="stock_confirm_new")])
    buttons.append([InlineKeyboardButton("❌ Пропустить", callback_data="stock_confirm_no")])
    lines = [f"Не нашёл «{item['orig']}». Возможно, вы имели в виду:"] + [f"• {c}" for c in candidates]
    await update.effective_message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))
    return STOCK_CONFIRM


async def _prompt_new(update, context, item):
    """Спрашивает, добавить ли товар как новую строку — когда вообще ничего похожего нет."""
    context.user_data["stock_current"] = item
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("➕ Добавить как новый", callback_data="stock_confirm_new"),
        InlineKeyboardButton("❌ Пропустить", callback_data="stock_confirm_no"),
    ]])
    await update.effective_message.reply_text(
        f"Товар «{item['name']}» не найден в таблице. Добавить его как новую строку?",
        reply_markup=kb,
    )
    return STOCK_CONFIRM


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

    if item["type"] == "existing":
        return await _prompt_existing(update, context, item)
    if item["type"] == "similar":
        return await _prompt_similar(update, context, item)
    return await _prompt_new(update, context, item)  # "new"


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

        row, exact_name = sheets.find_exact_product(name)
        if row:
            current_qty = sheets.get_current_qty(row)
            if current_qty:
                # у товара уже есть остаток — спросим, заменить или суммировать
                pending.append({
                    "type": "existing",
                    "row": row,
                    "matched_name": exact_name,
                    "qty": qty,
                    "current_qty": current_qty,
                })
            else:
                sheets.update_stock(row, exact_name, qty, user)
                sheets.log_action(update.effective_user.id, user, f"внёс остаток «{exact_name}»: {qty}")
                results.append(f"✅ «{exact_name}»: остаток обновлён на {qty}")
            continue

        similar = sheets.find_similar_products(name)
        if similar:
            pending.append({"type": "similar", "orig": name, "qty": qty, "candidates": similar})
        else:
            pending.append({"type": "new", "name": name, "qty": qty})

    context.user_data["stock_results"] = results
    context.user_data["stock_pending"] = pending
    return await _ask_next_pending(update, context)


async def confirm_stock(update, context):
    query = update.callback_query
    await query.answer()

    item = context.user_data.get("stock_current")
    user = update.effective_user.first_name
    results = context.user_data.setdefault("stock_results", [])

    if item is None:
        await query.edit_message_text("Что-то пошло не так, попробуй ещё раз.")
        return await _ask_next_pending(update, context)

    data = query.data

    # выбор конкретной позиции из списка похожих
    if data.startswith("stock_pick_"):
        try:
            idx = int(data[len("stock_pick_"):])
        except ValueError:
            idx = -1
        candidates = item.get("candidates", [])

        if idx < 0 or idx >= len(candidates):
            await query.edit_message_text("Что-то пошло не так, попробуй ещё раз.")
            context.user_data.pop("stock_current", None)
            return await _ask_next_pending(update, context)

        chosen_name = candidates[idx]
        row, exact_name = sheets.find_exact_product(chosen_name)
        if row is None:
            await query.edit_message_text("Не нашёл эту позицию — возможно, таблица изменилась.")
            context.user_data.pop("stock_current", None)
            return await _ask_next_pending(update, context)

        current_qty = sheets.get_current_qty(row)
        if current_qty:
            await query.edit_message_text(f"Выбрано: «{exact_name}»")
            new_item = {
                "type": "existing", "row": row, "matched_name": exact_name,
                "qty": item["qty"], "current_qty": current_qty,
            }
            return await _prompt_existing(update, context, new_item)

        sheets.update_stock(row, exact_name, item["qty"], user)
        sheets.log_action(update.effective_user.id, user, f"внёс остаток «{exact_name}»: {item['qty']}")
        results.append(f"✅ «{exact_name}»: остаток обновлён на {item['qty']}")
        await query.edit_message_text(f"✅ «{exact_name}»: остаток обновлён на {item['qty']}")
        context.user_data.pop("stock_current", None)
        return await _ask_next_pending(update, context)

    context.user_data.pop("stock_current", None)

    if data == "stock_confirm_new":
        name = item.get("orig") or item.get("name")
        sheets.update_stock(None, name, item["qty"], user)
        sheets.log_action(update.effective_user.id, user, f"добавил новый товар «{name}»: {item['qty']}")
        results.append(f"✅ «{name}» добавлен как новая строка, остаток {item['qty']}")
        await query.edit_message_text(f"✅ «{name}» добавлен как новая строка, остаток {item['qty']}")
    elif data == "stock_confirm_replace":
        sheets.update_stock(item["row"], item["matched_name"], item["qty"], user)
        sheets.log_action(
            update.effective_user.id, user,
            f"заменил остаток «{item['matched_name']}»: {item['current_qty']} → {item['qty']}",
        )
        results.append(f"✅ «{item['matched_name']}»: остаток заменён на {item['qty']}")
        await query.edit_message_text(f"✅ «{item['matched_name']}»: остаток заменён на {item['qty']}")
    elif data == "stock_confirm_sum":
        try:
            current = float(str(item["current_qty"]).replace(",", "."))
            new_total = current + float(item["qty"])
        except ValueError:
            new_total = item["qty"]  # на случай, если старое значение не число
        if isinstance(new_total, float) and new_total == int(new_total):
            new_total = int(new_total)
        sheets.update_stock(item["row"], item["matched_name"], new_total, user)
        sheets.log_action(
            update.effective_user.id, user,
            f"суммировал остаток «{item['matched_name']}»: {item['current_qty']} + {item['qty']} = {new_total}",
        )
        results.append(f"✅ «{item['matched_name']}»: остаток теперь {new_total}")
        await query.edit_message_text(f"✅ «{item['matched_name']}»: остаток теперь {new_total}")
    else:  # stock_confirm_no — пропустить
        label = item.get("orig") or item.get("name") or item.get("matched_name")
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
        # "^stock_" — единый префикс для всех callback_data этого диалога:
        # stock_confirm_* (новый/замена/сумма/пропустить) и stock_pick_N (выбор из похожих)
        STOCK_CONFIRM: [CallbackQueryHandler(confirm_stock, pattern="^stock_")],
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
        sheets.log_action(
            update.effective_user.id,
            update.effective_user.first_name,
            f"добавил пользователя {name.strip()} ({new_id})",
        )
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
            sheets.log_action(
                update.effective_user.id,
                update.effective_user.first_name,
                f"удалил пользователя {user_id}",
            )
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

async def post_init(app):
    """
    Срабатывает один раз сразу после старта бота. Если процесс был
    перезапущен через кнопку "Рестарт", в окружении осталась метка
    с чатом, куда нужно отчитаться, что бот снова на связи.
    """
    chat_id = os.environ.pop("RESTART_CHAT_ID", None)
    if not chat_id:
        return
    try:
        await app.bot.send_message(int(chat_id), "✅ Бот перезапущен и готов к работе.")
    except Exception:
        pass


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^❓ Помощь$"), help_cmd))
    app.add_handler(MessageHandler(filters.Regex("^📋 Все остатки$"), all_stock))
    app.add_handler(MessageHandler(filters.Regex("^📜 Действия$"), actions_log))
    app.add_handler(MessageHandler(filters.Regex("^👥 Управление доступом$"), access_menu))
    app.add_handler(MessageHandler(filters.Regex("^🔁 Рестарт$"), restart_bot))
    app.add_handler(MessageHandler(filters.Regex("^↩️ Откат очистки$"), undo_clear))
    app.add_handler(CallbackQueryHandler(access_list, pattern="^access_list$"))

    # диалоги регистрируются в таком порядке специально: узкие диалоги
    # со свободным текстовым вводом (add_item_conv, add_user_conv,
    # remove_user_conv) — раньше stock_conv. У stock_conv "жадный" фильтр
    # (распознаёт любой текст вида "название число" как остаток), и если
    # его проверить первым, он может случайно перехватить ввод, который
    # на самом деле предназначен другому активному диалогу (например,
    # формат "Название; Поставщик; 10" при добавлении позиции админом).
    app.add_handler(add_item_conv)
    app.add_handler(add_user_conv)
    app.add_handler(remove_user_conv)
    app.add_handler(stock_conv)
    app.add_handler(clear_table_conv)

    app.run_polling()


if __name__ == "__main__":
    main()
