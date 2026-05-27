#!/usr/bin/env python3
"""
startup_controller.py
=====================
Скрипт автозапуска для Raspberry Pi.

Поведение:
  1. Бесконечно ожидает появления камеры SIYI A8 mini (UDP 192.168.144.25:37260).
  2. При подключении переводит гимбал в Follow Mode.
  3. Открывает USB-UART порт (по умолчанию /dev/ttyUSB0) и читает MAVLink-пакеты.
  4. Отслеживает сообщение RC_CHANNELS / RC_CHANNELS_RAW.
     Если значение 8-го канала превышает RC_CH8_THRESHOLD (1800), запускает
     panorama_shoot.py (однократно — до сброса канала ниже порога).
  5. При потере связи с камерой возвращается к шагу 1 и снова ждёт подключения.

Зависимости (добавьте в requirements.txt):
    pymavlink>=2.4.40
    pyserial>=3.5

Запуск при старте системы — через systemd:
    Создайте юнит /etc/systemd/system/glider-controller.service
    (пример в конце файла, в строке с SYSTEMD_UNIT_EXAMPLE).
"""

import os
import sys
import time
import socket
import signal
import logging
import subprocess
import threading
from pathlib import Path

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

# SIYI A8 mini
GIMBAL_IP   = "192.168.144.25"
GIMBAL_PORT = 37260
GIMBAL_CONNECT_RETRY_SEC = 5      # пауза между попытками подключения

# USB-UART / MAVLink
MAVLINK_PORT        = "/dev/ttyUSB0"   # путь к устройству (измените при необходимости)
MAVLINK_BAUD        = 57600            # стандартный baudrate для ArduPilot / PX4
MAVLINK_DIALECT     = "ardupilotmega"  # диалект MAVLink

# RC Channel 8 threshold
RC_CH8_THRESHOLD    = 1800    # если ch8 > порога — запускаем панораму
RC_REARM_THRESHOLD  = 1500    # ниже этого значения — сброс блокировки повторного запуска

# Путь к скрипту панорамы (рядом с этим файлом)
SCRIPT_DIR     = Path(__file__).resolve().parent
PANORAMA_SCRIPT = SCRIPT_DIR / "panorama_shoot.py"

# Python-интерпретатор: если есть venv — используем его
_VENV_PYTHON = SCRIPT_DIR / "venv" / "bin" / "python3"
PYTHON_BIN   = str(_VENV_PYTHON) if _VENV_PYTHON.exists() else sys.executable

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("controller")

# ---------------------------------------------------------------------------
# Импорт pymavlink (с понятным сообщением об ошибке)
# ---------------------------------------------------------------------------
try:
    from pymavlink import mavutil
except ImportError:
    log.error("pymavlink не установлен. Выполните: pip install pymavlink")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Импорт SIYI SDK (из того же каталога)
# ---------------------------------------------------------------------------
sys.path.insert(0, str(SCRIPT_DIR))
try:
    from siyi_sdk import SIYICamera
except ImportError:
    log.error("siyi_sdk.py не найден в каталоге %s", SCRIPT_DIR)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Вспомогательные функции: SIYI
# ---------------------------------------------------------------------------

def wait_for_camera() -> SIYICamera:
    """
    Бесконечно пытается подключиться к SIYI A8 mini.
    Возвращает SIYICamera после первого успешного heartbeat-ответа.
    """
    log.info("Ожидание камеры SIYI A8 mini (%s:%d)...", GIMBAL_IP, GIMBAL_PORT)
    while True:
        cam = SIYICamera(ip=GIMBAL_IP, port=GIMBAL_PORT)
        try:
            cam.connect()
            # Проверяем связь — запрашиваем версию прошивки (короткий таймаут)
            fw = cam.get_firmware_version()
            if fw is not None:
                log.info("Камера обнаружена! Прошивка: %s", fw)
                return cam
            # Пустой ответ — камера ещё не готова
            cam.disconnect()
        except Exception as exc:
            log.debug("Камера недоступна: %s", exc)
            try:
                cam.disconnect()
            except Exception:
                pass
        log.info("Камера не отвечает, повтор через %d с...", GIMBAL_CONNECT_RETRY_SEC)
        time.sleep(GIMBAL_CONNECT_RETRY_SEC)


