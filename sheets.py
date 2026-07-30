import os
import json
import time
import difflib
from datetime import datetime

import gspread
from gspread import Cell
from dotenv import load_dotenv

load_dotenv()

# ---------- Подключение к Google Sheets ----------

if os.environ.get("GOOGLE_CREDENTIALS_JSON"):
    # деплой на Railway: весь JSON-ключ одной строкой в переменной окружения
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    gc = gspread.service_account_from_dict(creds_dict)
else:
    # локальный запуск: обычный файл credentials.json рядом с ботом
    gc = gspread.service_account(
        filename=os.environ.get("GOOGLE_CREDENTIALS_PATH", "credentials.json")
    )

sh = gc.open_by_key(os.environ["SPREADSHEET_ID"])
ws = sh.worksheet("Складской")

# ---------- Остатки (лист "Складской") ----------

def find_product_match(name: str, cutoff: float = 0.72):
    """
    Ищет товар по названию в колонке A.
    Возвращает (row, найденное_название, exact) где:
    - exact=True — точное совпадение (можно сразу писать без переспроса)
    - exact=False — похожее по написанию (нужно подтверждение у пользователя)
    - row=None — ничего похожего не нашлось (предложить добавить как новый)
    """
    names = ws.col_values(1)  # колонка A целиком, включая заголовки категорий
    target = name.strip().lower()

    rows_by_name = {}
    for i, n in enumerate(names, start=1):
        clean = n.strip()
        if not clean:
            continue
        key = clean.lower()
        if key == target:
            return i, clean, True  # точное совпадение — выходим сразу
        rows_by_name[key] = (i, clean)

    close = difflib.get_close_matches(target, rows_by_name.keys(), n=1, cutoff=cutoff)
    if close:
        row, clean = rows_by_name[close[0]]
        return row, clean, False

    return None, None, False


def get_supplier(row: int) -> str:
    return ws.cell(row, 2).value or ""  # колонка B, только для подсказки в ответе


def update_stock(row, name: str, qty, user: str):
    qty_str = str(qty).replace(".", ",")  # русская локаль таблицы: запятая как разделитель
    if row:
        ws.update_cell(row, 3, qty_str)  # колонка C — Остаток
        # если заведёшь колонки "Обновил"/"Дата" (D/E) — раскомментируй:
        # now = datetime.now().strftime("%d.%m.%Y %H:%M")
        # ws.update(f"D{row}:E{row}", [[user, now]])
    else:
        ws.append_row([name, "", qty_str])


def get_all_stock_grouped():
    """
    Читает лист "Складской" и группирует товары по разделам — так же,
    как они выглядят в самой таблице (закрашенные зелёным строки типа
    "Пиво/сидр", "Гарниши" и т.д.).

    Строка считается заголовком раздела, если в колонке A есть текст,
    а в колонке B (Поставщик) пусто — у обычных товаров колонка B
    всегда заполнена (Озон, склад, Драфт-пиво...), даже если остаток
    ещё не внесён. Все строки после заголовка относятся к этому
    разделу — пока не встретится следующий заголовок.

    В результат попадают только товары, у которых остаток (колонка C)
    реально заполнен — это список "что сейчас есть", а не полный
    перечень товаров раздела.

    Возвращает список (category, [(name, qty), ...]).
    Если в начале таблицы есть товары без заголовка раздела —
    они попадут в группу с category=None.
    """
    names = ws.col_values(1)      # колонка A — Название
    suppliers = ws.col_values(2)  # колонка B — Поставщик
    qtys = ws.col_values(3)       # колонка C — Остаток

    groups = []
    current_category = None
    current_items = []

    for i, name in enumerate(names):
        name = (name or "").strip()
        if not name:
            continue
        supplier = suppliers[i].strip() if i < len(suppliers) and suppliers[i] else ""
        qty = qtys[i].strip() if i < len(qtys) and qtys[i] else ""

        if not supplier:
            # заголовок раздела — закрываем предыдущую группу и начинаем новую
            if current_items:
                groups.append((current_category, current_items))
            current_category = name
            current_items = []
            continue

        if not qty:
            continue  # остаток по этому товару ещё не внесён — пропускаем

        current_items.append((name, qty))

    if current_items:
        groups.append((current_category, current_items))

    return groups


def clear_all_stock():
    """
    Очищает колонку "Остаток" (C) у всех товарных строк — названия
    товаров, поставщики и заголовки разделов остаются нетронутыми.
    Возвращает количество очищенных строк.
    """
    names = ws.col_values(1)
    qtys = ws.col_values(3)

    rows_to_clear = []
    for i, name in enumerate(names, start=1):
        clean = (name or "").strip()
        if not clean:
            continue
        qty = qtys[i - 1].strip() if i - 1 < len(qtys) and qtys[i - 1] else ""
        if not qty:
            continue  # уже пусто или это заголовок раздела — пропускаем
        rows_to_clear.append(i)

    if not rows_to_clear:
        return 0

    cell_list = [Cell(row=r, col=3, value="") for r in rows_to_clear]
    ws.update_cells(cell_list)
    return len(rows_to_clear)


# ---------- Доступ (лист "Доступ") ----------

ACCESS_SHEET = "Доступ"
ws_access = sh.worksheet(ACCESS_SHEET)

_cache = {"data": None, "ts": 0}
CACHE_TTL = 40  # секунд — как часто бот перечитывает список пользователей


def _normalize_row(row: dict) -> dict:
    # убирает лишние пробелы в названиях колонок ("Роль " -> "Роль"),
    # чтобы мелкие опечатки в заголовках таблицы не роняли бота
    return {(k or "").strip(): v for k, v in row.items()}


def _row_user_id(row: dict):
    # ищет user_id по паре вариантов названия колонки, терпимо к пустым/битым строкам
    for key in ("user_id", "User_id", "ID", "Id"):
        val = row.get(key)
        if val not in (None, ""):
            try:
                return int(val)
            except (TypeError, ValueError):
                return None
    return None


def _row_role(row: dict) -> str:
    for key in ("Роль", "роль", "Role", "role"):
        if key in row:
            return str(row[key] or "").strip().lower()
    return ""


def _row_name(row: dict) -> str:
    for key in ("Имя", "имя", "Name", "name"):
        if key in row:
            return str(row[key] or "").strip()
    return "?"


# публичные обёртки — использовать из bot.py вместо прямого r["Имя"]/r["user_id"],
# чтобы опечатки в заголовках таблицы не роняли бота с KeyError
row_user_id = _row_user_id
row_name = _row_name
row_role = _row_role


def _load_access():
    if _cache["data"] is None or time.time() - _cache["ts"] > CACHE_TTL:
        raw = ws_access.get_all_records()  # [{"user_id":.., "Имя":.., "Роль":..}, ...]
        _cache["data"] = [_normalize_row(r) for r in raw]
        _cache["ts"] = time.time()
    return _cache["data"]


def _reset_cache():
    _cache["data"] = None


def is_admin(user_id: int) -> bool:
    return any(_row_user_id(r) == user_id and _row_role(r) == "admin" for r in _load_access())


def is_allowed(user_id: int) -> bool:
    return any(_row_user_id(r) == user_id for r in _load_access())


def list_users():
    return _load_access()


def add_user(user_id: int, name: str, role: str = "staff"):
    ws_access.append_row([user_id, name, role])
    _reset_cache()


def remove_user(user_id: int) -> bool:
    for i, r in enumerate(_load_access(), start=2):  # +1 за заголовок, +1 т.к. с 1
        if _row_user_id(r) == user_id:
            ws_access.delete_rows(i)
            _reset_cache()
            return True
    return False
