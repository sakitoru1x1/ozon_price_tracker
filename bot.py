import asyncio
import json
import logging
import os
import re
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from typing import Tuple
from aiogram import Bot, Dispatcher, types, Router, F
from aiogram.filters import Command
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    Message
)
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiogram.filters.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium_stealth import stealth
from selenium.common.exceptions import (
    TimeoutException,
    WebDriverException,
    NoSuchElementException  # Добавить эту строку
)

OZON_LINK_RE = re.compile(r'https?://(www.)?ozon.(ru|by)/(product/|t/)[^s]+')

# =============================================
# НАСТРОЙКИ И ИНИЦИАЛИЗАЦИЯ
# =============================================

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

DATA_FILE = Path("user_data.json")
LOG_FILE = Path("user_actions.log")
MAX_URLS_PER_USER = 10
REQUEST_TIMEOUT = 20
ALLOWED_INTERVALS = [0, 1, 3, 5, 10, 24]
DEFAULT_INTERVAL = 24
INACTIVE_USER_THRESHOLD_DAYS = 30
OZON_DOMAINS = ("ru", "by")
INTERVAL_NAMES = {
    0: "По изменению цены",
    1: "1 час",
    3: "3 часа",
    5: "5 часов",
    10: "10 часов",
    24: "24 часа"
}
OWNER_ID = int(os.getenv("OWNER_ID"))

bot = Bot(token=os.getenv("BOT_TOKEN"))
router = Router()

# =============================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =============================================

def log_action(user: types.User, action: str, **kwargs):
    """Логирует действия пользователей кроме владельца"""
    if user.id == OWNER_ID:
        return

    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "user_id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "action": action
    }
    log_entry.update(kwargs)  # Добавляем дополнительные поля

    try:
        if not LOG_FILE.exists():
            LOG_FILE.touch(mode=0o600)

        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error(f"Ошибка записи в лог: {e}")

def format_interval(interval: int) -> str:
    return INTERVAL_NAMES.get(interval, f"{interval} часов")

class Form(StatesGroup):
    add_url = State()
    remove_url = State()

class ProductMenu:
    @staticmethod
    def get_main_menu():
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📋 Список товаров"), KeyboardButton(text="🔍 Проверка товаров")],
                [KeyboardButton(text="➕ Добавить товар"), KeyboardButton(text="🗑️ Удалить товар")],
                [KeyboardButton(text="⏱️ Интервал проверки"), KeyboardButton(text="📊 Статистика")],
                [KeyboardButton(text="ℹ️ Помощь")]
            ],
            resize_keyboard=True
        )

    @staticmethod
    def get_check_menu():
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="🔄 Проверить сейчас")],
                [KeyboardButton(text="⏸️ Прекратить отслеживание"), KeyboardButton(text="▶️ Возобновить отслеживание")],
                [KeyboardButton(text="🔙 Назад")]
            ],
            resize_keyboard=True
        )

    @staticmethod
    def get_interval_menu():
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="По изменению цены")],
                [KeyboardButton(text="1 час"), KeyboardButton(text="3 часа")],
                [KeyboardButton(text="5 часов"), KeyboardButton(text="10 часов")],
                [KeyboardButton(text="24 часа"), KeyboardButton(text="🔙 Назад")]
            ],
            resize_keyboard=True
        )

    @staticmethod
    def get_back_button():
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="🔙 Назад")]],
            resize_keyboard=True
        )

    @staticmethod
    def get_remove_menu(urls: List[str], product_names: Dict[str, str]):
        buttons = []
        for i, url in enumerate(urls, 1):
            name = product_names.get(url, f"Товар {i}")
            short_name = (name[:20] + "...") if len(name) > 20 else name
            buttons.append([KeyboardButton(text=f"🗑️ {i}. {short_name}")])
        buttons.append([KeyboardButton(text="🗑️ Удалить ВСЕ товары")])
        buttons.append([KeyboardButton(text="🔙 Назад")])
        return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

async def show_animation(msg: Message, text: str):
    """Показывает анимированное сообщение"""
    for emoji in ["🔄", "⏳", "🌀"]:
        await msg.edit_text(f"{emoji} {text}")
        await asyncio.sleep(0.2)
    await msg.edit_text(text)

async def delete_messages(chat_id: str, message_ids: list):
    """Удаляет список сообщений с обработкой ошибок"""
    for msg_id in message_ids:
        try:
            await bot.delete_message(chat_id, msg_id)
            await asyncio.sleep(0.1)
        except TelegramBadRequest as e:
            if "message to delete not found" not in str(e):
                logger.error(f"Ошибка удаления: {e}")
        except Exception as e:
            logger.error(f"Ошибка: {e}")

async def animate_success(msg: Message):
    """Анимация успешного завершения БЕЗ удаления сообщения"""
    try:
        for emoji in ["✨", "🌟", "💫", "🎉"]:
            try:
                await msg.edit_text(f"{emoji} {msg.html_text}")
                await asyncio.sleep(0.3)
            except TelegramBadRequest as e:
                if "message to edit not found" in str(e):
                    return
    except Exception as e:
        logger.error(f"Ошибка анимации: {e}")

def truncate(text: str, max_length: int) -> str:
    """Обрезает текст с многоточием"""
    return (text[:max_length] + '...') if len(text) > max_length else text

async def show_main_menu(message: Message):
    """Показывает главное меню"""
    await message.answer(
        "Выберите действие:",
        reply_markup=ProductMenu.get_main_menu()
    )

