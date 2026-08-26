import json
import os
import sys
import time
from pathlib import Path

import pyotp
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

CONFIG_FILE = Path("config.json")
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)


def print_banner():
    print("=" * 60)
    print("     Настройка и конфигурация автопроверки госуслуг")
    print("=" * 60)


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_config(config: dict):
    try:
        CONFIG_FILE.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n✅ Конфигурация успешно сохранена в '{CONFIG_FILE}'")
    except Exception as e:
        print(f"\n❌ Ошибка сохранения файла: {e}")


def display_summary(config: dict):
    print("\n---------------- ТЕКУЩАЯ КОНФИГУРАЦИЯ ----------------")
    print(f"Логин:          {config.get('gos_login', 'не задан')}")
    print(f"Пароль:         {'******' if config.get('gos_password') else 'не задан'}")
    print(f"TOTP 2FA:       {'настроен' if config.get('totp_secret') else 'отключен'}")
    print(f"ntfy URL:       {config.get('ntfy_url', 'не задан')}")
    print(f"Интервалы:      от {config.get('check_interval_min', 300)}s до {config.get('check_interval_max', 900)}s")
    print(f"Order ID:       {config.get('order_id', 'не перехвачен')}")
    print(f"API Slots URL:  {config.get('api_slots_url', 'не перехвачен')}")
    print("------------------------------------------------------")


def wizard_edit_credentials(config: dict) -> dict:
    print("\n--- [1] Учетные данные и уведомления ---")
    cur_login = config.get("gos_login", "")
    config["gos_login"] = input(f"Логин Госуслуги (телефон/email/СНИЛС) [{cur_login}]: ").strip() or cur_login

    cur_pass = config.get("gos_password", "")
    config["gos_password"] = input(f"Пароль [{'*' * len(cur_pass) if cur_pass else ''}]: ").strip() or cur_pass

    cur_totp = config.get("totp_secret", "")
    config["totp_secret"] = input(f"TOTP Secret 2FA (пусто, если 2FA выключен) [{cur_totp}]: ").strip() or cur_totp

    cur_ntfy = config.get("ntfy_url", "https://ntfy.sh/gibdd_slots")
    config["ntfy_url"] = input(f"URL темы ntfy [{cur_ntfy}]: ").strip() or cur_ntfy

    return config


def wizard_edit_intervals(config: dict) -> dict:
    print("\n--- [2] Настройки интервалов проверки ---")
    cur_min = config.get("check_interval_min", 300)
    val_min = input(f"Минимальная пауза (сек) [{cur_min}]: ").strip()
    config["check_interval_min"] = int(val_min) if val_min.isdigit() else cur_min

    cur_max = config.get("check_interval_max", 900)
    val_max = input(f"Максимальная пауза (сек) [{cur_max}]: ").strip()
    config["check_interval_max"] = int(val_max) if val_max.isdigit() else cur_max

    config["target_url"] = config.get("target_url", "https://www.gosuslugi.ru/600825/1/form")
    return config


