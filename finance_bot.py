# !/usr/bin/env python3
"""
Telegram-бот для учёта финансов.
Полностью рабочий, без зависаний.
"""

import logging
import sqlite3
import io
import csv
import numpy as np
from datetime import datetime, timedelta, time as dt_time
from decimal import Decimal
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
import asyncio
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, filters, CallbackQueryHandler, JobQueue
)
from telegram.request import HTTPXRequest
import matplotlib.pyplot as plt
from matplotlib import rcParams
import tempfile
import os

# Настройка matplotlib для работы без GUI
plt.switch_backend('Agg')
rcParams['font.family'] = 'DejaVu Sans'

TOKEN = 'СВОЙ ТОКЕН'

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# Состояния
(
    ADD_AMOUNT, ADD_TYPE, ADD_CATEGORY,
    REPORT_INTERVAL, REPORT_DAY, EXPORT,
    EDIT_ID, EDIT_ACTION, EDIT_NEW_VALUE,
    DELETE_ID, DELETE_CONFIRM,
    CAT_MENU, CAT_CREATE_NAME, CAT_CREATE_TYPE, CAT_DELETE_CHOOSE, CAT_DELETE_CONFIRM,
    CAT_RENAME_CHOOSE, CAT_RENAME_NEW, BUDGET_CAT, BUDGET_PERIOD, BUDGET_AMOUNT,
    CHARTS_MENU, CHARTS_MONTH_YEAR, CHARTS_YEAR,
    REMINDER_MENU, REMINDER_ADD_NAME, REMINDER_ADD_AMOUNT, REMINDER_ADD_DATE,
    REMINDER_ADD_FREQUENCY, REMINDER_ADD_INTERVAL, REMINDER_ADD_TYPE,
    REMINDER_LIST, REMINDER_DELETE
) = range(33)


# ---------- База данных ----------
def get_db():
    conn = sqlite3.connect('finance.db')
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            lang TEXT DEFAULT 'ru'
        )''')
        c.execute("PRAGMA table_info(users)")
        cols = [col[1] for col in c.fetchall()]
        if 'lang' not in cols:
            c.execute("ALTER TABLE users ADD COLUMN lang TEXT DEFAULT 'ru'")
        c.execute('''CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount DECIMAL(10,2),
            type TEXT,
            category TEXT,
            description TEXT,
            date TEXT
        )''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_user_date ON transactions(user_id, date)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_user_type ON transactions(user_id, type)')
        c.execute('''CREATE TABLE IF NOT EXISTS categories (
            user_id INTEGER,
            name TEXT,
            type TEXT,
            PRIMARY KEY (user_id, name)
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS budgets (
            user_id INTEGER,
            category TEXT,
            amount DECIMAL(10,2),
            month TEXT,
            PRIMARY KEY (user_id, category, month)
        )''')

        # Обновленная таблица для напоминаний - удаляем старую и создаем новую
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='reminders'")
        table_exists = c.fetchone()

        if table_exists:
            # Проверяем структуру существующей таблицы
            c.execute("PRAGMA table_info(reminders)")
            existing_cols = [col[1] for col in c.fetchall()]

            # Если таблица старая (без колонки name), удаляем её и создаем заново
            if 'name' not in existing_cols:
                c.execute("DROP TABLE reminders")
                table_exists = None

        if not table_exists:
            c.execute('''CREATE TABLE reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                name TEXT,
                amount DECIMAL(10,2),
                reminder_date TEXT,
                frequency TEXT,
                interval_days INTEGER,
                type TEXT,
                active INTEGER DEFAULT 1,
                last_notified TEXT,
                next_notification TEXT
            )''')
        else:
            # Проверяем и добавляем недостающие колонки
            c.execute("PRAGMA table_info(reminders)")
            existing_cols = [col[1] for col in c.fetchall()]

            if 'next_notification' not in existing_cols:
                c.execute("ALTER TABLE reminders ADD COLUMN next_notification TEXT")
            if 'reminder_date' not in existing_cols:
                c.execute("ALTER TABLE reminders ADD COLUMN reminder_date TEXT")
            if 'frequency' not in existing_cols:
                c.execute("ALTER TABLE reminders ADD COLUMN frequency TEXT DEFAULT 'once'")
            if 'interval_days' not in existing_cols:
                c.execute("ALTER TABLE reminders ADD COLUMN interval_days INTEGER DEFAULT 0")

        conn.commit()


def register_user(user_id, username, first_name):
    with get_db() as conn:
        c = conn.cursor()
        c.execute('INSERT OR IGNORE INTO users (user_id, username, first_name, lang) VALUES (?,?,?,?)',
                  (user_id, username, first_name, 'ru'))
        default_cats = [
            ('Зарплата', 'income'), ('Фриланс', 'income'), ('Подарок', 'income'), ('Инвестиции', 'income'),
            ('Другое', 'income'),
            ('Продукты', 'expense'), ('Транспорт', 'expense'), ('Кафе', 'expense'), ('Развлечения', 'expense'),
            ('Связь', 'expense'),
            ('Здоровье', 'expense'), ('Дом', 'expense'), ('Одежда', 'expense'), ('Общая', 'expense')
        ]
        for name, typ in default_cats:
            c.execute('INSERT OR IGNORE INTO categories (user_id, name, type) VALUES (?,?,?)', (user_id, name, typ))
        conn.commit()


def get_user_lang(user_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT lang FROM users WHERE user_id = ?', (user_id,))
        row = c.fetchone()
        return row['lang'] if row else 'ru'


def set_user_lang(user_id, lang):
    with get_db() as conn:
        c = conn.cursor()
        c.execute('UPDATE users SET lang = ? WHERE user_id = ?', (lang, user_id))
        conn.commit()


# ---------- Функции для напоминаний ----------
def add_reminder(user_id, name, amount, reminder_date, frequency, interval_days, typ='expense'):
    """Добавляет новое напоминание (тип по умолчанию расход)"""
    next_date = calculate_next_date(reminder_date, frequency, interval_days)
    with get_db() as conn:
        c = conn.cursor()
        c.execute('''INSERT INTO reminders (user_id, name, amount, reminder_date, frequency, interval_days, type, active, next_notification)
                     VALUES (?,?,?,?,?,?,?,1,?)''',
                  (user_id, name, float(amount), reminder_date, frequency, interval_days, typ, next_date))
        conn.commit()
        return c.lastrowid


def get_user_reminders(user_id, active_only=True):
    """Получает все напоминания пользователя"""
    with get_db() as conn:
        c = conn.cursor()
        if active_only:
            c.execute(
                'SELECT id, name, amount, reminder_date, frequency, interval_days, type, active, next_notification FROM reminders WHERE user_id=? AND active=1 ORDER BY next_notification',
                (user_id,))
        else:
            c.execute(
                'SELECT id, name, amount, reminder_date, frequency, interval_days, type, active, next_notification FROM reminders WHERE user_id=? ORDER BY next_notification',
                (user_id,))
        return c.fetchall()


def get_reminder_by_id(user_id, reminder_id):
    """Получает напоминание по ID"""
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT id, name, amount, reminder_date, frequency, interval_days, type FROM reminders WHERE user_id=? AND id=?',
            (user_id, reminder_id))
        return c.fetchone()


def delete_reminder(user_id, reminder_id):
    """Удаляет напоминание"""
    with get_db() as conn:
        c = conn.cursor()
        c.execute('DELETE FROM reminders WHERE user_id=? AND id=?', (user_id, reminder_id))
        conn.commit()
        return True


def deactivate_reminder(user_id, reminder_id):
    """Деактивирует напоминание (не удаляет)"""
    with get_db() as conn:
        c = conn.cursor()
        c.execute('UPDATE reminders SET active=0 WHERE user_id=? AND id=?', (user_id, reminder_id))
        conn.commit()


def update_notification(reminder_id, next_date):
    """Обновляет дату следующего уведомления"""
    today_str = datetime.now().strftime('%Y-%m-%d')
    with get_db() as conn:
        c = conn.cursor()
        c.execute('UPDATE reminders SET last_notified=?, next_notification=? WHERE id=?',
                  (today_str, next_date, reminder_id))
        conn.commit()


def calculate_next_date(current_date, frequency, interval_days):
    """Рассчитывает следующую дату напоминания"""
    date_obj = datetime.strptime(current_date, '%Y-%m-%d')

    if frequency == 'once':
        return None
    elif frequency == 'daily':
        next_date = date_obj + timedelta(days=interval_days)
    elif frequency == 'weekly':
        next_date = date_obj + timedelta(weeks=interval_days)
    elif frequency == 'monthly':
        # Добавляем месяцы
        year = date_obj.year + (date_obj.month + interval_days - 1) // 12
        month = (date_obj.month + interval_days - 1) % 12 + 1
        day = min(date_obj.day,
                  [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30,
                   31, 30, 31][month - 1])
        next_date = datetime(year, month, day)
    elif frequency == 'yearly':
        next_date = date_obj.replace(year=date_obj.year + interval_days)
    else:
        return None

    return next_date.strftime('%Y-%m-%d')


def get_reminders_for_today():
    """Получает напоминания на сегодня"""
    today_str = datetime.now().strftime('%Y-%m-%d')
    with get_db() as conn:
        c = conn.cursor()
        c.execute('''SELECT id, user_id, name, amount, type, frequency, interval_days, reminder_date, next_notification
                     FROM reminders 
                     WHERE active=1 AND next_notification <= ? 
                     AND (last_notified IS NULL OR last_notified != ?)''',
                  (today_str, today_str))
        return c.fetchall()