async def update_skus():
    logger.info("=== Начало обновления артикулов ===")
    for chat_id in user_data:
        user_info = user_data[chat_id]
        urls = user_info.get('urls', [])
        if not urls:
            continue

        # Получаем данные сразу по всем товарам списком
        try:
            products_data = await batch_fetch_products(urls)
        except Exception as e:
            logger.error(f"Ошибка пакетного получения данных: {e}")
            continue

        updated_skus = {}
        for url in urls:
            name, prices, new_full_sku, is_out_of_stock = products_data.get(url, (None, {}, None, True))
            if new_full_sku:
                # Удаляем старый артикул
                for old_sku in list(user_info.get('skus', {})):
                    if user_info['skus'][old_sku] == url:
                        del user_info['skus'][old_sku]
                # Добавляем новый
                updated_skus[new_full_sku] = url

        # Сохраняем обновленные данные
        user_info['skus'].update(updated_skus)
        save_user_data()

    logger.info("=== Обновление артикулов завершено ===")


def normalize_ozon_url(url: str) -> str:
    """Приводит ozon-ссылку к каноническому виду (без www и query-параметров)"""
    url = url.lower().split('?')[0].replace('www.', '')
    return url

def is_duplicate(url: str, full_sku: str, user_info: dict) -> Optional[str]:
    """
    Проверяет наличие дубликата по артикулу (full_sku) и по url.
    Возвращает строку-пояснение для пользователя, если есть дубль, иначе None.
    """
    # Проверка по артикулу (full_sku)
    if full_sku and 'skus' in user_info and full_sku in user_info['skus']:
        exist_url = user_info['skus'][full_sku]
        return f"✔️ Этот товар уже отслеживается:\n{exist_url}"
    # Проверка дубликата по url (для страховки)
    norm_url = normalize_ozon_url(url)
    for u in user_info.get('urls', []):
        if normalize_ozon_url(u) == norm_url:
            return f"✔️ Такой товар уже добавлен:\n{u}"
    return None

# =============================================
# ФУНКЦИИ РАБОТЫ С ДАННЫМИ
# =============================================

def migrate_user_data(data: dict) -> dict:
    """Мигрирует данные для поддержки артикулов"""
    migrated = {}
    for chat_id, user_info in data.items():
        migrated[chat_id] = {
            'urls': user_info.get('urls', []),
            'previous_prices': user_info.get('previous_prices', {}),
            'product_names': user_info.get('product_names', {}),
            'skus': user_info.get('skus', {}),  # Новое поле: {артикул: url}
            'last_active': user_info.get('last_active', datetime.now().isoformat()),
            'interval': user_info.get('interval', DEFAULT_INTERVAL),
            'last_check': user_info.get('last_check', None),
            'is_tracking': user_info.get('is_tracking', True)
        }
        # Автоматическая миграция для существующих данных
        if 'skus' not in user_info:
            migrated[chat_id]['skus'] = {}
            for url in user_info.get('urls', []):
                # Попытка извлечь артикул из URL
                match = re.search(r'/product/(\d+)/', url)
                if match:
                    migrated[chat_id]['skus'][match.group(1)] = url
    return migrated


def load_user_data() -> Dict[str, Any]:
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, "r", encoding='utf-8') as f:
                return migrate_user_data(json.load(f))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.error(f"Ошибка загрузки данных: {e}")
    return {}

def save_user_data():
    try:
        with open(DATA_FILE, "w", encoding='utf-8') as f:
            json.dump(user_data, f, indent=2, ensure_ascii=False)
    except IOError as e:
        logger.error(f"Ошибка сохранения данных: {e}")

user_data = load_user_data()

# =============================================
# WEBDRIVER И РАБОТА С OZON
# =============================================

def setup_driver() -> webdriver.Chrome:
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--log-level=3")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    driver = webdriver.Chrome(options=options)
    stealth(
        driver,
        languages=["en-US", "en"],
        vendor="Google Inc.",
        platform="Win32",
        webgl_vendor="Intel Inc.",
        renderer="Intel Iris OpenGL Engine",
        fix_hairline=True,
    )
    return driver

def clean_price(price_text: str) -> Optional[int]:
    try:
        return int(re.sub(r"[^\d]", "", price_text))
    except (ValueError, TypeError, AttributeError):
        return None

async def batch_fetch_products(urls: List[str]) -> Dict[str, Tuple[Optional[str], Dict[int, int], Optional[str], bool]]:
    """Обрабатывает все URL за одну сессию драйвера"""
    result = {}

    def sync_fetch():
        driver = setup_driver()
        try:
            for url in urls:
                try:
                    driver.get(url)
                    WebDriverWait(driver, 15).until(
                        lambda d: d.current_url.startswith("https://www.ozon.") or
                                  d.current_url.startswith("https://ozon.")
                    )

                    # Кончился ли товар
                    is_out_of_stock = False
                    try:
                        WebDriverWait(driver, 5).until(
                            EC.presence_of_element_located((By.XPATH, '//*[contains(text(), "Этот товар закончился")]'))
                        )
                        is_out_of_stock = True
                    except Exception:
                        pass

                    # Получаем артикул
                    full_sku = None
                    try:
                        sku_elem = driver.find_element(By.XPATH, '//*[@data-widget="webDetailSKU"]')
                        match = re.search(r'Артикул:\s*(\S+)', sku_elem.text.strip())
                        full_sku = match.group(1) if match else None
                    except Exception:
                        pass

                    # Цены
                    prices = {}
                    try:
                        price_elems = WebDriverWait(driver, 10).until(
                            EC.presence_of_all_elements_located((By.XPATH, '//*[@data-widget="webPrice"]//span[contains(text(),"₽")]'))
                        )
                        for i, elem in enumerate(price_elems[:2], 1):
                            price = clean_price(elem.text)
                            if price is not None:
                                prices[i] = price
                    except Exception:
                        pass

                    # Название
                    name = None
                    try:
                        heading_elem = WebDriverWait(driver, 7).until(
                            EC.visibility_of_element_located((By.XPATH, '//*[@data-widget="webProductHeading"]//h1'))
                        )
                        name = heading_elem.text.strip()
                    except Exception:
                        pass

                    result[url] = (name, prices, full_sku, is_out_of_stock)
                    if driver.current_url != url and "captcha" in driver.current_url:
                        raise Exception("Обнаружена капча")

                except Exception as e:
                    logger.error(f"Ошибка обработки {url}: {str(e)}")
                    result[url] = (None, {}, None, True)
                    continue
        finally:
            try:
                driver.quit()
            except:
                pass
        return result

    return await asyncio.to_thread(sync_fetch)

