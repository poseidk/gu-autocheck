import json
import logging
import os
import random
import sys
import threading
import time
import traceback
from pathlib import Path

import httpx
import pyotp
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

CONFIG_FILE = Path("config.json")
COOKIES_FILE = Path("cookies.json")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)

stop_event = threading.Event()
force_check_event = threading.Event()
force_refresh_event = threading.Event()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(levelname)s] [%(filename)s:%(lineno)d] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        logger.critical("❌ Файл config.json не найден! Запустите: python master.py")
        sys.exit(1)
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.critical(f"❌ Ошибка чтения config.json: {e}")
        sys.exit(1)


def console_listener():
    print("\nУправление: [c] Проверить | [r] Обновить куки | [q] Выход | [h] Справка\n")
    while not stop_event.is_set():
        try:
            cmd = input().strip().lower()
            if cmd in ["q", "quit", "exit"]:
                logger.info("⏳ Завершение работы...")
                stop_event.set()
                break
            elif cmd in ["c", "check"]:
                logger.info("🔄 Вызов внеочередной проверки...")
                force_check_event.set()
            elif cmd in ["r", "refresh"]:
                logger.info("🔄 Вызов принудительного обновления куки...")
                force_refresh_event.set()
            elif cmd in ["h", "help"]:
                print("\n[c] Check | [r] Refresh cookies | [q] Quit | [h] Help\n")
        except (EOFError, KeyboardInterrupt):
            stop_event.set()
            break


def send_notification(config: dict, message: str, title: str = "GIBDD SLOTS", priority: str = "high", tags: str = "car,warning"):
    ntfy_url = config.get("ntfy_url")
    if not ntfy_url:
        return
    try:
        httpx.post(
            ntfy_url,
            content=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": tags},
            timeout=10.0,
        )
        logger.info(f"✅ Уведомление ntfy отправлено ({title})")
    except Exception as e:
        logger.error(f"❌ Ошибка отправки ntfy: {e}")


def human_type(element, text: str):
    for char in text:
        element.send_keys(char)
        time.sleep(random.uniform(0.08, 0.22))


def save_cookies_to_file(cookies: dict):
    try:
        data = {"timestamp": time.time(), "cookies": cookies}
        COOKIES_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info(f"✅ Куки сохранены в '{COOKIES_FILE}'")
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения куки: {e}")


def load_cookies_from_file() -> dict | None:
    if not COOKIES_FILE.exists():
        return None
    try:
        data = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
        cookies = data.get("cookies", {})
        if cookies:
            saved_at = time.strftime("%H:%M:%S", time.localtime(data.get("timestamp", 0)))
            logger.info(f"✅ Загружены куки из '{COOKIES_FILE}' ({saved_at})")
            return cookies
    except Exception as e:
        logger.warning(f"⚠️ Ошибка чтения файла куки: {e}")
    return None


def handle_draft_modal(driver):
    try:
        continue_btn = WebDriverWait(driver, 6).until(
            EC.element_to_be_clickable((
                By.XPATH,
                "//button[.//span[contains(text(), 'Продолжить')] or contains(normalize-space(), 'Продолжить')]"
            ))
        )
        time.sleep(random.uniform(0.3, 0.7))
        continue_btn.click()
        logger.info("✅ Черновик продолжен.")
    except TimeoutException:
        pass


def check_login_errors(driver, config: dict) -> bool:
    error_xpaths = [
        "//*[contains(text(), 'Неверный логин или пароль') or contains(text(), 'Введен неверный пароль')]",
        "//*[contains(text(), 'Неверный код') or contains(text(), 'Код не подходит')]"
    ]
    for xpath in error_xpaths:
        try:
            for elem in driver.find_elements(By.XPATH, xpath):
                if elem.is_displayed() and elem.text.strip():
                    msg = elem.text.strip()
                    logger.critical(f"❌ Ошибка ЕСИА: '{msg}'")
                    send_notification(config, f"Oshibka vhoda: {msg}", "AUTH ERROR", "urgent", "x,no_entry")
                    return True
        except Exception:
            pass
    return False


def wait_for_slots_network_event(driver, timeout: int = 40) -> bool:
    end_time = time.time() + timeout
    while time.time() < end_time and not stop_event.is_set():
        try:
            for entry in driver.get_log("performance"):
                message = json.loads(entry["message"])["message"]
                if message.get("method") == "Network.responseReceived":
                    resp = message.get("params", {}).get("response", {})
                    if "slots" in resp.get("url", "").lower() and resp.get("status") == 200:
                        logger.info("✅ Перехвачен ответ API slots.")
                        return True
        except Exception:
            pass
        time.sleep(0.8)
    return False


