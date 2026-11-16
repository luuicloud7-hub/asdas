"""
Telegram-калькулятор: aiogram 3 + HTML-разметка + SQLite, всё в одном файле.

Установка:   pip install "aiogram>=3.7"
Запуск:      BOT_TOKEN=123:ABC python calc_bot.py      (Windows: set BOT_TOKEN=...)
"""

import ast
import asyncio
import html
import logging
import math
import operator
import os
import re
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
DB_PATH = os.getenv("DB_PATH", "calc_bot.db")
MAX_EXPR_LEN = 100

router = Router()


# ───────────────────────────── База данных ─────────────────────────────

def _connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def init_db() -> None:
    with _connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                username   TEXT,
                first_name TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                user_id INTEGER PRIMARY KEY,
                expr    TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                expression TEXT NOT NULL,
                result     TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_history_user ON history(user_id, id DESC);
            """
        )


def db_register_user(user_id: int, username: str | None, first_name: str | None) -> None:
    with _connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO users(user_id, username, first_name, created_at) VALUES (?,?,?,?)",
            (user_id, username, first_name, datetime.now().isoformat(timespec="seconds")),
        )


def db_get_expr(user_id: int) -> str:
    with _connect() as db:
        row = db.execute("SELECT expr FROM sessions WHERE user_id=?", (user_id,)).fetchone()
    return row[0] if row else ""


def db_set_expr(user_id: int, expr: str) -> None:
    with _connect() as db:
        db.execute(
            "INSERT INTO sessions(user_id, expr) VALUES (?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET expr=excluded.expr",
            (user_id, expr),
        )


def db_add_history(user_id: int, expression: str, result: str) -> None:
    with _connect() as db:
        db.execute(
            "INSERT INTO history(user_id, expression, result, created_at) VALUES (?,?,?,?)",
            (user_id, expression, result, datetime.now().isoformat(timespec="seconds")),
        )


def db_get_history(user_id: int, limit: int = 10) -> list[tuple[str, str, str]]:
    with _connect() as db:
        return db.execute(
            "SELECT expression, result, created_at FROM history "
            "WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


def db_clear_history(user_id: int) -> int:
    with _connect() as db:
        return db.execute("DELETE FROM history WHERE user_id=?", (user_id,)).rowcount


# ─────────────────────── Безопасный вычислитель ────────────────────────

class CalcError(Exception):
    pass


BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
FUNCS = {
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log10,
    "ln": math.log,
    "abs": abs,
    "round": round,
}
CONSTS = {"pi": math.pi, "e": math.e}


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.Name) and node.id in CONSTS:
        return CONSTS[node.id]
    if isinstance(node, ast.BinOp) and type(node.op) in BIN_OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:
            raise CalcError("Слишком большая степень")
        return BIN_OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in UNARY_OPS:
        return UNARY_OPS[type(node.op)](_eval(node.operand))
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in FUNCS
        and not node.keywords
        and 1 <= len(node.args) <= 2
    ):
        return FUNCS[node.func.id](*[_eval(a) for a in node.args])
    raise CalcError("Не понял выражение")


def _normalize(expr: str) -> str:
    s = (
        expr.replace("×", "*")
        .replace("÷", "/")
        .replace("−", "-")
        .replace("–", "-")
        .replace("^", "**")
        .replace("√", "sqrt")
        .replace("π", "pi")
        .replace(",", ".")
    )
    # неявное умножение: 2(3) → 2*(3), 2pi → 2*pi, )( → )*(
    s = re.sub(r"(\d|\))\s*(?=\(|pi|sqrt|sin|cos|tan|ln|log|abs)", r"\1*", s)
    return s


def format_number(x: float) -> str:
    if math.isnan(x) or math.isinf(x):
        raise CalcError("Результат не определён")
    if x == int(x) and abs(x) < 1e15:
        return str(int(x))
    return format(x, ".12g")


def calculate(expr: str) -> str:
    expr = expr.strip()
    if not expr:
        raise CalcError("Пустое выражение")
    if len(expr) > 200:
        raise CalcError("Слишком длинное выражение")
    try:
        tree = ast.parse(_normalize(expr), mode="eval")
        return format_number(_eval(tree))
    except CalcError:
        raise
    except ZeroDivisionError:
        raise CalcError("Деление на ноль")
    except OverflowError:
        raise CalcError("Слишком большое число")
    except (SyntaxError, ValueError, TypeError, RecursionError):
        raise CalcError("Не понял выражение")


# ───────────────────────────── Клавиатура ──────────────────────────────

LAYOUT = [
    ["C", "⌫", "(", ")", "÷"],
    ["7", "8", "9", "×", "^"],
    ["4", "5", "6", "−", "√("],
    ["1", "2", "3", "+", "%"],
    ["0", "00", ".", "π", "="],
]


def calc_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=k, callback_data=f"k:{k}") for k in row]
            for row in LAYOUT
        ]
    )


def render(expr: str, prev: str = "") -> str:
    text = "🧮 <b>Калькулятор</b>\n\n"
    if prev:
        text += f"<i>{html.escape(prev)} =</i>\n"
    text += f"<code>{html.escape(expr) if expr else '0'}</code>"
    return text


# ───────────────────────────── Хендлеры ────────────────────────────────

HELP_TEXT = (
    "👋 Привет, <b>{name}</b>! Я бот-калькулятор.\n\n"
    "• /calc — калькулятор с кнопками\n"
    "• Или просто пришли выражение: <code>(15 + 5) * 3 / 2</code>\n"
    "• /history — последние вычисления\n"
    "• /clear — очистить историю\n\n"
    "<b>Поддерживается:</b> <code>+ − × ÷ ^ %</code>, скобки, "
    "<code>sqrt sin cos tan ln log abs round</code>, константы <code>pi</code> и <code>e</code>."
)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    u = message.from_user
    await asyncio.to_thread(db_register_user, u.id, u.username, u.first_name)
    await message.answer(HELP_TEXT.format(name=html.escape(u.first_name or "друг")))


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT.format(name=html.escape(message.from_user.first_name or "друг")))


@router.message(Command("calc"))
async def cmd_calc(message: Message) -> None:
    uid = message.from_user.id
    await asyncio.to_thread(db_register_user, uid, message.from_user.username, message.from_user.first_name)
    await asyncio.to_thread(db_set_expr, uid, "")
    await message.answer(render(""), reply_markup=calc_keyboard())


@router.message(Command("history"))
async def cmd_history(message: Message) -> None:
    rows = await asyncio.to_thread(db_get_history, message.from_user.id)
    if not rows:
        await message.answer("История пуста.")
        return
    lines = ["🕘 <b>Последние вычисления:</b>\n"]
    for expr, result, created in rows:
        lines.append(f"<code>{html.escape(expr)} = {html.escape(result)}</code>")
    await message.answer("\n".join(lines))


@router.message(Command("clear"))
async def cmd_clear(message: Message) -> None:
    n = await asyncio.to_thread(db_clear_history, message.from_user.id)
    await message.answer(f"🗑 История очищена (записей удалено: {n}).")


@router.callback_query(F.data.startswith("k:"))
async def on_key(cb: CallbackQuery) -> None:
    key = cb.data[2:]
    uid = cb.from_user.id
    expr = await asyncio.to_thread(db_get_expr, uid)
    prev = ""

    if key == "C":
        expr = ""
    elif key == "⌫":
        expr = expr[:-2] if expr.endswith("√(") else expr[:-1]
    elif key == "=":
        if not expr:
            await cb.answer()
            return
        try:
            result = calculate(expr)
        except CalcError as e:
            await cb.answer(f"⚠️ {e}", show_alert=True)
            return
        await asyncio.to_thread(db_add_history, uid, expr, result)
        prev, expr = expr, result
    else:
        if len(expr) >= MAX_EXPR_LEN:
            await cb.answer("Слишком длинное выражение", show_alert=False)
            return
        expr += key

    await asyncio.to_thread(db_set_expr, uid, expr)
    try:
        await cb.message.edit_text(render(expr, prev), reply_markup=calc_keyboard())
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await cb.answer()


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message) -> None:
    u = message.from_user
    await asyncio.to_thread(db_register_user, u.id, u.username, u.first_name)
    expr = message.text.strip()
    try:
        result = calculate(expr)
    except CalcError as e:
        await message.answer(f"⚠️ {html.escape(str(e))}\nПример: <code>2 * (3 + 4)</code>")
        return
    await asyncio.to_thread(db_add_history, u.id, expr, result)
    await message.answer(f"<code>{html.escape(expr)}</code> = <b>{html.escape(result)}</b>")


# ────────────────────────────── Запуск ─────────────────────────────────

async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if BOT_TOKEN == "PUT_YOUR_TOKEN_HERE":
        raise SystemExit("Укажи токен: переменная окружения BOT_TOKEN (получить у @BotFather)")
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