# =============================================
# ОБРАБОТКА ЦЕН И УВЕДОМЛЕНИЙ
# =============================================

def get_price_display(prices: Dict[int, int]) -> str:
    card_price = prices.get(1, 'н/д')
    regular_price = prices.get(2, card_price if card_price != 'н/д' else 'н/д')
    return f"{card_price:,} / {regular_price:,}".replace(",", " ") if isinstance(card_price, int) else "н/д"

def generate_product_list(user_info: dict) -> str:
    response = ["📋 <b>Отслеживаемые товары:</b>"]
    for i, url in enumerate(user_info['urls'], 1):
        product_name = user_info['product_names'].get(url)
        prices = user_info['previous_prices'].get(url, {})
        price_display = get_price_display(prices)

        if not product_name:
            product_id = re.search(r'(product/|t/)([^/]+)', url)
            product_name = f"Товар {i} (ID: {product_id.group(2)[:20]}...)" if product_id else f"Товар {i}"
        else:
            product_name = product_name[:50] + "..." if len(product_name) > 50 else product_name

        response.append(f"{i}. <a href='{url}'>{product_name}</a> (последняя цена: {price_display}₽)")

    response.append(f"\nВсего: {len(user_info['urls'])}/{MAX_URLS_PER_USER}")
    return "\n".join(response)

def compare_prices(previous: Optional[Dict[int, int]], current: Dict[int, int]) -> List[str]:
    changes = []
    price_names = {1: "по карте", 2: "обычная"}

    if previous:
        for idx in current:
            prev_price = previous.get(idx)
            curr_price = current[idx]
            if prev_price is None:
                changes.append(f"• Цена {price_names[idx]} добавлена: {curr_price:,}₽".replace(",", " "))
            elif curr_price != prev_price:
                diff = abs(curr_price - prev_price)
                if curr_price > prev_price:
                    changes.append(f"• Цена {price_names[idx]} ↗ {curr_price:,}₽ (+{diff:,}₽".replace(",", " "))
                else:
                    changes.append(f"• Цена {price_names[idx]} ↘ {curr_price:,}₽ (-{diff:,}₽".replace(",", " "))
            else:
                changes.append(f"• Цена {price_names[idx]} не изменилась")
    else:
        changes.append("• Первая проверка цен")
    return changes

async def check_prices(chat_id: str, force_notify: bool = False):
    user_info = user_data.get(chat_id)
    if not user_info or not user_info.get('urls') or not user_info.get('is_tracking', True):
        return

    products_data = await batch_fetch_products(user_info['urls'])

    for url in user_info['urls']:
        name, prices, full_sku, is_out_of_stock = products_data.get(url, (None, {}, None, True))
        if not name or not prices or is_out_of_stock:
            continue

        user_info['product_names'][url] = name
        previous = user_info['previous_prices'].get(url, {})
        changes = compare_prices(previous, prices)
        user_info['previous_prices'][url] = prices

        # --- Главный фильтр для "По изменению цены" ---
        if user_info['interval'] == 0 and not force_notify:
            only_first_check = len(changes) == 1 and changes[0].startswith("• Первая проверка цен")
            only_no_change = all(
                (("не изменилась" in line) or ("Первая проверка цен" in line)) for line in changes
            )
            if only_first_check or only_no_change:
                continue

        if not force_notify and len(changes) == 1 and changes[0] == "• Первая проверка цен":
            continue

        card_price = prices.get(1, 'н/д')
        regular_price = prices.get(2, card_price if card_price != 'н/д' else 'н/д')
        interval_text = (
            "🔔 Режим: По изменению цены" if user_info['interval'] == 0 else
            f"⏱️ Следующая проверка: {(datetime.now() + timedelta(hours=user_info['interval'])).strftime('%H:%M %d.%m.%Y')}"
        )

        result = (
            f"🛍️ <b>{name}</b>\n"
            f"💳 {card_price:,}₽ | 🛒 {regular_price:,}₽\n".replace(",", " ") +
            f"📦 Артикул: {full_sku}\n"
            f"🔗 <a href='{url}'>Ссылка на товар</a>\n"
            f"📅 {datetime.now().strftime('%H:%M %d.%m.%Y')}\n"
            f"{interval_text}\n"
            f"\n<b>Изменения:</b>\n" + "\n".join(changes)
        )

        try:
            await bot.send_message(
                chat_id,
                result,
                disable_web_page_preview=True,
                parse_mode="HTML",
                reply_markup=ProductMenu.get_main_menu()
            )
            await asyncio.sleep(1)
        except TelegramForbiddenError:
            del user_data[chat_id]
            save_user_data()
            break
        except TelegramBadRequest as e:
            logger.error(f"Ошибка отправки: {e}")

    user_info['last_active'] = datetime.now().isoformat()
    save_user_data()