def fetch_cookies_via_selenium(config: dict) -> dict | None:
    logger.info("⏳ Запуск Chromium...")
    driver = None
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(f"user-agent={USER_AGENT}")

    binary_path = os.getenv("CHROME_BIN", "/usr/bin/chromium")
    if os.path.exists(binary_path):
        options.binary_location = binary_path

    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    try:
        driver = webdriver.Chrome(options=options)
        wait = WebDriverWait(driver, 20)
        driver.get(config.get("target_url", "https://www.gosuslugi.ru/600825/1/form"))

        try:
            qr = WebDriverWait(driver, 4).until(
                EC.presence_of_element_located((By.XPATH, "//div[contains(@class, 'qr-container')] | //h1[contains(text(), 'QR')]"))
            )
            if qr:
                driver.find_element(By.XPATH, "//button[contains(., 'логин') or contains(., 'пароль')] | //a[contains(., 'логин')]").click()
                time.sleep(1.0)
        except TimeoutException:
            pass

        login_in = wait.until(EC.element_to_be_clickable((By.XPATH, "//input[@id='login' or @name='login' or @type='text']")))
        login_in.clear()
        human_type(login_in, config.get("gos_login", ""))

        pass_in = wait.until(EC.element_to_be_clickable((By.XPATH, "//input[@id='password' or @type='password']")))
        pass_in.clear()
        human_type(pass_in, config.get("gos_password", ""))

        driver.find_element(By.XPATH, "//button[contains(normalize-space(), 'Войти') or @type='submit']").click()
        time.sleep(2.0)

        if check_login_errors(driver, config):
            return None

        totp_secret = config.get("totp_secret", "")
        if totp_secret:
            try:
                first_code_in = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, "//input[@id='code' or @autocomplete='one-time-code' or @type='tel' or @maxlength='1']"))
                )
                code_inputs = driver.find_elements(By.XPATH, "//input[@id='code' or @autocomplete='one-time-code' or @type='tel' or @maxlength='1']")
                code = pyotp.TOTP(totp_secret).now()

                if len(code_inputs) >= 6:
                    for i in range(6):
                        code_inputs[i].click()
                        code_inputs[i].send_keys(code[i])
                        time.sleep(0.1)
                else:
                    first_code_in.click()
                    human_type(first_code_in, code)

                time.sleep(2.0)
                if check_login_errors(driver, config):
                    return None
            except TimeoutException:
                pass

        WebDriverWait(driver, 35).until(lambda d: "esia.gosuslugi.ru" not in d.current_url)
        handle_draft_modal(driver)
        wait_for_slots_network_event(driver, timeout=40)

        raw_cookies = driver.get_cookies()
        if not raw_cookies:
            return None

        fresh_cookies = {c["name"]: c["value"] for c in raw_cookies}
        save_cookies_to_file(fresh_cookies)
        return fresh_cookies

    except Exception as e:
        logger.error(f"❌ Ошибка Selenium: {e}")
        return None
    finally:
        if driver:
            driver.quit()


def check_slots(client: httpx.Client, config: dict) -> bool:
    logger.info(f"[{time.strftime('%H:%M:%S')}] ⏳ Запрос к API slots...")
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://www.gosuslugi.ru",
        "Referer": config.get("target_url"),
        "x-form-id": str(config.get("form_id", "600825/1")),
        "x-order-id": str(config.get("order_id", "")),
    }

    try:
        resp = client.post(
            config.get("api_slots_url", "https://www.gosuslugi.ru/api/lk/v1/equeue/agg/slots"),
            headers=headers,
            json=config.get("json_data", {}),
            timeout=20.0
        )

        if resp.status_code in [401, 403]:
            logger.warning(f"⚠️ Сессия просрочена ({resp.status_code}).")
            send_notification(config, "Sessiya umerla! Obnovlyayu cookies...", "CRITICAL ERROR", "urgent", "no_entry")
            return False

        if resp.status_code != 200:
            return True

        slots = resp.json().get("slots", [])
        if not slots:
            logger.info("⏳ Слотов нет.")
            return True

        found_slots = []
        for slot in slots:
            vt = slot.get("visitTime", "")
            if vt:
                found_slots.append(vt.replace("T", " в ")[:16])

        if found_slots:
            msg = "ПОЯВИЛИСЬ СВОБОДНЫЕ СЛОТЫ!\n\n" + "\n".join(sorted(list(set(found_slots))))
            logger.info(f"✅ {msg}")
            send_notification(config, msg, "GIBDD SLOTS FOUND!", "urgent", "car,warning")

        return True

    except Exception as exc:
        logger.error(f"❌ Ошибка сети API: {exc}")
        return True


def run_checker():
    config = load_config()
    logger.info("=== Чекер ГИБДД запущен ===")
    send_notification(config, "Checker uspeshno zapushen!", "START RABOTY", "default", "rocket")

    cookies = load_cookies_from_file() or fetch_cookies_via_selenium(config)
    if not cookies:
        logger.critical("❌ Авторизация не удалась. Завершение.")
        return

    threading.Thread(target=console_listener, daemon=True).start()

    with httpx.Client(cookies=cookies, follow_redirects=True) as client:
        while not stop_event.is_set():
            if force_refresh_event.is_set():
                force_refresh_event.clear()
                new_cookies = fetch_cookies_via_selenium(config)
                if new_cookies:
                    client.cookies.update(new_cookies)

            if not check_slots(client, config):
                new_cookies = fetch_cookies_via_selenium(config)
                if new_cookies:
                    client.cookies.update(new_cookies)
                else:
                    if stop_event.wait(300):
                        break
                    continue

            if stop_event.is_set():
                break

            sleep_time = random.uniform(config.get("check_interval_min", 300), config.get("check_interval_max", 900))
            logger.info(f"⏳ Ожидание {round(sleep_time / 60, 1)} мин...")

            start_sleep = time.time()
            while time.time() - start_sleep < sleep_time:
                if stop_event.is_set() or force_check_event.is_set() or force_refresh_event.is_set():
                    force_check_event.clear()
                    break
                time.sleep(0.5)


if __name__ == "__main__":
    run_checker()