def set_follow_mode(cam: SIYICamera) -> None:
    """Переводит гимбал в Follow Mode и подтверждает это через get_config()."""
    log.info("Установка Follow Mode...")
    cam.set_mode_follow()
    time.sleep(0.5)

    cfg = cam.get_config()
    if cfg:
        mode = cfg.get("motion_mode", "?")
        log.info("Режим гимбала: %s", mode)
        if mode != "follow":
            log.warning("Гимбал не подтвердил Follow Mode (получено: %s)", mode)
    else:
        log.warning("Не удалось прочитать конфигурацию гимбала после смены режима")


def is_camera_alive(cam: SIYICamera) -> bool:
    """Проверяет, отвечает ли камера (лёгкий опрос позиции)."""
    try:
        att = cam.get_attitude()
        return att is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Вспомогательные функции: MAVLink / RC
# ---------------------------------------------------------------------------

def open_mavlink_connection() -> "mavutil.mavfile":
    """
    Открывает MAVLink-соединение через последовательный порт.
    Блокируется до успешного открытия.
    """
    log.info("Подключение к MAVLink на %s (baud=%d)...", MAVLINK_PORT, MAVLINK_BAUD)
    while True:
        try:
            mav = mavutil.mavlink_connection(
                MAVLINK_PORT,
                baud=MAVLINK_BAUD,
                dialect=MAVLINK_DIALECT,
                autoreconnect=True,
            )
            log.info("MAVLink: ожидание heartbeat...")
            mav.wait_heartbeat(timeout=10)
            log.info(
                "MAVLink: heartbeat получен (system=%d, component=%d)",
                mav.target_system,
                mav.target_component,
            )
            return mav
        except Exception as exc:
            log.warning("MAVLink недоступен: %s. Повтор через 5 с...", exc)
            time.sleep(5)


def get_rc_channel8(msg) -> int | None:
    """
    Извлекает значение 8-го RC-канала из MAVLink-сообщения.
    Поддерживает RC_CHANNELS и RC_CHANNELS_RAW.
    Возвращает None, если сообщение не содержит нужных данных.
    """
    msg_type = msg.get_type()

    if msg_type == "RC_CHANNELS":
        # chan8_raw — поле в RC_CHANNELS (MAVLink v2)
        val = getattr(msg, "chan8_raw", None)
        if val is not None and val != 65535:  # 65535 = UINT16_MAX = не подключён
            return int(val)

    elif msg_type == "RC_CHANNELS_RAW":
        # MAVLink v1: chan8_raw тоже присутствует
        val = getattr(msg, "chan8_raw", None)
        if val is not None and val != 65535:
            return int(val)

    return None


# ---------------------------------------------------------------------------
# Запуск panorama_shoot.py
# ---------------------------------------------------------------------------

_panorama_lock = threading.Lock()
_panorama_proc: subprocess.Popen | None = None


def launch_panorama() -> None:
    """
    Запускает panorama_shoot.py в отдельном процессе.
    Если предыдущий запуск ещё идёт — пропускает вызов.
    """
    global _panorama_proc

    with _panorama_lock:
        if _panorama_proc is not None and _panorama_proc.poll() is None:
            log.info("panorama_shoot.py уже выполняется (pid=%d), пропуск", _panorama_proc.pid)
            return

        if not PANORAMA_SCRIPT.exists():
            log.error("Скрипт не найден: %s", PANORAMA_SCRIPT)
            return

        log.info("▶ Запуск panorama_shoot.py (%s)...", PYTHON_BIN)
        _panorama_proc = subprocess.Popen(
            [PYTHON_BIN, str(PANORAMA_SCRIPT)],
            cwd=str(SCRIPT_DIR),
        )
        log.info("panorama_shoot.py запущен, pid=%d", _panorama_proc.pid)