# =============================================
# ОСНОВНЫЕ ОБРАБОТЧИКИ
# =============================================

@router.message(Command("start"))
async def cmd_start(message: types.Message):
    chat_id = str(message.chat.id)
    log_action(message.from_user, "Запуск бота")

    # Инициализация данных пользователя, если их нет
    if chat_id not in user_data:
        user_data[chat_id] = {
            'urls': [],
            'previous_prices': {},
            'product_names': {},
            'last_active': datetime.now().isoformat(),
            'interval': DEFAULT_INTERVAL,
            'last_check': None,
            'is_tracking': True
        }
        save_user_data()

    await message.answer(
        "👋 <b>Добро пожаловать в Ozon Price Tracker!</b>\n\n"
        "Используйте кнопки меню для управления:",
        parse_mode="HTML",
        reply_markup=ProductMenu.get_main_menu()
    )

@router.message(F.text == "🔙 Назад")
async def handle_back(message: types.Message, state: FSMContext):
    log_action(message.from_user, "Возврат в главное меню")
    await state.clear()
    await message.answer(
        "Главное меню:",
        reply_markup=ProductMenu.get_main_menu()
    )

@router.message(Command("help"))
@router.message(F.text == "ℹ️ Помощь")
async def cmd_help(message: types.Message):
    log_action(message.from_user, "Просмотр помощи")
    help_text = (
        "🛍️ <b>Ozon Price Tracker - Помощь</b>\n\n"
        "📌 <b>Основные возможности:</b>\n"
        "• Отслеживание цен на товары с Ozon\n"
        "• Мгновенные уведомления об изменении цены\n"
        "• Гибкая настройка интервалов проверки\n"
        "• Управление списком товаров через меню\n\n"

        "🔧 <b>Как пользоваться:</b>\n"
        "1. <b>Добавить товар:</b>\n"
        "   - Нажмите ➕ Добавить товар\n"
        "   - Отправьте ссылку на товар Ozon\n"
        "   Примеры ссылок:\n"
        "   <code>https://ozon.ru/product/123</code>\n"
        "   <code>https://ozon.by/t/AbcDeF</code>\n\n"

        "2. <b>Удалить товар:</b>\n"
        "   - Нажмите 🗑️ Удалить товар\n"
        "   - Выберите номер товара из списка\n"
        "   - Для удаления всех: 🗑️ Удалить ВСЕ товары\n\n"

        "3. <b>Настройка интервала:</b>\n"
        "   - ⏱️ Интервал проверки - выбирайте из предложенных\n"
        "   - 🔔 Режим 'По изменению цены': бот проверяет товары каждый час "
        "и присылает уведомления ТОЛЬКО при изменении цены\n\n"

        "4. <b>Ручная проверка:</b>\n"
        "   - 🔍 Проверка товаров → 🔄 Проверить сейчас\n"
        "   - Независимо от настроек интервала\n\n"

        "5. <b>Управление отслеживанием:</b>\n"
        "   - ⏸️ Приостановить: прекращает все проверки\n"
        "   - ▶️ Возобновить: продолжает с текущими настройками\n\n"

        "📝 <b>Важно знать:</b>\n"
        f"• Максимум товаров: {MAX_URLS_PER_USER}\n"
        "• При отсутствии активности более 30 дней данные удаляются\n"
        "• Бот не отслеживает товары при выключенном отслеживании\n\n"

        "🆘 <b>Проблемы?</b>\n"
        "• Неверная ссылка: проверьте формат ссылки\n"
        "• Нет уведомлений: проверьте настройки интервала\n"
        "• Пропали товары: возможно закончилось место в списке\n"
        "• Пишите: @matrix_is_the_first_step"
    )
    await message.answer(
        help_text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=ProductMenu.get_main_menu()
    )

@router.message(F.text == "🔍 Проверка товаров")
async def check_menu(message: types.Message):
    log_action(message.from_user, "Открытие меню проверки")
    await message.answer("Выберите действие:", reply_markup=ProductMenu.get_check_menu())

@router.message(Command("list"))
@router.message(F.text == "📋 Список товаров")
async def list_urls(message: types.Message):
    log_action(message.from_user, "Просмотр списка товаров")
    chat_id = str(message.chat.id)
    if chat_id not in user_data or not user_data[chat_id].get('urls'):
        await message.answer("📭 Список пуст", reply_markup=ProductMenu.get_main_menu())
        return

    product_list = generate_product_list(user_data[chat_id])
    await message.answer(
        product_list,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=ProductMenu.get_main_menu()
    )

# =============================================
# ОБРАБОТЧИКИ ДОБАВЛЕНИЯ ТОВАРОВ
# =============================================

@router.message(Command("add"))
@router.message(F.text == "➕ Добавить товар")
async def add_url_command(message: types.Message, state: FSMContext):
    log_action(message.from_user, "Начало добавления товара")
    chat_id = str(message.chat.id)
    if chat_id not in user_data:
        await message.answer("❌ Сначала запустите бота командой /start", reply_markup=ProductMenu.get_main_menu())
        return

    user_info = user_data[chat_id]
    if len(user_info['urls']) >= MAX_URLS_PER_USER:
        await message.answer(f"❌ Лимит {MAX_URLS_PER_USER} товаров!", reply_markup=ProductMenu.get_main_menu())
        return

    await state.set_state(Form.add_url)
    await message.answer(
        "📥 Отправьте ссылку на товар Ozon в сообщении боту:\n\n",
        reply_markup=ProductMenu.get_back_button()
    )

