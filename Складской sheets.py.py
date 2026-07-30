import os
import json
import time
import difflib
from datetime import datetime

import gspread
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


def get_all_stock():
    """
    Возвращает список (name, qty) по всем товарам листа "Складской".
    Пропускает пустые строки и строки без остатка (например, заголовки
    категорий в колонке A, у которых колонка C пустая).
    """
    names = ws.col_values(1)  # колонка A — Название
    qtys = ws.col_values(3)   # колонка C — Остаток

    result = []
    for i, name in enumerate(names):
        name = (name or "").strip()
        if not name:
            continue
        qty = qtys[i].strip() if i < len(qtys) and qtys[i] else ""
        if not qty:
            continue  # заголовок категории или ещё не внесённый остаток — пропускаем
        result.append((name, qty))
    return result


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