def is_panorama_running() -> bool:
    """Возвращает True, пока дочерний процесс ещё работает."""
    with _panorama_lock:
        return _panorama_proc is not None and _panorama_proc.poll() is None


# ---------------------------------------------------------------------------
# Главный цикл
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("=" * 60)
    log.info("  Glider Panorama — Startup Controller")
    log.info("=" * 60)

    # Корректное завершение при Ctrl-C / SIGTERM
    shutdown_event = threading.Event()

    def _handle_signal(sig, frame):
        log.info("Получен сигнал %s, завершение...", signal.Signals(sig).name)
        shutdown_event.set()

    signal.signal(signal.SIGINT,  _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    while not shutdown_event.is_set():
        # ── Шаг 1: подключение к камере ─────────────────────────────────────
        cam = wait_for_camera()
        if shutdown_event.is_set():
            break

        # ── Шаг 2: Follow Mode ──────────────────────────────────────────────
        set_follow_mode(cam)

        # ── Шаг 3: MAVLink-соединение ───────────────────────────────────────
        mav = open_mavlink_connection()
        if shutdown_event.is_set():
            break

        log.info("Мониторинг RC Channel 8 (порог: %d)...", RC_CH8_THRESHOLD)

        # Флаг: был ли уже активирован триггер (ждём сброса канала перед повтором)
        triggered = False
        camera_check_interval = 30   # проверять камеру каждые N итераций
        iteration = 0

        # ── Шаг 4: основной цикл чтения RC ──────────────────────────────────
        while not shutdown_event.is_set():
            # Проверяем камеру периодически
            iteration += 1
            if iteration % camera_check_interval == 0:
                if not is_camera_alive(cam):
                    log.warning("Камера не отвечает — возврат к ожиданию подключения")
                    break

            # Читаем MAVLink-сообщение (таймаут 1 с)
            try:
                msg = mav.recv_match(
                    type=["RC_CHANNELS", "RC_CHANNELS_RAW"],
                    blocking=True,
                    timeout=1.0,
                )
            except Exception as exc:
                log.warning("Ошибка чтения MAVLink: %s", exc)
                time.sleep(1)
                continue

            if msg is None:
                # Таймаут — нет RC-пакета. Не критично.
                continue

            ch8 = get_rc_channel8(msg)
            if ch8 is None:
                continue

            log.debug("RC ch8 = %d  (triggered=%s)", ch8, triggered)

            if ch8 > RC_CH8_THRESHOLD and not triggered:
                log.info(
                    "RC ch8=%d > %d → запуск панорамы!", ch8, RC_CH8_THRESHOLD
                )
                triggered = True
                launch_panorama()

            elif ch8 <= RC_REARM_THRESHOLD and triggered:
                log.info("RC ch8=%d ≤ %d → сброс триггера (готов к повторному запуску)",
                         ch8, RC_REARM_THRESHOLD)
                triggered = False

        # Закрываем MAVLink перед перезапуском цикла
        try:
            mav.close()
        except Exception:
            pass

        # Закрываем камеру
        try:
            cam.disconnect()
        except Exception:
            pass

    log.info("Контроллер завершён.")


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# SYSTEMD_UNIT_EXAMPLE
# ---------------------------------------------------------------------------
# Сохраните следующее в /etc/systemd/system/glider-controller.service
# и выполните: sudo systemctl enable --now glider-controller.service
#
# [Unit]
# Description=Glider Panorama Startup Controller
# After=network.target
# Wants=network.target
#
# [Service]
# Type=simple
# User=pi
# WorkingDirectory=/home/pi/glider_panorama
# ExecStart=/home/pi/glider_panorama/venv/bin/python3 /home/pi/glider_panorama/startup_controller.py
# Restart=always
# RestartSec=5
# StandardOutput=journal
# StandardError=journal
#
# [Install]
# WantedBy=multi-user.target
# ---------------------------------------------------------------------------