@router.message(Form.add_url)
async def add_url_state(message: types.Message, state: FSMContext):
    user = message.from_user
    chat_id = str(message.chat.id)
    user_info = user_data.get(chat_id, {})
    temp_messages = []

    try:
        temp_messages.append(message.message_id)
        loading_msg = await message.answer("⏳ <i>Инициализация проверки...</i>", parse_mode="HTML")
        temp_messages.append(loading_msg.message_id)

        url = message.text.strip()
        if url.startswith('ozon.ru/t/'):
            url = 'https://' + url  # Автоматически добавляем https
        log_action(user, "Попытка добавления товара", product_url=url)

        # Проверка формата ссылки
        await loading_msg.edit_text("⏳ <i>Проверка формата ссылки...</i>", parse_mode="HTML")
        await asyncio.sleep(0.5)
        if not re.match(r'^https?://(www\.)?ozon\.(ru|by)/(product/|t/)', url):
            await show_animation(loading_msg, "❌ Неверный формат!")
            await delete_messages(chat_id, temp_messages)
            return await show_main_menu(message)

        # Получение данных с сайта
        await loading_msg.edit_text("⏳ <i>Проверка на дубликаты...</i>", parse_mode="HTML")
        products_data = await batch_fetch_products([url])
        name, prices, full_sku, is_out_of_stock = products_data.get(url, (None, {}, None, True))

        if not all([name, prices, full_sku]):
            await show_animation(loading_msg, "⚠️ Не удалось получить данные!")
            await delete_messages(chat_id, temp_messages)
            return await show_main_menu(message)

        if not full_sku:
            await show_animation(loading_msg, "❌ Не удалось получить артикул!")
            await delete_messages(chat_id, temp_messages)
            return await show_main_menu(message)

        # Проверка на дубль по артикулу (full_sku) и url
        dupe_reason = is_duplicate(url, full_sku, user_info)
        if dupe_reason:
            await show_animation(loading_msg, dupe_reason)
            await delete_messages(chat_id, temp_messages)
            return await show_main_menu(message)

        # Проверка наличия товара
        await loading_msg.edit_text("⏳ <i>Проверка наличия товара...</i>", parse_mode="HTML")
        if is_out_of_stock:
            await show_animation(loading_msg, "🚫 Товар закончился!")
            await delete_messages(chat_id, temp_messages)
            return await show_main_menu(message)

        if not name or not prices:
            await show_animation(loading_msg, "⚠️ Ошибка данных!")
            await delete_messages(chat_id, temp_messages)
            return await show_main_menu(message)

        # Успешное добавление
        user_info.setdefault('urls', []).append(url)
        user_info.setdefault('product_names', {})[url] = name
        user_info.setdefault('previous_prices', {})[url] = prices
        user_info.setdefault('skus', {})[full_sku] = url
        user_info['last_active'] = datetime.now().isoformat()
        save_user_data()

        success_text = (
            f"✅ <b>Товар добавлен!</b>\n"
            f"🏷 {name}\n"
            f"📦 Артикул: <code>{full_sku}</code>\n"
            f"💵 {get_price_display(prices)}₽\n"
            f"🔗 <a href='{url}'>Ссылка на товар</a>\n"
            f"⏱️ Режим проверки: {format_interval(user_info.get('interval', DEFAULT_INTERVAL))}"
        )

        success_msg = await message.answer(
            success_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=ProductMenu.get_main_menu()
        )

        await animate_success(success_msg)
        await delete_messages(chat_id, temp_messages)

    except Exception as e:
        logger.error(f"Ошибка: {str(e)}", exc_info=True)
        await delete_messages(chat_id, temp_messages)
        await message.answer("⚠️ Произошла ошибка", reply_markup=ProductMenu.get_main_menu())
    finally:
        await state.clear()



# =============================================
# ОБРАБОТЧИКИ УДАЛЕНИЯ ТОВАРОВ
# =============================================

@router.message(F.text == "🗑️ Удалить товар")
async def remove_url_menu(message: types.Message, state: FSMContext):
    log_action(message.from_user, "Открытие меню удаления")
    chat_id = str(message.chat.id)
    user_info = user_data.get(chat_id)

    if not user_info or not user_info.get('urls'):
        await message.answer("❌ Нет отслеживаемых товаров!", reply_markup=ProductMenu.get_main_menu())
        return

    product_list = generate_product_list(user_info)
    await message.answer(
        product_list,
        parse_mode="HTML",
        disable_web_page_preview=True
    )

    await message.answer(
        "Выберите товар для удаления:",
        reply_markup=ProductMenu.get_remove_menu(
            user_info['urls'],
            user_info['product_names']
        )
    )
    await state.set_state(Form.remove_url)

@router.message(F.text.startswith("🗑️"))
async def handle_remove_actions(message: types.Message, state: FSMContext):
    user = message.from_user
    if message.text == "🗑️ Удалить ВСЕ товары":
        log_action(user, "Попытка удаления всех товаров")
        await remove_all_products(message, state)
    elif re.match(r"^🗑️\s*\d+\.", message.text):
        log_action(user, "Попытка удаления товара")
        await remove_single_product(message, state)
    else:
        await message.answer("❌ Неизвестная команда!", reply_markup=ProductMenu.get_main_menu())