# ----- Напоминания -----
async def reminder_menu(update, context):
    uid = update.effective_user.id
    kb = ReplyKeyboardMarkup([
        [get_text(uid, 'reminder_add'), get_text(uid, 'reminder_list')],
        [get_text(uid, 'reminder_delete'), get_text(uid, 'back_btn')]
    ], resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text(get_text(uid, 'reminder_menu'), reply_markup=kb)
    return REMINDER_MENU


async def reminder_choice(update, context):
    uid = update.effective_user.id
    choice = update.message.text

    if choice == get_text(uid, 'back_btn'):
        await update.message.reply_text(get_text(uid, 'cancel'), reply_markup=main_keyboard(uid))
        return ConversationHandler.END
    elif choice == get_text(uid, 'reminder_add'):
        # Здесь должна быть отправка сообщения с просьбой ввести название
        await update.message.reply_text(get_text(uid, 'reminder_add_name'), reply_markup=ReplyKeyboardRemove())
        return REMINDER_ADD_NAME
    elif choice == get_text(uid, 'reminder_list'):
        reminders = get_user_reminders(uid)
        if not reminders:
            await update.message.reply_text(get_text(uid, 'reminder_empty'), reply_markup=main_keyboard(uid))
            return ConversationHandler.END

        text = get_text(uid, 'reminder_list_header')
        freq_text = get_text(uid, 'reminder_frequency_text')
        for r in reminders:
            typ_text = "расход" if r[6] == 'expense' else "доход"
            freq_name = freq_text.get(r[4], r[4]) if r[4] else 'одноразовое'
            reminder_date = r[3] if r[3] else 'не указана'
            text += get_text(uid, 'reminder_list_item', r[0], r[1], r[2], reminder_date, freq_name, typ_text)

        if len(text) > 4000:
            for i in range(0, len(text), 4000):
                await update.message.reply_text(text[i:i + 4000], reply_markup=main_keyboard(uid))
        else:
            await update.message.reply_text(text, reply_markup=main_keyboard(uid))
        return ConversationHandler.END
    elif choice == get_text(uid, 'reminder_delete'):
        reminders = get_user_reminders(uid)
        if not reminders:
            await update.message.reply_text(get_text(uid, 'reminder_empty'), reply_markup=main_keyboard(uid))
            return ConversationHandler.END

        text = get_text(uid, 'reminder_list_header')
        freq_text = get_text(uid, 'reminder_frequency_text')
        for r in reminders:
            typ_text = "расход" if r[6] == 'expense' else "доход"
            freq_name = freq_text.get(r[4], r[4]) if r[4] else 'одноразовое'
            reminder_date = r[3] if r[3] else 'не указана'
            text += get_text(uid, 'reminder_list_item', r[0], r[1], r[2], reminder_date, freq_name, typ_text)
        text += "\n" + get_text(uid, 'reminder_delete_prompt')

        await update.message.reply_text(text, reply_markup=ReplyKeyboardRemove())
        return REMINDER_DELETE
    else:
        await update.message.reply_text(get_text(uid, 'reminder_menu'))
        return REMINDER_MENU


async def reminder_add_name(update, context):
    uid = update.effective_user.id
    name = update.message.text.strip()

    if name == '0':
        await update.message.reply_text(get_text(uid, 'cancel'), reply_markup=main_keyboard(uid))
        return ConversationHandler.END

    context.user_data['reminder_name'] = name
    await update.message.reply_text(get_text(uid, 'reminder_add_amount'))
    return REMINDER_ADD_AMOUNT


async def reminder_add_amount(update, context):
    uid = update.effective_user.id
    try:
        amount = Decimal(update.message.text.replace(',', '.'))
        if amount <= 0:
            raise ValueError
    except:
        await update.message.reply_text(get_text(uid, 'add_amount_invalid'))
        return REMINDER_ADD_AMOUNT

    context.user_data['reminder_amount'] = amount
    await update.message.reply_text(get_text(uid, 'reminder_add_date'))
    return REMINDER_ADD_DATE


async def reminder_add_date(update, context):
    uid = update.effective_user.id
    text = update.message.text.strip()

    if text == '0':
        await update.message.reply_text(get_text(uid, 'cancel'), reply_markup=main_keyboard(uid))
        return ConversationHandler.END

    # Проверяем формат даты
    try:
        # Поддерживаем форматы: ДД.ММ.ГГГГ или сегодня
        if text.lower() == 'сегодня' or text.lower() == 'today':
            reminder_date = datetime.now().strftime('%Y-%m-%d')
        else:
            # Проверяем формат ДД.ММ.ГГГГ
            date_obj = datetime.strptime(text, '%d.%m.%Y')
            reminder_date = date_obj.strftime('%Y-%m-%d')

        context.user_data['reminder_date'] = reminder_date

        # Переходим к выбору периодичности
        buttons = [[get_text(uid, 'reminder_once'), get_text(uid, 'reminder_recurring')]]
        await update.message.reply_text(get_text(uid, 'reminder_add_frequency'),
                                        reply_markup=ReplyKeyboardMarkup(buttons, resize_keyboard=True,
                                                                         one_time_keyboard=True))
        return REMINDER_ADD_FREQUENCY

    except ValueError:
        await update.message.reply_text("❌ Неверный формат даты. Используйте ДД.ММ.ГГГГ или 'сегодня'")
        return REMINDER_ADD_DATE


async def reminder_add_frequency(update, context):
    uid = update.effective_user.id
    choice = update.message.text

    if choice == get_text(uid, 'reminder_once'):
        context.user_data['reminder_frequency'] = 'once'
        context.user_data['reminder_interval'] = 0
        # Тип всегда расход
        typ = 'expense'

        reminder_id = add_reminder(uid,
                                   context.user_data['reminder_name'],
                                   context.user_data['reminder_amount'],
                                   context.user_data['reminder_date'],
                                   context.user_data['reminder_frequency'],
                                   context.user_data['reminder_interval'],
                                   typ)

        freq_text = get_text(uid, 'reminder_frequency_text')
        frequency_name = freq_text.get(context.user_data['reminder_frequency'], context.user_data['reminder_frequency'])

        await update.message.reply_text(get_text(uid, 'reminder_add_success',
                                                 context.user_data['reminder_name'],
                                                 context.user_data['reminder_amount'],
                                                 context.user_data['reminder_date'],
                                                 frequency_name,
                                                 "расход"),
                                        reply_markup=main_keyboard(uid))

        # Очищаем данные
        del context.user_data['reminder_name']
        del context.user_data['reminder_amount']
        del context.user_data['reminder_date']
        del context.user_data['reminder_frequency']
        del context.user_data['reminder_interval']

        return ConversationHandler.END

    elif choice == get_text(uid, 'reminder_recurring'):
        buttons = [
            [get_text(uid, 'reminder_daily')],
            [get_text(uid, 'reminder_weekly')],
            [get_text(uid, 'reminder_monthly')],
            [get_text(uid, 'reminder_yearly')]
        ]
        await update.message.reply_text(get_text(uid, 'reminder_interval'),
                                        reply_markup=ReplyKeyboardMarkup(buttons, resize_keyboard=True,
                                                                         one_time_keyboard=True))
        return REMINDER_ADD_INTERVAL
    else:
        await update.message.reply_text(get_text(uid, 'reminder_add_frequency'))
        return REMINDER_ADD_FREQUENCY


async def reminder_add_interval(update, context):
    uid = update.effective_user.id
    choice = update.message.text

    if choice == get_text(uid, 'reminder_daily'):
        context.user_data['reminder_frequency'] = 'daily'
        context.user_data['reminder_interval'] = 1
    elif choice == get_text(uid, 'reminder_weekly'):
        context.user_data['reminder_frequency'] = 'weekly'
        context.user_data['reminder_interval'] = 1
    elif choice == get_text(uid, 'reminder_monthly'):
        context.user_data['reminder_frequency'] = 'monthly'
        context.user_data['reminder_interval'] = 1
    elif choice == get_text(uid, 'reminder_yearly'):
        context.user_data['reminder_frequency'] = 'yearly'
        context.user_data['reminder_interval'] = 1
    else:
        await update.message.reply_text(get_text(uid, 'reminder_interval'))
        return REMINDER_ADD_INTERVAL

    # Тип всегда расход
    typ = 'expense'

    reminder_id = add_reminder(uid,
                               context.user_data['reminder_name'],
                               context.user_data['reminder_amount'],
                               context.user_data['reminder_date'],
                               context.user_data['reminder_frequency'],
                               context.user_data['reminder_interval'],
                               typ)

    freq_text = get_text(uid, 'reminder_frequency_text')
    frequency_name = freq_text.get(context.user_data['reminder_frequency'], context.user_data['reminder_frequency'])

    await update.message.reply_text(get_text(uid, 'reminder_add_success',
                                             context.user_data['reminder_name'],
                                             context.user_data['reminder_amount'],
                                             context.user_data['reminder_date'],
                                             frequency_name,
                                             "расход"),
                                    reply_markup=main_keyboard(uid))

    # Очищаем данные
    del context.user_data['reminder_name']
    del context.user_data['reminder_amount']
    del context.user_data['reminder_date']
    del context.user_data['reminder_frequency']
    del context.user_data['reminder_interval']

    return ConversationHandler.END


async def reminder_add_type(update, context):
    uid = update.effective_user.id
    choice = update.message.text

    if choice == get_text(uid, 'reminder_expense_btn'):
        typ = 'expense'
        typ_text = "расход"
    elif choice == get_text(uid, 'reminder_income_btn'):
        typ = 'income'
        typ_text = "доход"
    else:
        await update.message.reply_text(get_text(uid, 'reminder_add_type'))
        return REMINDER_ADD_TYPE

    reminder_id = add_reminder(uid,
                               context.user_data['reminder_name'],
                               context.user_data['reminder_amount'],
                               context.user_data['reminder_date'],
                               context.user_data['reminder_frequency'],
                               context.user_data['reminder_interval'],
                               typ)

    freq_text = get_text(uid, 'reminder_frequency_text')
    frequency_name = freq_text.get(context.user_data['reminder_frequency'], context.user_data['reminder_frequency'])

    await update.message.reply_text(get_text(uid, 'reminder_add_success',
                                             context.user_data['reminder_name'],
                                             context.user_data['reminder_amount'],
                                             context.user_data['reminder_date'],
                                             frequency_name,
                                             typ_text),
                                    reply_markup=main_keyboard(uid))

    # Очищаем данные
    del context.user_data['reminder_name']
    del context.user_data['reminder_amount']
    del context.user_data['reminder_date']
    del context.user_data['reminder_frequency']
    del context.user_data['reminder_interval']

    return ConversationHandler.END


async def reminder_delete(update, context):
    uid = update.effective_user.id
    try:
        reminder_id = int(update.message.text)
        if reminder_id == 0:
            await update.message.reply_text(get_text(uid, 'cancel'), reply_markup=main_keyboard(uid))
            return ConversationHandler.END
    except:
        await update.message.reply_text(get_text(uid, 'reminder_delete_prompt'))
        return REMINDER_DELETE

    reminder = get_reminder_by_id(uid, reminder_id)
    if not reminder:
        await update.message.reply_text(get_text(uid, 'reminder_not_found'), reply_markup=main_keyboard(uid))
        return ConversationHandler.END

    delete_reminder(uid, reminder_id)
    await update.message.reply_text(get_text(uid, 'reminder_deleted'), reply_markup=main_keyboard(uid))
    return ConversationHandler.END


# ----- Фоновая задача для напоминаний -----
async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Ежедневная проверка напоминаний"""
    reminders = get_reminders_for_today()

    if not reminders:
        return

    # Группируем напоминания по пользователям
    user_reminders = {}
    for r in reminders:
        user_id = r[1]
        if user_id not in user_reminders:
            user_reminders[user_id] = []
        user_reminders[user_id].append({
            'id': r[0],
            'name': r[2],
            'amount': r[3],
            'type': r[4],
            'frequency': r[5],
            'interval_days': r[6],
            'reminder_date': r[7],
            'next_notification': r[8]
        })

    # Отправляем уведомления и обновляем даты
    for user_id, reminders_list in user_reminders.items():
        lang = get_user_lang(user_id)

        for r in reminders_list:
            typ_text = get_text(user_id, 'reminder_expense_btn') if r['type'] == 'expense' else get_text(user_id,
                                                                                                         'reminder_income_btn')
            text = get_text(user_id, 'reminder_daily_title')
            text += get_text(user_id, 'reminder_daily_item', r['name'], r['amount'], typ_text)
            text += "💡 Чтобы добавить транзакцию, используйте кнопку '➕ Добавить'"

            try:
                await context.bot.send_message(chat_id=user_id, text=text)

                # Обновляем дату следующего напоминания
                if r['frequency'] != 'once':
                    next_date = calculate_next_date(r['next_notification'], r['frequency'], r['interval_days'])
                    update_notification(r['id'], next_date)
                else:
                    # Для одноразовых - деактивируем
                    deactivate_reminder(user_id, r['id'])

            except Exception as e:
                logging.error(f"Failed to send reminder to user {user_id}: {e}")


# ---------- Функции для графиков ----------
def get_chart_data_by_month(user_id, year, month):
    """Получает данные для графика за указанный месяц"""
    start_date = datetime(year, month, 1)
    if month == 12:
        end_date = datetime(year + 1, 1, 1) - timedelta(days=1)
    else:
        end_date = datetime(year, month + 1, 1) - timedelta(days=1)

    start = start_date.strftime('%Y-%m-%d')
    end = end_date.strftime('%Y-%m-%d')

    with get_db() as conn:
        c = conn.cursor()
        # Доходы по категориям
        c.execute('''SELECT category, COALESCE(SUM(amount),0) 
                     FROM transactions 
                     WHERE user_id=? AND type='income' AND date BETWEEN ? AND ? 
                     GROUP BY category''', (user_id, start, end))
        income_data = {row[0]: float(row[1]) for row in c.fetchall()}

        # Расходы по категориям
        c.execute('''SELECT category, COALESCE(SUM(amount),0) 
                     FROM transactions 
                     WHERE user_id=? AND type='expense' AND date BETWEEN ? AND ? 
                     GROUP BY category''', (user_id, start, end))
        expense_data = {row[0]: float(row[1]) for row in c.fetchall()}

        # Динамика по дням
        c.execute('''SELECT date, type, COALESCE(SUM(amount),0)
                     FROM transactions
                     WHERE user_id=? AND date BETWEEN ? AND ?
                     GROUP BY date, type''', (user_id, start, end))
        daily = {}
        for date, typ, amt in c.fetchall():
            if date not in daily:
                daily[date] = {'income': 0, 'expense': 0}
            daily[date][typ] += float(amt)

        return income_data, expense_data, daily, start_date, end_date


def get_chart_data_by_year(user_id, year):
    """Получает данные для графика за указанный год"""
    start_date = datetime(year, 1, 1)
    end_date = datetime(year, 12, 31)

    start = start_date.strftime('%Y-%m-%d')
    end = end_date.strftime('%Y-%m-%d')

    with get_db() as conn:
        c = conn.cursor()
        # Доходы по месяцам
        c.execute('''SELECT strftime('%m', date) as month, COALESCE(SUM(amount),0)
                     FROM transactions
                     WHERE user_id=? AND type='income' AND date BETWEEN ? AND ?
                     GROUP BY month''', (user_id, start, end))
        income_by_month = {int(row[0]): float(row[1]) for row in c.fetchall()}

        # Расходы по месяцам
        c.execute('''SELECT strftime('%m', date) as month, COALESCE(SUM(amount),0)
                     FROM transactions
                     WHERE user_id=? AND type='expense' AND date BETWEEN ? AND ?
                     GROUP BY month''', (user_id, start, end))
        expense_by_month = {int(row[0]): float(row[1]) for row in c.fetchall()}

        # Доходы по категориям (годовые)
        c.execute('''SELECT category, COALESCE(SUM(amount),0)
                     FROM transactions
                     WHERE user_id=? AND type='income' AND date BETWEEN ? AND ?
                     GROUP BY category''', (user_id, start, end))
        income_by_cat = {row[0]: float(row[1]) for row in c.fetchall() if row[1] > 0}

        # Расходы по категориям (годовые)
        c.execute('''SELECT category, COALESCE(SUM(amount),0)
                     FROM transactions
                     WHERE user_id=? AND type='expense' AND date BETWEEN ? AND ?
                     GROUP BY category''', (user_id, start, end))
        expense_by_cat = {row[0]: float(row[1]) for row in c.fetchall() if row[1] > 0}

        return income_by_month, expense_by_month, income_by_cat, expense_by_cat, start_date, end_date


def create_pie_chart(data, title, colors=None):
    """Создает круговую диаграмму с разными цветами для каждого сектора"""
    if not data:
        return None

    fig, ax = plt.subplots(figsize=(10, 8))
    labels = list(data.keys())
    values = list(data.values())
    colors = generate_colors(len(labels))

    # Если цвета не указаны, создаем массив разных цветов
    if colors is None:
        # Используем colormap 'tab20' для 20 разных цветов
        colors = []
        for i in range(len(labels)):
            if i < 10:
                colors.append(plt.cm.tab10(i))
            else:
                colors.append(plt.cm.Set3(i % 12))
    elif hasattr(colors, '__call__') or (hasattr(colors, 'N') and not isinstance(colors, list)):
        # Если colors - это colormap, а не список цветов
        # Создаем список цветов из colormap
        colors = [colors(i / len(labels)) for i in range(len(labels))]
    elif isinstance(colors, list) and len(colors) > 0 and hasattr(colors[0], '__call__'):
        # Если это список colormap функций
        colors = [c(i / len(labels)) if hasattr(c, '__call__') else c for i, c in enumerate(colors)]

    # Создаем диаграмму с разными цветами
    wedges, texts, autotexts = ax.pie(
        values,
        labels=labels,
        autopct='%1.1f%%',
        colors=colors,
        startangle=90,
        textprops={'fontsize': 12}
    )

    # Настройка внешнего вида текста
    for text in texts:
        text.set_fontsize(11)
        text.set_weight('bold')

    for autotext in autotexts:
        autotext.set_color('white')
        autotext.set_fontsize(10)
        autotext.set_weight('bold')

    ax.set_title(title, fontsize=16, fontweight='bold', pad=20)
    ax.axis('equal')

    # Добавляем легенду для лучшего понимания
    ax.legend(wedges, labels, title="Категории", loc="center left",
              bbox_to_anchor=(1, 0, 0.5, 1), fontsize=10)

    plt.tight_layout()
    return fig


def create_bar_chart(daily_data, title):
    """Создает столбчатую диаграмму дневной динамики"""
    if not daily_data:
        return None

    dates = sorted(daily_data.keys())
    incomes = [daily_data[d]['income'] for d in dates]
    expenses = [daily_data[d]['expense'] for d in dates]

    fig, ax = plt.subplots(figsize=(12, 6))
    x = range(len(dates))
    width = 0.35

    ax.bar([i - width / 2 for i in x], incomes, width, label='Доходы', color='green', alpha=0.7)
    ax.bar([i + width / 2 for i in x], expenses, width, label='Расходы', color='red', alpha=0.7)

    ax.set_xlabel('Дата', fontsize=12)
    ax.set_ylabel('Сумма (₽)', fontsize=12)
    ax.set_title(title, fontsize=16, fontweight='bold')
    ax.set_xticks(x)
    ax.set_xticklabels([d[5:] for d in dates], rotation=45)
    ax.legend()
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    return fig


def create_monthly_trend_chart(income_by_month, expense_by_month, year):
    """Создает график тренда по месяцам"""
    months = list(range(1, 13))
    incomes = [income_by_month.get(m, 0) for m in months]
    expenses = [expense_by_month.get(m, 0) for m in months]

    fig, ax = plt.subplots(figsize=(12, 6))
    x = months

    ax.plot(x, incomes, marker='o', label='Доходы', color='green', linewidth=2, markersize=8)
    ax.plot(x, expenses, marker='s', label='Расходы', color='red', linewidth=2, markersize=8)

    ax.set_xlabel('Месяц', fontsize=12)
    ax.set_ylabel('Сумма (₽)', fontsize=12)
    ax.set_title(f'Динамика доходов и расходов за {year} год', fontsize=16, fontweight='bold')
    ax.set_xticks(months)
    ax.set_xticklabels(['Янв', 'Фев', 'Мар', 'Апр', 'Май', 'Июн', 'Июл', 'Авг', 'Сен', 'Окт', 'Ноя', 'Дек'])
    ax.legend()
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    return fig

    # ----- Графики -----


async def charts_menu(update, context):
    uid = update.effective_user.id
    kb = ReplyKeyboardMarkup([
        [get_text(uid, 'charts_month'), get_text(uid, 'charts_year')],
        [get_text(uid, 'back_btn')]
    ], resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text(get_text(uid, 'charts_menu'), reply_markup=kb)
    return CHARTS_MENU


async def charts_choice(update, context):
    uid = update.effective_user.id
    choice = update.message.text

    if choice == get_text(uid, 'back_btn'):
        await update.message.reply_text(get_text(uid, 'cancel'), reply_markup=main_keyboard(uid))
        return ConversationHandler.END
    elif choice == get_text(uid, 'charts_month'):
        await update.message.reply_text(get_text(uid, 'charts_select_month'), reply_markup=ReplyKeyboardRemove())
        return CHARTS_MONTH_YEAR
    elif choice == get_text(uid, 'charts_year'):
        await update.message.reply_text(get_text(uid, 'charts_select_year'), reply_markup=ReplyKeyboardRemove())
        return CHARTS_YEAR
    else:
        await update.message.reply_text(get_text(uid, 'charts_menu'))
        return CHARTS_MENU


async def charts_month_year(update, context):
    uid = update.effective_user.id
    try:
        month = int(update.message.text.strip())
        if month < 1 or month > 12:
            await update.message.reply_text(get_text(uid, 'charts_invalid_month'))
            return CHARTS_MONTH_YEAR
        context.user_data['chart_month'] = month
        await update.message.reply_text(get_text(uid, 'charts_select_year'))
        return CHARTS_YEAR
    except:
        await update.message.reply_text(get_text(uid, 'charts_invalid_month'))
        return CHARTS_MONTH_YEAR


def generate_colors(n):
    base_colors = list(plt.cm.tab20.colors)

    if n <= len(base_colors):
        return base_colors[:n]

    # Если категорий больше 20
    colors = []
    for i in range(n):
        colors.append(base_colors[i % len(base_colors)])

    return colors


async def charts_generate(update, context):
    uid = update.effective_user.id

    # Отправляем сообщение о начале генерации
    loading_msg = await update.message.reply_text(get_text(uid, 'charts_loading'))

    try:
        if 'chart_month' in context.user_data:
            # Генерация графика за месяц
            year = int(update.message.text.strip())
            month = context.user_data['chart_month']

            if year < 2000 or year > datetime.now().year + 1:
                await update.message.reply_text(get_text(uid, 'charts_invalid_year'))
                return ConversationHandler.END

            income_data, expense_data, daily_data, start_date, end_date = get_chart_data_by_month(uid, year, month)

            if not income_data and not expense_data:
                await update.message.reply_text(get_text(uid, 'charts_error'), reply_markup=main_keyboard(uid))
                return ConversationHandler.END

            month_name_ru = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
                             'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь'][month - 1]
            title = f"{month_name_ru} {year}"

            # Создаем графики
            charts_to_send = []

            # Круговая диаграмма доходов
            if income_data:
                fig_income = create_pie_chart(income_data, f'Доходы по категориям\n{title}')
                if fig_income:
                    charts_to_send.append(('Доходы', fig_income))

            # Круговая диаграмма расходов
            if expense_data:
                fig_expense = create_pie_chart(expense_data, f'Расходы по категориям\n{title}')
                if fig_expense:
                    charts_to_send.append(('Расходы', fig_expense))

            # Столбчатая диаграмма по дням
            if daily_data:
                fig_daily = create_bar_chart(daily_data, f'Дневная динамика\n{title}')
                if fig_daily:
                    charts_to_send.append(('Динамика', fig_daily))

            # Отправляем графики
            if charts_to_send:
                await loading_msg.delete()
                for name, fig in charts_to_send:
                    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
                        fig.savefig(tmp.name, format='png', dpi=100, bbox_inches='tight')
                        tmp_path = tmp.name

                    with open(tmp_path, 'rb') as photo:
                        await update.message.reply_photo(photo=photo, caption=f"📊 {name}\n{title}")

                    os.unlink(tmp_path)
                    plt.close(fig)
            else:
                await loading_msg.edit_text(get_text(uid, 'charts_error'))

            # Очищаем данные
            del context.user_data['chart_month']

        else:
            # Генерация графика за год
            year = int(update.message.text.strip())

            if year < 2000 or year > datetime.now().year + 1:
                await update.message.reply_text(get_text(uid, 'charts_invalid_year'))
                return ConversationHandler.END

            income_by_month, expense_by_month, income_by_cat, expense_by_cat, start_date, end_date = get_chart_data_by_year(
                uid, year)

            if not income_by_month and not expense_by_month:
                await update.message.reply_text(get_text(uid, 'charts_error'), reply_markup=main_keyboard(uid))
                return ConversationHandler.END

            # Создаем графики
            charts_to_send = []

            # Тренд по месяцам
            if income_by_month or expense_by_month:
                fig_trend = create_monthly_trend_chart(income_by_month, expense_by_month, year)
                if fig_trend:
                    charts_to_send.append(('Тренд', fig_trend))

            # Круговая диаграмма доходов за год
            if income_by_cat:
                fig_income_year = create_pie_chart(income_by_cat, f'Доходы по категориям\n{year} год',
                                                   plt.cm.Greens(range(len(income_by_cat))))
                if fig_income_year:
                    charts_to_send.append(('Доходы за год', fig_income_year))

            # Круговая диаграмма расходов за год
            if expense_by_cat:
                fig_expense_year = create_pie_chart(expense_by_cat, f'Расходы по категориям\n{year} год',
                                                    plt.cm.Reds(range(len(expense_by_cat))))
                if fig_expense_year:
                    charts_to_send.append(('Расходы за год', fig_expense_year))

            # Отправляем графики
            if charts_to_send:
                await loading_msg.delete()
                for name, fig in charts_to_send:
                    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
                        fig.savefig(tmp.name, format='png', dpi=100, bbox_inches='tight')
                        tmp_path = tmp.name

                    with open(tmp_path, 'rb') as photo:
                        await update.message.reply_photo(photo=photo, caption=f"📊 {name}\n{year} год")

                    os.unlink(tmp_path)
                    plt.close(fig)
            else:
                await loading_msg.edit_text(get_text(uid, 'charts_error'))

        await update.message.reply_text("✅ Графики готовы!", reply_markup=main_keyboard(uid))
        return ConversationHandler.END

    except Exception as e:
        logging.error(f"Error generating chart: {e}")
        await loading_msg.edit_text("❌ Ошибка при генерации графика. Попробуйте позже.")
        return ConversationHandler.END


# ---------- Тексты ----------
TEXTS = {
    'ru': {
        'start': "Привет, {}!\nБот для учёта финансов.",
        'help': "➕ Добавить – доход/расход\n💰 Баланс\n📋 История\n📊 Отчёт\n✏️ Редактировать\n🗑️ Удалить\n📂 Категории и бюджеты\n📊 Графики\n⏰ Напоминания\n🌐 Язык",
        'balance': "💰 Баланс: {:.2f} ₽",
        'history_empty': "История пуста.",
        'cancel': "Отменено.",
        'add_amount_prompt': "Введите сумму (и комментарий через пробел):\n0 - отмена",
        'add_amount_invalid': "Введите число >0 или 0.",
        'type_prompt': "Выберите тип:",
        'income_btn': "💰 Доход",
        'expense_btn': "💸 Расход",
        'category_prompt': "Выберите категорию (или 'Пропустить' для 'Общей'):",
        'skip_btn': "⏩ Пропустить",
        'add_success': "✅ Добавлен {}\nСумма: {:.2f}\nКатегория: {}\nОписание: {}\nID: {}",
        'budget_warning': "⚠️ Превышен бюджет '{}'! {:.2f} / {:.2f}",
        'report_interval': "Интервал отчёта:",
        'day_btn': "День",
        'month_btn': "Месяц",
        'year_btn': "Год",
        'enter_day': "Введите день месяца (1-31):",
        'invalid_day': "Неверный день.",
        'export_prompt': "Формат выгрузки:",
        'export_success': "Файл готов.",
        'export_error': "Ошибка экспорта.",
        'edit_id': "ID транзакции (0-отмена):",
        'not_found': "Не найдено.",
        'edit_info': "ID {}: {} {:.2f} {}\n{}",
        'edit_type_btn': "🔄 Тип",
        'edit_cat_btn': "📂 Категория",
        'edit_amount_btn': "💰 Сумма",
        'edit_desc_btn': "✏️ Коммент",
        'edit_delete_btn': "🗑️ Удалить",
        'edit_back_btn': "🔙 Назад",
        'type_changed': "Тип изменён на {}.",
        'new_cat_prompt': "Новая категория:",
        'new_amount_prompt': "Новая сумма:",
        'new_desc_prompt': "Новый комментарий (/skip):",
        'edit_success': "✅ Изменено.",
        'confirm_delete': "Введите 'да' для удаления:",
        'delete_success': "✅ Удалено.",
        'cat_menu': "Управление категориями и бюджетами:",
        'cat_create': "➕ Создать",
        'cat_delete': "❌ Удалить",
        'cat_rename': "✏️ Переименовать",
        'budget_btn': "📉 Бюджет",
        'back_btn': "🔙 Назад",
        'cat_name_prompt': "Название категории (0-отмена):",
        'cat_type_prompt': "Тип категории:",
        'cat_created': "✅ Создана '{}'.",
        'cat_exists': "⚠️ Уже существует.",
        'cat_choose_del': "Выберите категорию для удаления:",
        'cat_confirm_del': "Удалить '{}' и все транзакции? Введите 'да':",
        'cat_deleted': "✅ Удалена '{}'.",
        'cat_choose_rename': "Выберите категорию для переименования:",
        'cat_new_name': "Новое название (0-отмена):",
        'cat_renamed': "✅ Переименована в '{}'.",
        'budget_cat_choose': "Выберите категорию расходов:",
        'budget_period': "Период:",
        'current_month': "Текущий месяц",
        'next_month': "Следующий месяц",
        'whole_year': "Весь год",
        'budget_amount_prompt': "Сумма бюджета:",
        'budget_set': "✅ Бюджет '{}' = {:.2f}",
        'charts_btn': "📊 Графики",
        'charts_menu': "Выберите период для графика:",
        'charts_month': "📅 Месяц",
        'charts_year': "📆 Год",
        'charts_select_month': "Выберите месяц (1-12):",
        'charts_select_year': "Введите год (например, 2024):",
        'charts_invalid_month': "Введите число от 1 до 12",
        'charts_invalid_year': "Введите корректный год (например, 2024)",
        'charts_loading': "⏳ Генерирую график...",
        'charts_error': "❌ Нет данных за указанный период",
        'reminder_btn': "⏰ Напоминания",
        'reminder_menu': "Управление напоминаниями:",
        'reminder_add': "➕ Новое напоминание",
        'reminder_list': "📋 Мои напоминания",
        'reminder_delete': "❌ Удалить напоминание",
        'reminder_add_name': "Введите название напоминания (например, 'Квартплата'):\n0 - отмена",
        'reminder_add_amount': "Введите сумму:\n0 - отмена",
        'reminder_add_date': "Введите дату первого напоминания (ДД.ММ.ГГГГ) или 'сегодня':\n0 - отмена",
        'reminder_add_frequency': "Выберите периодичность напоминания:",
        'reminder_once': "🔹 Одноразовое",
        'reminder_recurring': "🔁 Многоразовое",
        'reminder_interval': "Выберите интервал:",
        'reminder_daily': "📅 Каждый день",
        'reminder_weekly': "📆 Каждую неделю",
        'reminder_monthly': "📅 Каждый месяц",
        'reminder_yearly': "📆 Каждый год",
        'reminder_custom_days': "Введите количество дней (для ежедневного) или недель (для еженедельного):",
        'reminder_add_success': "✅ Напоминание добавлено!\nНазвание: {}\nСумма: {:.2f}\nДата: {}\nПериодичность: {}",
        'reminder_empty': "📭 У вас нет активных напоминаний",
        'reminder_list_header': "📋 Ваши напоминания:\n\n",
        'reminder_list_item': "ID:{} • {} • {:.2f} ₽ • Дата: {} • {} • {}\n",
        'reminder_delete_prompt': "Введите ID напоминания для удаления (0-отмена):",
        'reminder_not_found': "❌ Напоминание не найдено",
        'reminder_deleted': "✅ Напоминание удалено",
        'reminder_daily_title': "⏰ Напоминание о платеже!\n\n",
        'reminder_daily_item': "📌 {}\n💰 Сумма: {:.2f} ₽\n📅 Тип: {}\n\n",
        'reminder_frequency_text': {
            'once': 'одноразовое',
            'daily': 'ежедневно',
            'weekly': 'еженедельно',
            'monthly': 'ежемесячно',
            'yearly': 'ежегодно'
        },

        'lang_changed': "Язык: русский."
    },
    'en': {
        'start': "Hello {}!\nFinance bot.",
        'help': "➕ Add\n💰 Balance\n📋 History\n📊 Report\n✏️ Edit\n🗑️ Delete\n📂 Categories & budgets\n📊 Charts\n⏰ Reminders\n🌐 Language",
        'balance': "💰 Balance: {:.2f} ₽",
        'history_empty': "Empty.",
        'cancel': "Cancelled.",
        'add_amount_prompt': "Amount (+ comment):\n0 - cancel",
        'add_amount_invalid': "Invalid.",
        'type_prompt': "Select type:",
        'income_btn': "💰 Income",
        'expense_btn': "💸 Expense",
        'category_prompt': "Select category (or 'Skip' for 'General'):",
        'skip_btn': "⏩ Skip",
        'add_success': "✅ {} added\n{:.2f}\n{}\n{}\nID: {}",
        'budget_warning': "⚠️ Budget exceeded '{}'! {:.2f} / {:.2f}",
        'report_interval': "Report interval:",
        'day_btn': "Day",
        'month_btn': "Month",
        'year_btn': "Year",
        'enter_day': "Enter day (1-31):",
        'invalid_day': "Invalid day.",
        'export_prompt': "Export format:",
        'export_success': "File ready.",
        'export_error': "Export error.",
        'edit_id': "Transaction ID (0-cancel):",
        'not_found': "Not found.",
        'edit_info': "ID {}: {} {:.2f} {}\n{}",
        'edit_type_btn': "🔄 Type",
        'edit_cat_btn': "📂 Category",
        'edit_amount_btn': "💰 Amount",
        'edit_desc_btn': "✏️ Comment",
        'edit_delete_btn': "🗑️ Delete",
        'edit_back_btn': "🔙 Back",
        'type_changed': "Type changed to {}.",
        'new_cat_prompt': "New category:",
        'new_amount_prompt': "New amount:",
        'new_desc_prompt': "New comment (/skip):",
        'edit_success': "✅ Updated.",
        'confirm_delete': "Type 'yes' to delete:",
        'delete_success': "✅ Deleted.",
        'cat_menu': "Manage categories and budgets:",
        'cat_create': "➕ Create",
        'cat_delete': "❌ Delete",
        'cat_rename': "✏️ Rename",
        'budget_btn': "📉 Budget",
        'back_btn': "🔙 Back",
        'cat_name_prompt': "Category name (0-cancel):",
        'cat_type_prompt': "Category type:",
        'cat_created': "✅ Created '{}'.",
        'cat_exists': "⚠️ Exists.",
        'cat_choose_del': "Choose category to delete:",
        'cat_confirm_del': "Delete '{}' and all transactions? Type 'yes':",
        'cat_deleted': "✅ Deleted '{}'.",
        'cat_choose_rename': "Choose category to rename:",
        'cat_new_name': "New name (0-cancel):",
        'cat_renamed': "✅ Renamed to '{}'.",
        'budget_cat_choose': "Choose expense category:",
        'budget_period': "Period:",
        'current_month': "Current month",
        'next_month': "Next month",
        'whole_year': "Whole year",
        'budget_amount_prompt': "Budget amount:",
        'budget_set': "✅ Budget '{}' = {:.2f}",
        'charts_btn': "📊 Charts",
        'charts_menu': "Select period for chart:",
        'charts_month': "📅 Month",
        'charts_year': "📆 Year",
        'charts_select_month': "Enter month (1-12):",
        'charts_select_year': "Enter year (e.g., 2024):",
        'charts_invalid_month': "Enter number from 1 to 12",
        'charts_invalid_year': "Enter valid year (e.g., 2024)",
        'charts_loading': "⏳ Generating chart...",
        'charts_error': "❌ No data for selected period",
        'reminder_btn': "⏰ Reminders",
        'reminder_menu': "Manage reminders:",
        'reminder_add': "➕ New reminder",
        'reminder_list': "📋 My reminders",
        'reminder_delete': "❌ Delete reminder",
        'reminder_add_name': "Enter reminder name (e.g., 'Rent'):\n0 - cancel",
        'reminder_add_amount': "Enter amount:\n0 - cancel",
        'reminder_add_date': "Enter first reminder date (DD.MM.YYYY) or 'today':\n0 - cancel",
        'reminder_add_frequency': "Select reminder frequency:",
        'reminder_once': "🔹 One-time",
        'reminder_recurring': "🔁 Recurring",
        'reminder_interval': "Select interval:",
        'reminder_daily': "📅 Every day",
        'reminder_weekly': "📆 Every week",
        'reminder_monthly': "📅 Every month",
        'reminder_yearly': "📆 Every year",
        'reminder_custom_days': "Enter number of days (for daily) or weeks (for weekly):",
        'reminder_add_type': "Select payment type:",
        'reminder_expense_btn': "💸 Expense",
        'reminder_income_btn': "💰 Income",
        'reminder_add_success': "✅ Reminder added!\nName: {}\nAmount: {:.2f}\nDate: {}\nFrequency: {}\nType: {}",
        'reminder_empty': "📭 You have no active reminders",
        'reminder_list_header': "📋 Your reminders:\n\n",
        'reminder_list_item': "ID:{} • {} • {:.2f} ₽ • Date: {} • {} • {}\n",
        'reminder_delete_prompt': "Enter reminder ID to delete (0-cancel):",
        'reminder_not_found': "❌ Reminder not found",
        'reminder_deleted': "✅ Reminder deleted",
        'reminder_daily_title': "⏰ Payment reminder!\n\n",
        'reminder_daily_item': "📌 {}\n💰 Amount: {:.2f} ₽\n📅 Type: {}\n\n",
        'reminder_frequency_text': {
            'once': 'one-time',
            'daily': 'daily',
            'weekly': 'weekly',
            'monthly': 'monthly',
            'yearly': 'yearly'
        },

        'lang_changed': "Language: English."
    }
}


# ---------- Функции для календаря ----------
def create_calendar(year, month):
    """Создает inline клавиатуру-календарь"""
    keyboard = []

    # Заголовок с месяцем и годом
    month_names = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
                   'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь']
    keyboard.append([InlineKeyboardButton(f"{month_names[month - 1]} {year}", callback_data="ignore")])

    # Дни недели
    week_days = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
    keyboard.append([InlineKeyboardButton(day, callback_data="ignore") for day in week_days])

    # Получаем первый день месяца и количество дней
    first_day = datetime(year, month, 1)
    start_weekday = first_day.weekday()  # 0 = понедельник

    if month == 12:
        next_month = datetime(year + 1, 1, 1)
    else:
        next_month = datetime(year, month + 1, 1)
    days_in_month = (next_month - timedelta(days=1)).day

    # Заполняем календарь
    week = []
    # Пустые ячейки для первого дня
    for _ in range(start_weekday):
        week.append(InlineKeyboardButton(" ", callback_data="ignore"))

    for day in range(1, days_in_month + 1):
        week.append(InlineKeyboardButton(str(day), callback_data=f"date_{year}_{month}_{day}"))
        if len(week) == 7:
            keyboard.append(week)
            week = []

    # Добавляем оставшиеся пустые ячейки
    if week:
        while len(week) < 7:
            week.append(InlineKeyboardButton(" ", callback_data="ignore"))
        keyboard.append(week)

    # Кнопки навигации
    keyboard.append([
        InlineKeyboardButton("◀️", callback_data=f"prev_{year}_{month}"),
        InlineKeyboardButton("Сегодня", callback_data="today"),
        InlineKeyboardButton("▶️", callback_data=f"next_{year}_{month}")
    ])

    return InlineKeyboardMarkup(keyboard)


async def show_calendar(update, context, year=None, month=None):
    """Показывает календарь пользователю"""
    if year is None or month is None:
        now = datetime.now()
        year = now.year
        month = now.month

    context.user_data['calendar_year'] = year
    context.user_data['calendar_month'] = month

    calendar = create_calendar(year, month)
    await update.message.reply_text("📅 Выберите дату напоминания:", reply_markup=calendar)


async def calendar_callback(update, context):
    """Обработчик нажатий на календарь"""
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "ignore":
        return

    if data == "today":
        # Выбрана сегодняшняя дата
        today = datetime.now()
        selected_date = today.strftime('%Y-%m-%d')
        context.user_data['reminder_date'] = selected_date

        # Переходим к выбору периодичности
        buttons = [[get_text(query.from_user.id, 'reminder_once'), get_text(query.from_user.id, 'reminder_recurring')]]
        await query.edit_message_text(get_text(query.from_user.id, 'reminder_add_frequency'),
                                      reply_markup=ReplyKeyboardMarkup(buttons, resize_keyboard=True,
                                                                       one_time_keyboard=True))
        return REMINDER_ADD_FREQUENCY

    if data.startswith("prev_"):
        _, year, month = data.split("_")
        year = int(year)
        month = int(month)
        if month == 1:
            month = 12
            year -= 1
        else:
            month -= 1
        calendar = create_calendar(year, month)
        await query.edit_message_text("📅 Выберите дату напоминания:", reply_markup=calendar)
        return

    if data.startswith("next_"):
        _, year, month = data.split("_")
        year = int(year)
        month = int(month)
        if month == 12:
            month = 1
            year += 1
        else:
            month += 1
        calendar = create_calendar(year, month)
        await query.edit_message_text("📅 Выберите дату напоминания:", reply_markup=calendar)
        return

    if data.startswith("date_"):
        _, year, month, day = data.split("_")
        selected_date = f"{year}-{month.zfill(2)}-{day.zfill(2)}"
        context.user_data['reminder_date'] = selected_date

        # Переходим к выбору периодичности
        buttons = [[get_text(query.from_user.id, 'reminder_once'), get_text(query.from_user.id, 'reminder_recurring')]]
        await query.edit_message_text(get_text(query.from_user.id, 'reminder_add_frequency'),
                                      reply_markup=ReplyKeyboardMarkup(buttons, resize_keyboard=True,
                                                                       one_time_keyboard=True))
        return REMINDER_ADD_FREQUENCY


def get_text(user_id, key, *args, **kwargs):
    lang = get_user_lang(user_id)
    text = TEXTS[lang].get(key, TEXTS['ru'][key])
    if args or kwargs:
        return text.format(*args, **kwargs)
    return text


def main_keyboard(user_id):
    lang = get_user_lang(user_id)
    if lang == 'ru':
        return ReplyKeyboardMarkup([
            ["➕ Добавить", "💰 Баланс"],
            ["📋 История", "📊 Отчёт"],
            ["✏️ Редактировать", "🗑️ Удалить"],
            ["📂 Категории и бюджеты", "📊 Графики"],
            ["⏰ Напоминания", "🌐 Язык / Language"]
        ], resize_keyboard=True)
    else:
        return ReplyKeyboardMarkup([
            ["➕ Add", "💰 Balance"],
            ["📋 History", "📊 Report"],
            ["✏️ Edit", "🗑️ Delete"],
            ["📂 Categories & budgets", "📊 Charts"],
            ["⏰ Reminders", "🌐 Язык / Language"]
        ], resize_keyboard=True)


# ---------- Функции БД ----------
def add_transaction(user_id, amount, typ, cat, desc):
    date = datetime.now().strftime('%Y-%m-%d')
    with get_db() as conn:
        c = conn.cursor()
        c.execute('INSERT INTO transactions (user_id, amount, type, category, description, date) VALUES (?,?,?,?,?,?)',
                  (user_id, float(amount), typ, cat, desc, date))
        return c.lastrowid


def get_balance(user_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT SUM(CASE WHEN type="income" THEN amount ELSE -amount END) FROM transactions WHERE user_id=?',
                  (user_id,))
        row = c.fetchone()
        return Decimal(row[0] or 0)


def get_history(user_id, limit=15):
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT id, amount, type, category, description, date FROM transactions WHERE user_id=? ORDER BY date DESC, id DESC LIMIT ?',
            (user_id, limit))
        return c.fetchall()


def get_transaction(user_id, trans_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT id, amount, type, category, description, date FROM transactions WHERE user_id=? AND id=?',
                  (user_id, trans_id))
        return c.fetchone()


def update_transaction_field(trans_id, field, value):
    with get_db() as conn:
        c = conn.cursor()
        c.execute(f'UPDATE transactions SET {field}=? WHERE id=?', (value, trans_id))
        conn.commit()


def delete_transaction_by_id(trans_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute('DELETE FROM transactions WHERE id=?', (trans_id,))
        conn.commit()


def get_user_categories(user_id, typ=None):
    with get_db() as conn:
        c = conn.cursor()
        if typ:
            c.execute('SELECT name FROM categories WHERE user_id=? AND type=?', (user_id, typ))
        else:
            c.execute('SELECT name FROM categories WHERE user_id=?', (user_id,))
        return [row[0] for row in c.fetchall()]


def add_user_category(user_id, name, typ):
    with get_db() as conn:
        c = conn.cursor()
        try:
            c.execute('INSERT INTO categories (user_id, name, type) VALUES (?,?,?)', (user_id, name, typ))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def delete_user_category(user_id, name):
    if name == 'Общая' or name == 'General':
        return False
    with get_db() as conn:
        c = conn.cursor()
        c.execute('DELETE FROM transactions WHERE user_id=? AND category=?', (user_id, name))
        c.execute('DELETE FROM categories WHERE user_id=? AND name=?', (user_id, name))
        c.execute('DELETE FROM budgets WHERE user_id=? AND category=?', (user_id, name))
        conn.commit()
        return True


def rename_user_category(user_id, old, new):
    if old == 'Общая' or old == 'General':
        return False
    with get_db() as conn:
        c = conn.cursor()
        c.execute('UPDATE categories SET name=? WHERE user_id=? AND name=?', (new, user_id, old))
        c.execute('UPDATE transactions SET category=? WHERE user_id=? AND category=?', (new, user_id, old))
        c.execute('UPDATE budgets SET category=? WHERE user_id=? AND category=?', (new, user_id, old))
        conn.commit()
        return True


def set_budget(user_id, cat, amount, month_str):
    with get_db() as conn:
        c = conn.cursor()
        c.execute('INSERT OR REPLACE INTO budgets (user_id, category, amount, month) VALUES (?,?,?,?)',
                  (user_id, cat, float(amount), month_str))
        conn.commit()


def get_budget(user_id, cat, month_str):
    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT amount FROM budgets WHERE user_id=? AND category=? AND month=?', (user_id, cat, month_str))
        row = c.fetchone()
        return Decimal(row[0]) if row else None


def get_spent_for_category(user_id, cat, month_str):
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT COALESCE(SUM(amount),0) FROM transactions WHERE user_id=? AND type="expense" AND category=? AND strftime("%Y-%m", date)=?',
            (user_id, cat, month_str))
        row = c.fetchone()
        return Decimal(row[0])


def get_monthly_budgets(user_id, month_str):
    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT category, amount FROM budgets WHERE user_id=? AND month=?', (user_id, month_str))
        return {row[0]: Decimal(row[1]) for row in c.fetchall()}


def get_report_data(user_id, start_date, end_date):
    start = start_date.strftime('%Y-%m-%d')
    end = end_date.strftime('%Y-%m-%d')
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT category, type, COALESCE(SUM(amount),0) FROM transactions WHERE user_id=? AND date BETWEEN ? AND ? GROUP BY category, type',
            (user_id, start, end))
        agg = {}
        for cat, typ, amt in c.fetchall():
            if cat not in agg:
                agg[cat] = {'income': Decimal('0'), 'expense': Decimal('0')}
            agg[cat][typ] = Decimal(amt)
        c.execute(
            'SELECT id, amount, type, category, description, date FROM transactions WHERE user_id=? AND date BETWEEN ? AND ? ORDER BY date, id',
            (user_id, start, end))
        transactions = c.fetchall()
        return agg, transactions


# ---------- Обработчики ----------
async def start(update, context):
    user = update.effective_user
    register_user(user.id, user.username, user.first_name)
    await update.message.reply_text(get_text(user.id, 'start', user.first_name), reply_markup=main_keyboard(user.id))


async def help_command(update, context):
    uid = update.effective_user.id
    await update.message.reply_text(get_text(uid, 'help'), reply_markup=main_keyboard(uid))


async def balance(update, context):
    uid = update.effective_user.id
    bal = get_balance(uid)
    await update.message.reply_text(get_text(uid, 'balance', bal), reply_markup=main_keyboard(uid))


async def history(update, context):
    uid = update.effective_user.id
    rows = get_history(uid)
    if not rows:
        await update.message.reply_text(get_text(uid, 'history_empty'), reply_markup=main_keyboard(uid))
        return
    text = "📋 *История:*\n"
    for r in rows:
        sign = '+' if r[2] == 'income' else '-'
        text += f"ID:{r[0]} {r[5]} {sign} {r[1]:.2f} | {r[3]}"
        if r[4]: text += f" | {r[4]}"
        text += "\n"
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=main_keyboard(uid))


# ----- Добавление -----
async def add_amount(update, context):
    uid = update.effective_user.id
    txt = update.message.text.strip()
    if txt == '0':
        await update.message.reply_text(get_text(uid, 'cancel'), reply_markup=main_keyboard(uid))
        return ConversationHandler.END
    parts = txt.split(maxsplit=1)
    try:
        amount = Decimal(parts[0].replace(',', '.'))
        if amount <= 0:
            raise ValueError
    except:
        await update.message.reply_text(get_text(uid, 'add_amount_invalid'))
        return ADD_AMOUNT
    desc = parts[1] if len(parts) > 1 else ''
    context.user_data['add_amount'] = amount
    context.user_data['add_desc'] = desc
    buttons = [[get_text(uid, 'income_btn'), get_text(uid, 'expense_btn')]]
    await update.message.reply_text(get_text(uid, 'type_prompt'),
                                    reply_markup=ReplyKeyboardMarkup(buttons, resize_keyboard=True,
                                                                     one_time_keyboard=True))
    return ADD_TYPE


async def add_type(update, context):
    uid = update.effective_user.id
    choice = update.message.text
    if choice == get_text(uid, 'income_btn'):
        context.user_data['add_type'] = 'income'
        cats = get_user_categories(uid, 'income')
    elif choice == get_text(uid, 'expense_btn'):
        context.user_data['add_type'] = 'expense'
        cats = get_user_categories(uid, 'expense')
    else:
        await update.message.reply_text(get_text(uid, 'type_prompt'))
        return ADD_TYPE
    buttons = [[cat] for cat in cats] + [[get_text(uid, 'skip_btn')]]
    await update.message.reply_text(get_text(uid, 'category_prompt'),
                                    reply_markup=ReplyKeyboardMarkup(buttons, resize_keyboard=True,
                                                                     one_time_keyboard=True))
    return ADD_CATEGORY


async def add_category(update, context):
    uid = update.effective_user.id
    cat = update.message.text
    if cat == get_text(uid, 'skip_btn'):
        cat = 'Общая'
    elif cat not in get_user_categories(uid, context.user_data['add_type']):
        await update.message.reply_text(get_text(uid, 'category_prompt'))
        return ADD_CATEGORY
    trans_id = add_transaction(uid, context.user_data['add_amount'], context.user_data['add_type'], cat,
                               context.user_data['add_desc'])
    typ_ru = "доход" if context.user_data['add_type'] == 'income' else "расход"
    await update.message.reply_text(
        get_text(uid, 'add_success', typ_ru, context.user_data['add_amount'], cat, context.user_data['add_desc'] or '—',
                 trans_id),
        reply_markup=main_keyboard(uid))
    if context.user_data['add_type'] == 'expense' and cat != 'Общая':
        month_str = datetime.now().strftime('%Y-%m')
        budget = get_budget(uid, cat, month_str)
        if budget:
            spent = get_spent_for_category(uid, cat, month_str)
            if spent > budget:
                await update.message.reply_text(get_text(uid, 'budget_warning', cat, spent, budget))
    del context.user_data['add_amount'], context.user_data['add_desc'], context.user_data['add_type']
    return ConversationHandler.END


# ----- Отчёты и экспорт (сокращённо, но рабоче) -----
async def report_interval(update, context):
    uid = update.effective_user.id
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(get_text(uid, 'day_btn'), callback_data='day')],
        [InlineKeyboardButton(get_text(uid, 'month_btn'), callback_data='month')],
        [InlineKeyboardButton(get_text(uid, 'year_btn'), callback_data='year')]
    ])
    await update.message.reply_text(get_text(uid, 'report_interval'), reply_markup=keyboard)
    return REPORT_INTERVAL


async def report_callback(update, context):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    interval = query.data
    if interval == 'day':
        await query.edit_message_text(get_text(uid, 'enter_day'))
        return REPORT_DAY
    elif interval == 'month':
        await generate_monthly_report(update, context, query)
        return ConversationHandler.END
    else:
        await generate_yearly_report(update, context, query)
        return ConversationHandler.END


async def report_day(update, context):
    uid = update.effective_user.id
    try:
        day = int(update.message.text)
        if 1 <= day <= 31:
            now = datetime.now()
            try:
                selected = now.replace(day=day)
                await generate_daily_report(update, context, selected)
                return ConversationHandler.END
            except:
                await update.message.reply_text(get_text(uid, 'invalid_day'))
                return REPORT_DAY
        else:
            raise ValueError
    except:
        await update.message.reply_text(get_text(uid, 'invalid_day'))
        return REPORT_DAY


async def generate_daily_report(update, context, date):
    uid = update.effective_user.id
    start = date.replace(hour=0, minute=0, second=0)
    end = date.replace(hour=23, minute=59, second=59)
    agg, transactions = get_report_data(uid, start, end)
    month_str = start.strftime('%Y-%m')
    text = f"📅 *{date.strftime('%d.%m.%Y')}*\n\n"
    total_income = sum(d['income'] for d in agg.values())
    total_expense = sum(d['expense'] for d in agg.values())
    gen_budget = get_budget(uid, 'Общая', month_str) or Decimal('0')
    remaining = gen_budget - total_expense
    text += f"*Общая:*\n  Бюджет: {gen_budget:.2f}\n  Доход: {total_income:.2f}\n  Расход: {total_expense:.2f}\n  Остаток: {remaining:.2f}\n\n"
    for cat, data in agg.items():
        if cat == 'Общая': continue
        budget = get_budget(uid, cat, month_str) or Decimal('0')
        spent = data['expense']
        text += f"*{cat}:*\n  Бюджет: {budget:.2f}\n  Доход: {data['income']:.2f}\n  Расход: {spent:.2f}\n  Остаток: {budget - spent:.2f}\n"
        for t in transactions:
            if t[3] == cat:
                text += f"    ID:{t[0]} {t[5][:5]} {'+' if t[2] == 'income' else '-'}{t[1]:.2f} | {t[4] or '—'}\n"
        text += "\n"
    context.user_data['report_data'] = (agg, transactions, start, end, 'day')
    buttons = [InlineKeyboardButton(fmt, callback_data=f'export_{fmt.lower()}') for fmt in
               ['TXT', 'CSV', 'XLSX', 'PDF']]
    await update.message.reply_text(text, parse_mode='Markdown')
    await update.message.reply_text(get_text(uid, 'export_prompt'), reply_markup=InlineKeyboardMarkup([buttons]))
    return EXPORT


async def generate_monthly_report(update, context, query=None):
    uid = update.effective_user.id
    now = datetime.now()
    start = now.replace(day=1)
    end = (start + timedelta(days=32)).replace(day=1) - timedelta(seconds=1)
    agg, transactions = get_report_data(uid, start, end)
    month_str = start.strftime('%Y-%m')
    budgets = get_monthly_budgets(uid, month_str)
    text = f"📊 *{start.strftime('%B %Y')}* ({start.day}.{start.month} - {end.day}.{end.month})\n"
    text += "| Категория | Расход | Доход | Бюджет | Остаток | % |\n|-----------|--------|-------|--------|---------|---|\n"
    all_cats = set(agg.keys()) | set(budgets.keys()) | {'Общая'}
    for cat in sorted(all_cats):
        inc = agg.get(cat, {}).get('income', 0)
        exp = agg.get(cat, {}).get('expense', 0)
        bud = budgets.get(cat, 0)
        rem = bud - exp
        pct = (exp / bud * 100) if bud > 0 else 0
        text += f"|{cat}|{exp:.2f}|{inc:.2f}|{bud:.2f}|{rem:.2f}|{pct:.1f}%\n"
    context.user_data['report_data'] = (agg, transactions, start, end, 'month')
    buttons = [InlineKeyboardButton(fmt, callback_data=f'export_{fmt.lower()}') for fmt in
               ['TXT', 'CSV', 'XLSX', 'PDF']]
    if query:
        await query.edit_message_text(text, parse_mode='Markdown')
        await query.message.reply_text(get_text(uid, 'export_prompt'), reply_markup=InlineKeyboardMarkup([buttons]))
    else:
        await update.message.reply_text(text, parse_mode='Markdown')
        await update.message.reply_text(get_text(uid, 'export_prompt'), reply_markup=InlineKeyboardMarkup([buttons]))


async def generate_yearly_report(update, context, query=None):
    uid = update.effective_user.id
    year = datetime.now().year
    text = f"📅 *{year}*\n"
    for m in range(1, 13):
        start = datetime(year, m, 1)
        end = (start + timedelta(days=32)).replace(day=1) - timedelta(seconds=1)
        agg, _ = get_report_data(uid, start, end)
        total_inc = sum(d['income'] for d in agg.values())
        total_exp = sum(d['expense'] for d in agg.values())
        text += f"{start.strftime('%B')}: +{total_inc:.2f} -{total_exp:.2f}\n"
    context.user_data['year_report_text'] = text
    if query:
        await query.edit_message_text(text, parse_mode='Markdown')
        await query.message.reply_text(get_text(uid, 'export_prompt'), reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("TXT", callback_data='export_yeartxt')]]))
    else:
        await update.message.reply_text(text, parse_mode='Markdown')
        await update.message.reply_text(get_text(uid, 'export_prompt'), reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("TXT", callback_data='export_yeartxt')]]))


async def export_callback(update, context):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    fmt = query.data.split('_')[1]
    if fmt == 'yeartxt':
        text = context.user_data.get('year_report_text', '')
        file = io.BytesIO(text.encode('utf-8'))
        await query.message.reply_document(document=file, filename=f"report_{datetime.now().year}.txt")
        return ConversationHandler.END
    agg, transactions, start, end, rtype = context.user_data.get('report_data', (None, None, None, None, None))
    if not agg:
        await query.message.reply_text(get_text(uid, 'export_error'))
        return ConversationHandler.END
    month_str = start.strftime('%Y-%m') if rtype != 'day' else start.strftime('%Y-%m')
    budgets = get_monthly_budgets(uid, month_str) if rtype != 'day' else {}
    rows1 = []
    all_cats = set(agg.keys()) | set(budgets.keys()) | {'Общая'}
    for cat in sorted(all_cats):
        inc = float(agg.get(cat, {}).get('income', 0))
        exp = float(agg.get(cat, {}).get('expense', 0))
        bud = float(budgets.get(cat, 0))
        rem = bud - exp
        pct = (exp / bud * 100) if bud > 0 else 0
        rows1.append([cat, exp, inc, bud, rem, pct])
    rows2 = []
    for t in transactions:
        rows2.append([t[5], t[3], t[2], float(t[1]), t[4] or ''])
    if fmt == 'txt':
        buf = io.StringIO()
        buf.write("=== Агрегированный отчёт ===\n")
        buf.write("Категория\tРасход\tДоход\tБюджет\tОстаток\t%\n")
        for r in rows1:
            buf.write("\t".join(str(x) for x in r) + "\n")
        buf.write("\n=== Детальные транзакции ===\n")
        buf.write("Дата\tКатегория\tТип\tСумма\tКомментарий\n")
        for r in rows2:
            buf.write("\t".join(str(x) for x in r) + "\n")
        data = buf.getvalue().encode('utf-8')
        filename = f"report_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}.txt"
    elif fmt == 'csv':
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Категория", "Расход", "Доход", "Бюджет", "Остаток", "%"])
        writer.writerows(rows1)
        writer.writerow([])
        writer.writerow(["Дата", "Категория", "Тип", "Сумма", "Комментарий"])
        writer.writerows(rows2)
        data = buf.getvalue().encode('utf-8-sig')
        filename = f"report_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}.csv"
    elif fmt == 'xlsx':
        try:
            import pandas as pd
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine='openpyxl') as writer:
                pd.DataFrame(rows1, columns=["Категория", "Расход", "Доход", "Бюджет", "Остаток", "%"]).to_excel(writer,
                                                                                                                 sheet_name="Агрегат",
                                                                                                                 index=False)
                pd.DataFrame(rows2, columns=["Дата", "Категория", "Тип", "Сумма", "Комментарий"]).to_excel(writer,
                                                                                                           sheet_name="Транзакции",
                                                                                                           index=False)
            data = buf.getvalue()
            filename = f"report_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}.xlsx"
        except ImportError:
            await query.message.reply_text("Установите pandas и openpyxl: pip install pandas openpyxl")
            return
    elif fmt == 'pdf':
        try:
            from fpdf import FPDF
            pdf = FPDF()
            pdf.add_page()
            pdf.set_font("Arial", size=10)
            pdf.cell(200, 10, "Aggregated Report", ln=1)
            for r in rows1:
                pdf.cell(200, 6, " | ".join(str(x) for x in r), ln=1)
            pdf.add_page()
            pdf.cell(200, 10, "Detailed Transactions", ln=1)
            for r in rows2:
                pdf.cell(200, 6, " | ".join(str(x) for x in r), ln=1)
            data = pdf.output(dest='S').encode('latin1')
            filename = f"report_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}.pdf"
        except ImportError:
            await query.message.reply_text("Установите fpdf: pip install fpdf")
            return
    else:
        await query.message.reply_text(get_text(uid, 'export_error'))
        return
    await query.message.reply_document(document=io.BytesIO(data), filename=filename)
    return ConversationHandler.END


# ----- Редактирование и удаление -----
async def edit_start(update, context):
    uid = update.effective_user.id
    await update.message.reply_text(get_text(uid, 'edit_id'), reply_markup=ReplyKeyboardRemove())
    return EDIT_ID


async def edit_get_id(update, context):
    uid = update.effective_user.id
    txt = update.message.text.strip()
    if txt == '0':
        await update.message.reply_text(get_text(uid, 'cancel'), reply_markup=main_keyboard(uid))
        return ConversationHandler.END
    try:
        trans_id = int(txt)
    except:
        await update.message.reply_text(get_text(uid, 'not_found'))
        return EDIT_ID
    trans = get_transaction(uid, trans_id)
    if not trans:
        await update.message.reply_text(get_text(uid, 'not_found'))
        return EDIT_ID
    context.user_data['edit_id'] = trans_id
    info = get_text(uid, 'edit_info', trans_id, 'доход' if trans[2] == 'income' else 'расход', trans[1], trans[3],
                    trans[4] or '—')
    kb = ReplyKeyboardMarkup([
        [get_text(uid, 'edit_type_btn'), get_text(uid, 'edit_cat_btn')],
        [get_text(uid, 'edit_amount_btn'), get_text(uid, 'edit_desc_btn')],
        [get_text(uid, 'edit_delete_btn'), get_text(uid, 'edit_back_btn')]
    ], resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text(info, reply_markup=kb)
    return EDIT_ACTION


async def edit_action(update, context):
    uid = update.effective_user.id
    choice = update.message.text
    trans_id = context.user_data['edit_id']
    trans = get_transaction(uid, trans_id)
    if not trans:
        await update.message.reply_text(get_text(uid, 'not_found'))
        return ConversationHandler.END
    if choice == get_text(uid, 'edit_type_btn'):
        new_type = 'expense' if trans[2] == 'income' else 'income'
        update_transaction_field(trans_id, 'type', new_type)
        await update.message.reply_text(get_text(uid, 'type_changed', new_type), reply_markup=main_keyboard(uid))
        return ConversationHandler.END
    elif choice == get_text(uid, 'edit_cat_btn'):
        cats = get_user_categories(uid, trans[2])
        kb = ReplyKeyboardMarkup([[c] for c in cats], resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text(get_text(uid, 'new_cat_prompt'), reply_markup=kb)
        return EDIT_NEW_VALUE
    elif choice == get_text(uid, 'edit_amount_btn'):
        await update.message.reply_text(get_text(uid, 'new_amount_prompt'), reply_markup=ReplyKeyboardRemove())
        return EDIT_NEW_VALUE
    elif choice == get_text(uid, 'edit_desc_btn'):
        await update.message.reply_text(get_text(uid, 'new_desc_prompt'), reply_markup=ReplyKeyboardRemove())
        return EDIT_NEW_VALUE
    elif choice == get_text(uid, 'edit_delete_btn'):
        await update.message.reply_text(get_text(uid, 'confirm_delete'))
        return DELETE_CONFIRM
    elif choice == get_text(uid, 'edit_back_btn'):
        await update.message.reply_text(get_text(uid, 'cancel'), reply_markup=main_keyboard(uid))
        return ConversationHandler.END
    else:
        await update.message.reply_text(get_text(uid, 'new_cat_prompt'))
        return EDIT_ACTION


async def edit_new_value(update, context):
    uid = update.effective_user.id
    trans_id = context.user_data['edit_id']
    trans = get_transaction(uid, trans_id)
    if not trans:
        await update.message.reply_text(get_text(uid, 'not_found'))
        return ConversationHandler.END
    if update.message.text in get_user_categories(uid, trans[2]):
        update_transaction_field(trans_id, 'category', update.message.text)
        await update.message.reply_text(get_text(uid, 'edit_success'), reply_markup=main_keyboard(uid))
        return ConversationHandler.END
    else:
        try:
            new_amt = Decimal(update.message.text.replace(',', '.'))
            if new_amt > 0:
                update_transaction_field(trans_id, 'amount', float(new_amt))
                await update.message.reply_text(get_text(uid, 'edit_success'), reply_markup=main_keyboard(uid))
                return ConversationHandler.END
        except:
            pass
        new_desc = update.message.text
        if new_desc == '/skip':
            new_desc = ''
        update_transaction_field(trans_id, 'description', new_desc)
        await update.message.reply_text(get_text(uid, 'edit_success'), reply_markup=main_keyboard(uid))
        return ConversationHandler.END


async def delete_confirm(update, context):
    uid = update.effective_user.id
    ans = update.message.text.lower()
    if ans == 'да' or ans == 'yes':
        delete_transaction_by_id(context.user_data['edit_id'])
        await update.message.reply_text(get_text(uid, 'delete_success'), reply_markup=main_keyboard(uid))
    else:
        await update.message.reply_text(get_text(uid, 'cancel'), reply_markup=main_keyboard(uid))
    return ConversationHandler.END


# ----- Категории и бюджеты -----
async def cat_budget_menu(update, context):
    uid = update.effective_user.id
    kb = ReplyKeyboardMarkup([
        [get_text(uid, 'cat_create'), get_text(uid, 'cat_delete')],
        [get_text(uid, 'cat_rename'), get_text(uid, 'budget_btn')],
        [get_text(uid, 'back_btn')]
    ], resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text(get_text(uid, 'cat_menu'), reply_markup=kb)
    return CAT_MENU


async def cat_budget_handler(update, context):
    uid = update.effective_user.id
    text = update.message.text
    if text == get_text(uid, 'back_btn'):
        await update.message.reply_text(get_text(uid, 'cancel'), reply_markup=main_keyboard(uid))
        return ConversationHandler.END
    elif text == get_text(uid, 'cat_create'):
        await update.message.reply_text(get_text(uid, 'cat_name_prompt'), reply_markup=ReplyKeyboardRemove())
        return CAT_CREATE_NAME
    elif text == get_text(uid, 'cat_delete'):
        cats = [c for c in get_user_categories(uid) if c != 'Общая']
        if not cats:
            await update.message.reply_text("Нет категорий для удаления.", reply_markup=main_keyboard(uid))
            return ConversationHandler.END
        kb = ReplyKeyboardMarkup([[c] for c in cats], resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text(get_text(uid, 'cat_choose_del'), reply_markup=kb)
        return CAT_DELETE_CHOOSE
    elif text == get_text(uid, 'cat_rename'):
        cats = [c for c in get_user_categories(uid) if c != 'Общая']
        if not cats:
            await update.message.reply_text("Нет категорий для переименования.", reply_markup=main_keyboard(uid))
            return ConversationHandler.END
        kb = ReplyKeyboardMarkup([[c] for c in cats], resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text(get_text(uid, 'cat_choose_rename'), reply_markup=kb)
        return CAT_RENAME_CHOOSE
    elif text == get_text(uid, 'budget_btn'):
        cats = get_user_categories(uid, 'expense')
        cats.append('Общая')
        kb = ReplyKeyboardMarkup([[c] for c in cats], resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text(get_text(uid, 'budget_cat_choose'), reply_markup=kb)
        return BUDGET_CAT
    else:
        await update.message.reply_text(get_text(uid, 'cat_menu'))
        return CAT_MENU


async def cat_create_name(update, context):
    uid = update.effective_user.id
    name = update.message.text.strip()
    if name == '0':
        await update.message.reply_text(get_text(uid, 'cancel'), reply_markup=main_keyboard(uid))
        return ConversationHandler.END
    context.user_data['new_cat'] = name
    kb = ReplyKeyboardMarkup([[get_text(uid, 'income_btn'), get_text(uid, 'expense_btn')]], resize_keyboard=True,
                             one_time_keyboard=True)
    await update.message.reply_text(get_text(uid, 'cat_type_prompt'), reply_markup=kb)
    return CAT_CREATE_TYPE


async def cat_create_type(update, context):
    uid = update.effective_user.id
    txt = update.message.text
    if txt == get_text(uid, 'income_btn'):
        typ = 'income'
    elif txt == get_text(uid, 'expense_btn'):
        typ = 'expense'
    else:
        await update.message.reply_text(get_text(uid, 'cat_type_prompt'))
        return CAT_CREATE_TYPE
    name = context.user_data['new_cat']
    if add_user_category(uid, name, typ):
        await update.message.reply_text(get_text(uid, 'cat_created', name), reply_markup=main_keyboard(uid))
    else:
        await update.message.reply_text(get_text(uid, 'cat_exists', name), reply_markup=main_keyboard(uid))
    return ConversationHandler.END


async def cat_delete_choose(update, context):
    uid = update.effective_user.id
    cat = update.message.text
    if cat not in get_user_categories(uid):
        await update.message.reply_text(get_text(uid, 'cat_choose_del'))
        return CAT_DELETE_CHOOSE
    context.user_data['del_cat'] = cat
    await update.message.reply_text(get_text(uid, 'cat_confirm_del', cat))
    return CAT_DELETE_CONFIRM


async def cat_delete_confirm(update, context):
    uid = update.effective_user.id
    ans = update.message.text.lower()
    if ans == 'да' or ans == 'yes':
        cat = context.user_data['del_cat']
        delete_user_category(uid, cat)
        await update.message.reply_text(get_text(uid, 'cat_deleted', cat), reply_markup=main_keyboard(uid))
    else:
        await update.message.reply_text(get_text(uid, 'cancel'), reply_markup=main_keyboard(uid))
    return ConversationHandler.END


async def cat_rename_choose(update, context):
    uid = update.effective_user.id
    cat = update.message.text
    if cat not in get_user_categories(uid) or cat == 'Общая':
        await update.message.reply_text(get_text(uid, 'cat_choose_rename'))
        return CAT_RENAME_CHOOSE
    context.user_data['rename_old'] = cat
    await update.message.reply_text(get_text(uid, 'cat_new_name'), reply_markup=ReplyKeyboardRemove())
    return CAT_RENAME_NEW


async def cat_rename_new(update, context):
    uid = update.effective_user.id
    new = update.message.text.strip()
    if new == '0':
        await update.message.reply_text(get_text(uid, 'cancel'), reply_markup=main_keyboard(uid))
        return ConversationHandler.END
    old = context.user_data['rename_old']
    if rename_user_category(uid, old, new):
        await update.message.reply_text(get_text(uid, 'cat_renamed', new), reply_markup=main_keyboard(uid))
    else:
        await update.message.reply_text("Ошибка или имя занято", reply_markup=main_keyboard(uid))
    return ConversationHandler.END


async def budget_cat_choose(update, context):
    uid = update.effective_user.id
    cat = update.message.text
    if cat not in get_user_categories(uid, 'expense') and cat != 'Общая':
        await update.message.reply_text(get_text(uid, 'budget_cat_choose'))
        return BUDGET_CAT
    context.user_data['budget_cat'] = cat
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(get_text(uid, 'current_month'), callback_data='current')],
        [InlineKeyboardButton(get_text(uid, 'next_month'), callback_data='next')],
        [InlineKeyboardButton(get_text(uid, 'whole_year'), callback_data='whole_year')]
    ])
    await update.message.reply_text(get_text(uid, 'budget_period'), reply_markup=kb)
    return BUDGET_PERIOD


async def budget_period_callback(update, context):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    choice = query.data
    now = datetime.now()
    if choice == 'current':
        months = [now.strftime('%Y-%m')]
    elif choice == 'next':
        nxt = (now.replace(day=1) + timedelta(days=32)).replace(day=1)
        months = [nxt.strftime('%Y-%m')]
    else:
        months = [datetime(now.year, m, 1).strftime('%Y-%m') for m in range(now.month, 13)]
    context.user_data['budget_months'] = months
    await query.edit_message_text(get_text(uid, 'budget_amount_prompt'))
    return BUDGET_AMOUNT


async def budget_set_amount(update, context):
    uid = update.effective_user.id
    try:
        amount = Decimal(update.message.text.replace(',', '.'))
        if amount <= 0: raise ValueError
    except:
        await update.message.reply_text(get_text(uid, 'add_amount_invalid'))
        return BUDGET_AMOUNT
    cat = context.user_data['budget_cat']
    for month_str in context.user_data['budget_months']:
        set_budget(uid, cat, amount, month_str)
    await update.message.reply_text(get_text(uid, 'budget_set', cat, amount), reply_markup=main_keyboard(uid))
    return ConversationHandler.END


# ----- Язык и главные кнопки -----
async def language_switch(update, context):
    uid = update.effective_user.id
    current = get_user_lang(uid)
    new = 'en' if current == 'ru' else 'ru'
    set_user_lang(uid, new)
    await update.message.reply_text(get_text(uid, 'lang_changed'), reply_markup=main_keyboard(uid))


async def handle_buttons(update, context):
    uid = update.effective_user.id
    text = update.message.text
    if text in ['➕ Добавить', '➕ Add']:
        await add_amount(update, context)
    elif text in ['💰 Баланс', '💰 Balance']:
        await balance(update, context)
    elif text in ['📋 История', '📋 History']:
        await history(update, context)
    elif text in ['📊 Отчёт', '📊 Report']:
        await report_interval(update, context)
    elif text in ['✏️ Редактировать', '✏️ Edit']:
        await edit_start(update, context)
    elif text in ['🗑️ Удалить', '🗑️ Delete']:
        await edit_start(update, context)
    elif text in ['📂 Категории и бюджеты', '📂 Categories & budgets']:
        await cat_budget_menu(update, context)
    elif text in ['📊 Графики', '📊 Charts']:
        await charts_menu(update, context)  # ← ЭТА СТРОКА ОТСУТСТВУЕТ!
    elif text in ['⏰ Напоминания', '⏰ Reminders']:
        await reminder_menu(update, context)  # ← ЭТА СТРОКА ТОЖЕ ОТСУТСТВУЕТ!
    elif text in ['🌐 Язык / Language']:
        await language_switch(update, context)
    else:
        await help_command(update, context)


async def cancel_all(update, context):
    uid = update.effective_user.id
    await update.message.reply_text(get_text(uid, 'cancel'), reply_markup=main_keyboard(uid))
    return ConversationHandler.END


# -------------------- Main --------------------
def main():
    init_db()
    req = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    app = Application.builder().token(TOKEN).request(req).build()

    # Настройка JobQueue для ежедневных напоминаний
    # JobQueue создается автоматически при использовании правильной установки
    job_queue = app.job_queue
    if job_queue:
        # Запускаем проверку каждый день в 10:00
        job_queue.run_daily(check_reminders, time=dt_time(hour=10, minute=0))
        logging.info("JobQueue для напоминаний настроен")
    else:
        logging.warning(
            "JobQueue не доступен. Напоминания не будут работать. Установите: pip install 'python-telegram-bot[job-queue]'")

    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('help', help_command))
    app.add_handler(CommandHandler('cancel', cancel_all))

    # Добавление
    add_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^[➕] Добавить|[➕] Add$"), add_amount)],
        states={
            ADD_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_amount)],
            ADD_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_type)],
            ADD_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_category)],
        },
        fallbacks=[CommandHandler('cancel', cancel_all)],
        per_message=False,
    )
    app.add_handler(add_conv)

    # Отчёты - исправлено per_message=True для CallbackQueryHandler
    report_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^[📊] Отчёт|[📊] Report$"), report_interval)],
        states={
            REPORT_INTERVAL: [CallbackQueryHandler(report_callback)],
            REPORT_DAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, report_day)],
            EXPORT: [CallbackQueryHandler(export_callback)],
        },
        fallbacks=[CommandHandler('cancel', cancel_all)],
        per_message=True,  # Изменено на True для поддержки CallbackQueryHandler
    )
    app.add_handler(report_conv)

    # Редактирование / удаление
    edit_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^[✏️] Редактировать|[✏️] Edit$"), edit_start)],
        states={
            EDIT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_get_id)],
            EDIT_ACTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_action)],
            EDIT_NEW_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_new_value)],
            DELETE_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_confirm)],
        },
        fallbacks=[CommandHandler('cancel', cancel_all)],
        per_message=False,
    )
    app.add_handler(edit_conv)

    # Напоминания
    reminder_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^[⏰] Напоминания|[⏰] Reminders$"), reminder_menu)],
        states={
            REMINDER_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, reminder_choice)],
            REMINDER_ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, reminder_add_name)],
            REMINDER_ADD_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, reminder_add_amount)],
            REMINDER_ADD_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, reminder_add_date)],
            # Изменено на MessageHandler
            REMINDER_ADD_FREQUENCY: [MessageHandler(filters.TEXT & ~filters.COMMAND, reminder_add_frequency)],
            REMINDER_ADD_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, reminder_add_interval)],
            REMINDER_DELETE: [MessageHandler(filters.TEXT & ~filters.COMMAND, reminder_delete)],
        },
        fallbacks=[CommandHandler('cancel', cancel_all)],
        per_message=False,
        allow_reentry=True,
    )
    app.add_handler(reminder_conv)

    # Графики
    charts_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^[📊] Графики|[📊] Charts$"), charts_menu)],
        states={
            CHARTS_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, charts_choice)],
            CHARTS_MONTH_YEAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, charts_month_year)],
            CHARTS_YEAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, charts_generate)],
        },
        fallbacks=[CommandHandler('cancel', cancel_all)],
        per_message=False,
    )
    app.add_handler(charts_conv)

    # Категории и бюджеты - исправлено per_message=True для CallbackQueryHandler
    cat_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^[📂] Категории и бюджеты|[📂] Categories & budgets$"), cat_budget_menu)],
        states={
            CAT_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, cat_budget_handler)],
            CAT_CREATE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, cat_create_name)],
            CAT_CREATE_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, cat_create_type)],
            CAT_DELETE_CHOOSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, cat_delete_choose)],
            CAT_DELETE_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, cat_delete_confirm)],
            CAT_RENAME_CHOOSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, cat_rename_choose)],
            CAT_RENAME_NEW: [MessageHandler(filters.TEXT & ~filters.COMMAND, cat_rename_new)],
            BUDGET_CAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, budget_cat_choose)],
            BUDGET_PERIOD: [CallbackQueryHandler(budget_period_callback)],
            BUDGET_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, budget_set_amount)],
        },
        fallbacks=[CommandHandler('cancel', cancel_all)],
        per_message=True,  # Изменено на True для поддержки CallbackQueryHandler
    )
    app.add_handler(cat_conv)

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))
    app.run_polling()


if __name__ == '__main__':
    main()