def capture_network_params(config: dict) -> dict:
    print("\n⏳ Запуск Chromium для перехвата параметров отделения...")
    options = webdriver.ChromeOptions()
    options.add_argument("--window-size=1920,1080")
    options.add_argument(f"user-agent={USER_AGENT}")
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    binary_path = os.getenv("CHROME_BIN", "/usr/bin/chromium")
    if not os.path.exists(binary_path):
        binary_path = "/usr/bin/chromium-browser"
    if os.path.exists(binary_path):
        options.binary_location = binary_path

    driver = None
    try:
        driver = webdriver.Chrome(options=options)
        wait = WebDriverWait(driver, 20)
        target_url = config.get("target_url", "https://www.gosuslugi.ru/600825/1/form")

        print(f"👉 Открытие страницы: {target_url}")
        driver.get(target_url)

        gos_login = config.get("gos_login")
        gos_password = config.get("gos_password")
        totp_secret = config.get("totp_secret")

        if gos_login and gos_password:
            try:
                login_in = wait.until(EC.element_to_be_clickable((By.XPATH, "//input[@id='login' or @name='login' or @type='text']")))
                login_in.clear()
                login_in.send_keys(gos_login)

                pass_in = wait.until(EC.element_to_be_clickable((By.XPATH, "//input[@id='password' or @type='password']")))
                pass_in.clear()
                pass_in.send_keys(gos_password)

                driver.find_element(By.XPATH, "//button[contains(normalize-space(), 'Войти') or @type='submit']").click()
                time.sleep(2)

                if totp_secret:
                    try:
                        code_in = WebDriverWait(driver, 8).until(
                            EC.element_to_be_clickable((By.XPATH, "//input[@id='code' or @autocomplete='one-time-code' or @type='tel' or @maxlength='1']"))
                        )
                        code = pyotp.TOTP(totp_secret).now()
                        code_in.send_keys(code)
                    except Exception:
                        pass
            except Exception as e:
                print(f"⚠️ Авторизация потребовала ручного участия: {e}")

        print("\n⏳ Ожидание выбора отделения пользователем в браузере (до 3 минут)...")
        captured = False
        end_time = time.time() + 180

        while time.time() < end_time:
            logs = driver.get_log("performance")
            for entry in logs:
                try:
                    msg = json.loads(entry["message"])["message"]
                    if msg.get("method") == "Network.requestWillBeSent":
                        req = msg.get("params", {}).get("request", {})
                        url = req.get("url", "")
                        if "slots" in url.lower() and req.get("method") == "POST":
                            post_data_str = req.get("postData")
                            headers = req.get("headers", {})

                            if post_data_str:
                                post_json = json.loads(post_data_str)
                                config["json_data"] = post_json
                                config["order_id"] = headers.get("x-order-id") or headers.get("X-Order-Id") or post_json.get("caseNumber")
                                config["form_id"] = headers.get("x-form-id") or headers.get("X-Form-Id") or "600825/1"
                                config["api_slots_url"] = url
                                print("✅ Параметры API slots успешно перехвачены!")
                                captured = True
                                break
                except Exception:
                    pass
            if captured:
                break
            time.sleep(1)

        if not captured:
            print("❌ Запрос slots не зафиксирован.")
    except Exception as e:
        print(f"❌ Ошибка во время выполнения Selenium: {e}")
    finally:
        if driver:
            driver.quit()
    return config


def main():
    print_banner()
    config = load_config()

    if config:
        print("ℹ️ Найден существующий файл конфигурации.")
        display_summary(config)

    while True:
        print("\nГЛАВНОЕ МЕНЮ:")
        print("  1. Посмотреть текущую конфигурацию")
        print("  2. Изменить логин, пароль, 2FA и ntfy")
        print("  3. Изменить интервалы проверки")
        print("  4. Запустить Chromium для перехвата параметров отделения (slots)")
        print("  5. Полный мастер настройки с нуля")
        print("  6. Сохранить и выйти")
        print("  0. Выйти без сохранения")

        choice = input("\nВыберите действие [0-6]: ").strip()

        if choice == "1":
            display_summary(config)
        elif choice == "2":
            config = wizard_edit_credentials(config)
            save_config(config)
        elif choice == "3":
            config = wizard_edit_intervals(config)
            save_config(config)
        elif choice == "4":
            config = capture_network_params(config)
            save_config(config)
        elif choice == "5":
            config = {}
            config = wizard_edit_credentials(config)
            config = wizard_edit_intervals(config)
            print("\nЗапустить Chromium прямо сейчас для выборa отделения? (y/n)")
            if input("> ").strip().lower() in ["y", "yes", "д", "да"]:
                config = capture_network_params(config)
            save_config(config)
        elif choice == "6":
            save_config(config)
            print("👋 Настройка завершена.")
            break
        elif choice == "0":
            print("👋 Выход.")
            break
        else:
            print("❌ Неверный ввод.")


if __name__ == "__main__":
    main()