async def remove_single_product(message: types.Message, state: FSMContext):
    user = message.from_user
    chat_id = str(message.chat.id)
    user_info = user_data.get(chat_id)

    if not user_info or not user_info.get('urls'):
        await message.answer("❌ Нет товаров для удаления!", reply_markup=ProductMenu.get_main_menu())
        return

    match = re.search(r"(\d+)", message.text)
    if not match:
        await message.answer("❌ Ошибка распознавания номера!",
                           reply_markup=ProductMenu.get_remove_menu(
                               user_info['urls'],
                               user_info['product_names']
                           ))
        return

    try:
        product_num = int(match.group(1))
        if 1 <= product_num <= len(user_info['urls']):
            removed_url = user_info['urls'].pop(product_num - 1)

            # Удаление артикула (артикул = ключ, url = значение)
            sku_to_delete = None
            for sku, url in list(user_info['skus'].items()):
                if url == removed_url:
                    sku_to_delete = sku
                    break
            if sku_to_delete:
                del user_info['skus'][sku_to_delete]

            product_name = user_info['product_names'].get(removed_url, "Неизвестно")
            user_info['previous_prices'].pop(removed_url, None)
            user_info['product_names'].pop(removed_url, None)
            user_info['last_active'] = datetime.now().isoformat()
            save_user_data()

            log_action(
                user=user,
                action="Товар удален",
                product_name=product_name,
                product_url=removed_url,
                sku=sku
            )
            await message.answer("🗑️ Товар удален!", reply_markup=ProductMenu.get_main_menu())
            await state.clear()
        else:
            await message.answer("❌ Неверный номер товара!",
                               reply_markup=ProductMenu.get_remove_menu(
                                   user_info['urls'],
                                   user_info['product_names']
                               ))
    except (ValueError, IndexError):
        await message.answer("❌ Неверный номер товара!",
                           reply_markup=ProductMenu.get_remove_menu(
                               user_info['urls'],
                               user_info['product_names']
                           ))

async def remove_all_products(message: types.Message, state: FSMContext):
    user = message.from_user
    chat_id = str(message.chat.id)
    user_info = user_data.get(chat_id)

    if not user_info or not user_info.get('urls'):
        await message.answer("❌ Нет товаров для удаления!", reply_markup=ProductMenu.get_main_menu())
        return

    user_info['urls'].clear()
    user_info['previous_prices'].clear()
    user_info['product_names'].clear()
    user_info['skus'].clear()  # <--- добавить это!
    user_info['last_active'] = datetime.now().isoformat()
    save_user_data()

    log_action(user, "Все товары удалены")
    await message.answer("✅ Все товары удалены!", reply_markup=ProductMenu.get_main_menu())
    await state.clear()

# =============================================
# ОБРАБОТЧИКИ ИНТЕРВАЛА ПРОВЕРКИ
# =============================================

@router.message(Command("setinterval"))
@router.message(F.text == "⏱️ Интервал проверки")
async def set_interval_menu(message: types.Message):
    log_action(message.from_user, "Изменение интервала проверки")
    chat_id = str(message.chat.id)
    if chat_id not in user_data:
        await message.answer("❌ Сначала запустите бота /start", reply_markup=ProductMenu.get_main_menu())
        return
    await message.answer("🕒 Выберите интервал:", reply_markup=ProductMenu.get_interval_menu())

@router.message(F.text.in_(INTERVAL_NAMES.values()))
async def set_interval_value(message: types.Message):
    user = message.from_user
    chat_id = str(message.chat.id)
    if chat_id not in user_data:
        await message.answer("❌ Сначала запустите бота /start", reply_markup=ProductMenu.get_main_menu())
        return

    interval = next(k for k, v in INTERVAL_NAMES.items() if v == message.text)
    user_data[chat_id]['interval'] = interval
    user_data[chat_id]['last_active'] = datetime.now().isoformat()
    save_user_data()

    log_action(user, f"Установлен интервал: {format_interval(interval)}")
    response = ("✅ Режим проверки: По изменению цены\n• Проверка каждый час\n• Уведомления только при изменениях"
                if interval == 0 else
                f"✅ Интервал: каждые {format_interval(interval)}")
    await message.answer(response, reply_markup=ProductMenu.get_main_menu())

# =============================================
# ДОПОЛНИТЕЛЬНЫЕ ОБРАБОТЧИКИ
# =============================================

@router.message(Command("check"))
@router.message(F.text == "🔄 Проверить сейчас")
async def manual_check(message: types.Message):
    log_action(message.from_user, "Ручная проверка цен")
    chat_id = str(message.chat.id)
    if chat_id not in user_data or not user_data[chat_id].get('urls'):
        await message.answer("❌ Нет товаров для проверки!", reply_markup=ProductMenu.get_main_menu())
        return

    msg = await message.answer("⏳ Запрашиваю актуальные цены... Это может занять некоторое время.", parse_mode="HTML")
    user_data[chat_id]['last_active'] = datetime.now().isoformat()
    save_user_data()

    await check_prices(chat_id, force_notify=True)
    user_data[chat_id]['last_check'] = datetime.now().isoformat()
    save_user_data()

    try: await bot.delete_message(chat_id, msg.message_id)
    except: pass

@router.message(Command("stats"))
@router.message(F.text == "📊 Статистика")
async def show_stats(message: types.Message):
    log_action(message.from_user, "Просмотр статистики")
    chat_id = str(message.chat.id)
    if chat_id not in user_data:
        await message.answer("❌ Вы еще не начали отслеживать товары!", reply_markup=ProductMenu.get_main_menu())
        return

    user_info = user_data[chat_id]
    interval = user_info.get('interval', DEFAULT_INTERVAL)
    last_check = user_info.get('last_check')
    tracking_status = "✅ Активно" if user_info.get('is_tracking', True) else "⏸ Приостановлено"

    stats_message = (
        f"📊 <b>Статистика:</b>\n\n"
        f"• Статус отслеживания: {tracking_status}\n"
        f"• Интервал проверки: {format_interval(interval)}\n"
        f"• Последняя проверка: {last_check[:16] if last_check else 'еще не было'}\n"
        f"• Отслеживается товаров: {len(user_info.get('urls', []))}\n"
        f"• Максимум товаров: {MAX_URLS_PER_USER}"
    )
    await message.answer(stats_message, parse_mode="HTML", reply_markup=ProductMenu.get_main_menu())

@router.message(Command("logs"))
async def send_logs(message: types.Message):
    if message.from_user.id != OWNER_ID:
        return

    try:
        with open(LOG_FILE, "rb") as f:
            await message.answer_document(
                types.BufferedInputFile(
                    f.read(),
                    filename="user_actions.log"
                ),
                caption="📁 Логи действий пользователей"
            )
    except Exception as e:
        await message.answer(f"❌ Ошибка получения логов: {str(e)}")

@router.message(F.text.regexp(r'^https?://(www\.)?ozon\.(ru|by)/(product/|t/)'))
async def handle_direct_link(message: types.Message):
    chat_id = str(message.chat.id)
    user = message.from_user
    user_info = user_data.get(chat_id)
    temp_messages = []

    try:
        # Проверка наличия текста сообщения
        if not message.text:
            await message.answer("❌ Не получена ссылка")
            return

        url = message.text.strip()
        if url.startswith('ozon.ru/t/'):
            url = 'https://' + url
        temp_messages.append(message.message_id)

        # Проверка инициализации пользователя
        if not user_info:
            await message.answer("❌ Сначала запустите бота командой /start",
                               reply_markup=ProductMenu.get_main_menu())
            return

        # Проверка лимита товаров
        if len(user_info['urls']) >= MAX_URLS_PER_USER:
            await message.answer(f"❌ Лимит {MAX_URLS_PER_USER} товаров!",
                               reply_markup=ProductMenu.get_main_menu())
            return

        loading_msg = await message.answer("⏳ <i>Проверка ссылки...</i>", parse_mode="HTML")
        temp_messages.append(loading_msg.message_id)

        log_action(user, "Попытка добавления товара (прямая ссылка)", product_url=url)

        # Проверка формата ссылки уже есть в роутере

        await loading_msg.edit_text("⏳ <i>Проверка на дубликаты...</i>", parse_mode="HTML")
        products_data = await batch_fetch_products([url])
        name, prices, full_sku, is_out_of_stock = products_data.get(url, (None, {}, None, True))

        if not full_sku:
            await show_animation(loading_msg, "❌ Не удалось получить артикул!")
            await delete_messages(chat_id, temp_messages)
            return await show_main_menu(message)

        # ВАЖНО: Проверка дубля по артикула!
        dupe_reason = is_duplicate(url, full_sku, user_info)
        if dupe_reason:
            await show_animation(loading_msg, dupe_reason)
            await delete_messages(chat_id, temp_messages)
            return await show_main_menu(message)

        # Проверка наличия товара
        await loading_msg.edit_text("⏳ <i>Проверка наличия товара...</i>", parse_mode="HTML")
        if is_out_of_stock:
            await show_animation(loading_msg, "🚫 Товар закончился!")
            await delete_messages(chat_id, temp_messages)
            return await show_main_menu(message)

        if not name or not prices:
            await show_animation(loading_msg, "⚠️ Ошибка данных!")
            await delete_messages(chat_id, temp_messages)
            return await show_main_menu(message)

        # Успешное добавление
        user_info.setdefault('urls', []).append(url)
        user_info.setdefault('product_names', {})[url] = name
        user_info.setdefault('previous_prices', {})[url] = prices
        user_info.setdefault('skus', {})[full_sku] = url
        user_info['last_active'] = datetime.now().isoformat()
        save_user_data()

        success_text = (
            f"✅ <b>Товар добавлен!</b>\n"
            f"🏷 {name}\n"
            f"📦 Артикул: <code>{full_sku}</code>\n"
            f"💵 {get_price_display(prices)}₽\n"
            f"🔗 <a href='{url}'>Ссылка на товар</a>\n"
            f"⏱️ Режим проверки: {format_interval(user_info.get('interval', DEFAULT_INTERVAL))}"
        )

        success_msg = await message.answer(
            success_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=ProductMenu.get_main_menu()
        )

        await animate_success(success_msg)
        await asyncio.sleep(1)
        await delete_messages(chat_id, temp_messages)

    except Exception as e:
        logger.error(f"Ошибка при обработке ссылки: {e}")
        await delete_messages(chat_id, temp_messages)
        await message.answer("⚠️ Произошла ошибка при обработке ссылки",
                           reply_markup=ProductMenu.get_main_menu())

# Обработчик для удаления неподдерживаемых сообщений
@router.message()
async def handle_any_message(message: types.Message):
    text = message.text or ""
    url_match = OZON_LINK_RE.search(text)
    if url_match:
        url = url_match.group(0)
        chat_id = str(message.chat.id)
        user = message.from_user
        user_info = user_data.get(chat_id)
        temp_messages = []

        # Лимиты и первичная проверка
        if not user_info:
            await message.answer("❌ Сначала запустите бота командой /start",
                                 reply_markup=ProductMenu.get_main_menu())
            return

        if len(user_info['urls']) >= MAX_URLS_PER_USER:
            await message.answer(f"❌ Лимит {MAX_URLS_PER_USER} товаров!",
                                 reply_markup=ProductMenu.get_main_menu())
            return

        try:
            loading_msg = await message.answer("⏳ <i>Проверяю ссылку…</i>", parse_mode="HTML")
            temp_messages.append(loading_msg.message_id)

            products_data = await batch_fetch_products([url])
            name, prices, full_sku, is_out_of_stock = products_data.get(url, (None, {}, None, True))

            # Проверка артикула
            if not full_sku:
                await loading_msg.edit_text("❌ Не удалось получить артикул!")
                await delete_messages(chat_id, temp_messages)
                return

            # Проверка дубликатов
            dupe_reason = is_duplicate(url, full_sku, user_info)
            if dupe_reason:
                await loading_msg.edit_text(dupe_reason)
                temp_messages.append(loading_msg.message_id)
                await delete_messages(chat_id, temp_messages)
                return

            # Наличие товара
            if is_out_of_stock:
                await loading_msg.edit_text("🚫 Товар закончился!")
                temp_messages.append(loading_msg.message_id)
                await delete_messages(chat_id, temp_messages)
                return

            if not name or not prices:
                await loading_msg.edit_text("⚠️ Ошибка данных!")
                temp_messages.append(loading_msg.message_id)
                await delete_messages(chat_id, temp_messages)
                return

            # Успешное добавление
            user_info.setdefault('urls', []).append(url)
            user_info.setdefault('product_names', {})[url] = name
            user_info.setdefault('previous_prices', {})[url] = prices
            user_info.setdefault('skus', {})[full_sku] = url
            user_info['last_active'] = datetime.now().isoformat()
            save_user_data()

            success_text = (
                f"✅ <b>Товар добавлен!</b>\n"
                f"🏷 {name}\n"
                f"📦 Артикул: <code>{full_sku}</code>\n"
                f"💵 {get_price_display(prices)}₽\n"
                f"🔗 <a href='{url}'>Ссылка на товар</a>\n"
                f"⏱️ Режим проверки: {format_interval(user_info.get('interval', DEFAULT_INTERVAL))}"
            )
            await message.answer(success_text, parse_mode="HTML", disable_web_page_preview=True,
                                reply_markup=ProductMenu.get_main_menu())
            await delete_messages(chat_id, temp_messages)
        except Exception as e:
            await message.answer(f"⚠️ Произошла ошибка: {e}",
                                 reply_markup=ProductMenu.get_main_menu())
    else:
        # Если ссылки Ozоn нет — просто удаляем сообщение без лишнего шума
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass

# =============================================
# ПЛАНИРОВЩИК И ЗАПУСК
# =============================================

async def dynamic_interval_check():
    logger.info("=== Динамическая (по изменению цены) проверка цен ===")
    now = datetime.now()
    for chat_id, user_info in user_data.items():
        if not user_info.get('is_tracking', True):
            continue
        if user_info.get('interval') != 0:
            continue

        last_check_str = user_info.get('last_check')
        last_check = datetime.fromisoformat(last_check_str) if last_check_str else None

        # Проверяем не чаще раза в 50 минут
        if not last_check or (now - last_check) >= timedelta(minutes=25):
            logger.info(f"[dynamic] Проверка {chat_id}, last_check='{last_check_str}'")
            await check_prices(chat_id)
            user_info['last_check'] = now.isoformat()
            save_user_data()


async def scheduled_price_check():
    logger.info("=== Стандартная проверка цен ===")
    now = datetime.now()
    for chat_id, user_info in user_data.items():
        if not user_info.get('is_tracking', True):
            continue

        interval = user_info.get('interval', DEFAULT_INTERVAL)
        if interval == 0:
            continue

        last_check_str = user_info.get('last_check')
        last_check = datetime.fromisoformat(last_check_str) if last_check_str else None

        # Вычисляем следующий ОЖИДАЕМЫЙ чекпоинт
        if not last_check:
            # первая проверка — ставим сейчас
            next_check = now
        else:
            next_check = last_check + timedelta(hours=interval)
            # если бот был выключен — пропускаем "прошедшие" интервалы
            while next_check + timedelta(hours=interval) <= now:
                next_check += timedelta(hours=interval)

        if not last_check or now >= next_check:
            logger.info(f"[scheduled] Проверка {chat_id}, last_check='{last_check_str}', interval={interval}")
            await check_prices(chat_id)
            # ставим следующий чекпоинт (а не now! — чтобы не было дрифта)
            user_info['last_check'] = next_check.isoformat()
            save_user_data()



async def cleanup_inactive_users():
    threshold = datetime.now() - timedelta(days=INACTIVE_USER_THRESHOLD_DAYS)
    inactive_users = [
        chat_id for chat_id, user_info in user_data.items()
        if datetime.fromisoformat(user_info['last_active']) < threshold
    ]

    for chat_id in inactive_users:
        del user_data[chat_id]

    if inactive_users:
        logger.info(f"Удалено {len(inactive_users)} неактивных пользователей")
        save_user_data()

async def main():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(scheduled_price_check, 'interval', minutes=10, jitter=30)

    scheduler.add_job(
        dynamic_interval_check,
        'interval',
        minutes=30,
        jitter=300
    )

    scheduler.add_job(cleanup_inactive_users, 'cron', hour=3)
    scheduler.add_job(update_skus, 'interval', hours=24)

    scheduler.start()

    dp = Dispatcher()
    dp.include_router(router)

    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown()
        save_user_data()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")