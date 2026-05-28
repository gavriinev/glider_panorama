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
import json
import time
import socket
import signal
import logging
import subprocess
import threading
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

# SIYI A8 mini
GIMBAL_IP   = "192.168.144.25"
GIMBAL_PORT = 37260
GIMBAL_CONNECT_RETRY_SEC = 5      # пауза между попытками подключения

# Удержание горизонта и нулевого Yaw (Lock Mode)
YAW_TARGET            = 0.0   # целевой угол Yaw (градусы)
YAW_CORRECT_INTERVAL  = 2.0   # интервал проверки и коррекции Yaw (сек)
YAW_CORRECT_TOLERANCE = 3.0   # допуск: не отправлять команду, если ошибка < N°

# MAVLink — целевой узел
MAV_TARGET_SYSTEM    = 1   # System ID автопилота
MAV_TARGET_COMPONENT = 1   # Component ID (главный контроллер)

# USB-UART / MAVLink
MAVLINK_PORT        = "/dev/ttyUSB0"   # путь к устройству (измените при необходимости)
MAVLINK_BAUD        = 57600            # стандартный baudrate для ArduPilot / PX4
MAVLINK_DIALECT     = "ardupilotmega"  # диалект MAVLink

# RC Channel 8 threshold
RC_CH8_THRESHOLD    = 1800    # если ch8 > порога — запускаем панораму
RC_REARM_THRESHOLD  = 1500    # ниже этого значения — сброс блокировки повторного запуска

# Чтение RC-пакетов из потока
RC_POLL_INTERVAL    = 0.1     # пауза между проверками RC из потока (0.5 раз/сек)
RC_REPLY_TIMEOUT    = 0.1     # таймаут recv_match (стрим 10 Hz → пакет за 0.1 с)

# Телеметрия во время съёмки
TELEMETRY_MSGS = {
    "AHRS2":    178,  # roll, pitch, yaw, altitude, lat, lng
    "ATTITUDE":  30,  # roll, pitch, yaw + скорости
    "VFR_HUD":   74,  # airspeed, groundspeed, heading, throttle, alt, climb
}
TELEMETRY_POLL_HZ   = 5       # частота записи телеметрии (Hz)
TELEMETRY_FILENAME  = "telemetry.json"  # имя файла в папке с фото

# Путь к скрипту панорамы (рядом с этим файлом)
SCRIPT_DIR      = Path(__file__).resolve().parent
PANORAMA_SCRIPT = SCRIPT_DIR / "panorama_shoot.py"
STITCH_SCRIPT   = SCRIPT_DIR / "stitch_panorama.py"

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


def set_level_lock_mode(cam: SIYICamera) -> None:
    """
    Устанавливает Lock Mode (стабилизация горизонта) и
    возвращает гимбал в нулевое положение (Yaw=0°, Pitch=0°).
    В Lock Mode гимбал удерживает абсолютный угол независимо
    от движения платформы.
    """
    log.info("Установка Lock Mode (горизонт, Yaw=0°)...")
    cam.set_mode_lock()
    time.sleep(0.5)

    # Принудительно выставляем Yaw=0, Pitch=0
    cam.set_angle(yaw=YAW_TARGET, pitch=0.0)
    time.sleep(0.5)

    cfg = cam.get_config()
    if cfg:
        mode = cfg.get("motion_mode", "?")
        log.info("Режим гимбала: %s", mode)
        if mode != "lock":
            log.warning("Гимбал не подтвердил Lock Mode (получено: %s)", mode)
    else:
        log.warning("Не удалось прочитать конфигурацию гимбала после смены режима")


def yaw_correction_worker(cam: SIYICamera, stop_event: threading.Event) -> None:
    """
    Фоновый поток: периодически считывает текущий Yaw и корректирует
    его обратно к YAW_TARGET, если отклонение превышает YAW_CORRECT_TOLERANCE.
    Завершается при установке stop_event.
    """
    log.info("Запуск фонового потока коррекции Yaw (цель=%.1f°, интервал=%.1fс).",
             YAW_TARGET, YAW_CORRECT_INTERVAL)
    while not stop_event.wait(timeout=YAW_CORRECT_INTERVAL):
        try:
            att = cam.get_attitude()
            if att is None:
                log.debug("[YAW] Нет ответа от гимбала, пропуск.")
                continue
            err = att.yaw - YAW_TARGET
            log.debug("[YAW] текущий=%.1f°, цель=%.1f°, ошибка=%.1f°",
                      att.yaw, YAW_TARGET, err)
            if abs(err) > YAW_CORRECT_TOLERANCE:
                log.info("[YAW] Коррекция: %.1f° → %.1f°", att.yaw, YAW_TARGET)
                cam.set_angle(yaw=YAW_TARGET, pitch=0.0)
        except Exception as exc:
            log.warning("[YAW] Ошибка потока коррекции: %s", exc)
    log.info("Поток коррекции Yaw завершён.")


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
    После heartbeat запрашивает стриминг RC-каналов через REQUEST_DATA_STREAM.
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
            hb = mav.wait_heartbeat(timeout=30)
            if hb is None:
                log.warning("MAVLink: heartbeat не получен (таймаут). Повтор...")
                time.sleep(2)
                continue
            log.info(
                "MAVLink: heartbeat получен (system=%d, component=%d)",
                mav.target_system, mav.target_component,
            )

            # RC-каналы: 10 Hz
            mav.mav.request_data_stream_send(
                MAV_TARGET_SYSTEM, MAV_TARGET_COMPONENT,
                mavutil.mavlink.MAV_DATA_STREAM_RC_CHANNELS,
                10, 1,
            )
            # ATTITUDE (roll/pitch/yaw): 5 Hz — MAV_DATA_STREAM_EXTRA1
            mav.mav.request_data_stream_send(
                MAV_TARGET_SYSTEM, MAV_TARGET_COMPONENT,
                mavutil.mavlink.MAV_DATA_STREAM_EXTRA1,
                5, 1,
            )
            # VFR_HUD (airspeed/alt/climb): 5 Hz — MAV_DATA_STREAM_EXTRA2
            mav.mav.request_data_stream_send(
                MAV_TARGET_SYSTEM, MAV_TARGET_COMPONENT,
                mavutil.mavlink.MAV_DATA_STREAM_EXTRA2,
                5, 1,
            )
            # AHRS2 (filtered attitude + position): 5 Hz — MAV_DATA_STREAM_EXTRA3
            mav.mav.request_data_stream_send(
                MAV_TARGET_SYSTEM, MAV_TARGET_COMPONENT,
                mavutil.mavlink.MAV_DATA_STREAM_EXTRA3,
                5, 1,
            )
            log.info(
                "MAVLink: запрошены RC (10 Hz) + ATTITUDE/VFR_HUD/AHRS2 (5 Hz)"
            )
            return mav
        except Exception as exc:
            log.warning("MAVLink недоступен: %s. Повтор через 5 с...", exc)
            time.sleep(5)


# MAVLink message IDs для RC
_RC_CHANNELS_MSG_ID     = 65   # RC_CHANNELS     (MAVLink v2)
_RC_CHANNELS_RAW_MSG_ID = 35   # RC_CHANNELS_RAW (MAVLink v1)


def verify_telemetry_streams(mav, timeout: float = 3.0) -> bool:
    """
    Проверяет, что все типы сообщений из TELEMETRY_MSGS, а также RC_CHANNELS
    действительно поступают в MAVLink-поток в течение timeout секунд.
    Записывает результат проверки в лог.
    Возвращает True, если все потоки активны.
    """
    # Типы которые хотим увидеть
    wanted = list(TELEMETRY_MSGS.keys()) + ["RC_CHANNELS", "RC_CHANNELS_RAW"]
    received: dict[str, bool] = {t: False for t in TELEMETRY_MSGS}
    received["RC"] = False

    log.info("[CHK] Проверка MAVLink-потоков (%.1f с)...", timeout)
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline and not all(received.values()):
        with _mav_lock:
            msg = mav.recv_match(type=wanted, blocking=True,
                                 timeout=deadline - time.monotonic())
        if msg is None:
            break
        mtype = msg.get_type()
        if mtype in received:
            received[mtype] = True
        elif mtype in ("RC_CHANNELS", "RC_CHANNELS_RAW"):
            received["RC"] = True

    ok = all(received.values())
    for name, got in received.items():
        mark = "✓" if got else "✗"
        level = log.info if got else log.warning
        level("[CHK] %s %s", mark, name)
    if ok:
        log.info("[CHK] Все потоки активны")
    else:
        log.warning("[CHK] Некоторые потоки отсутствуют — телеметрия будет неполной")
    return ok


def recv_rc_channels(mav: "mavutil.mavfile") -> "mavutil.mavlink.MAVLink_message | None":
    """
    Читает очередной пакет RC_CHANNELS из MAVLink-потока.
    Автопилот сам отправляет эти пакеты по подписке REQUEST_DATA_STREAM (10 Hz).
    Использует _mav_lock для защиты от одновременного доступа из потока телеметрии.
    Возвращает принятый пакет или None при таймауте.
    """
    with _mav_lock:
        msg = mav.recv_match(
            type=["RC_CHANNELS", "RC_CHANNELS_RAW"],
            blocking=True,
            timeout=RC_REPLY_TIMEOUT,
        )
    if msg is None:
        log.info("[RC] Пакет не получен за %.1f с (автопилот не шлёт RC_CHANNELS?)", RC_REPLY_TIMEOUT)
    else:
        log.debug("[RC] Получен %s", msg.get_type())
    return msg



def get_rc_channel8(msg) -> int | None:
    """
    Извлекает значение 8-го RC-канала из MAVLink-сообщения.
    Поддерживает RC_CHANNELS и RC_CHANNELS_RAW.
    Возвращает None, если сообщение не содержит нужных данных.
    """
    msg_type = msg.get_type()

    if msg_type == "RC_CHANNELS":
        val = getattr(msg, "chan8_raw", None)
        if val is not None and val != 65535:  # 65535 = UINT16_MAX = не подключён
            return int(val)

    elif msg_type == "RC_CHANNELS_RAW":
        val = getattr(msg, "chan8_raw", None)
        if val is not None and val != 65535:
            return int(val)

    return None


# ---------------------------------------------------------------------------
# Телеметрия во время съёмки
# ---------------------------------------------------------------------------

# Общий лок для доступа к MAVLink-соединению из нескольких потоков
_mav_lock = threading.Lock()


def _request_msg(mav, msg_id: int, msg_type: str, timeout: float = 1.5):
    """
    Пассивно получает msg_type из MAVLink-потока (thread-safe).
    Автопилот сам шлёт эти сообщения через REQUEST_DATA_STREAM.
    """
    with _mav_lock:
        return mav.recv_match(type=msg_type, blocking=True, timeout=timeout)


def telemetry_logger(
    mav,
    output_dir: Path,
    panorama_proc: subprocess.Popen,
    stop_event: threading.Event,
) -> None:
    """
    Фоновый поток: запрашивает AHRS2 / ATTITUDE / VFR_HUD через MAVLink
    и пишет записи пока идёт съёмка (процесс не завершён).
    Сохраняет JSON-файл в output_dir/telemetry.json.
    """
    interval = 1.0 / TELEMETRY_POLL_HZ
    records = []

    log.info("[TEL] Начало записи телеметрии в %s", output_dir / TELEMETRY_FILENAME)

    while not stop_event.is_set() and panorama_proc.poll() is None:
        record = {"ts": datetime.utcnow().isoformat(timespec="milliseconds") + "Z"}

        for msg_type, msg_id in TELEMETRY_MSGS.items():
            try:
                msg = _request_msg(mav, msg_id, msg_type)
                if msg is not None:
                    record[msg_type] = msg.to_dict()
                    # Убираем служебные поля mavlink
                    record[msg_type].pop("mavpackettype", None)
            except Exception as exc:
                log.debug("[TEL] %s: %s", msg_type, exc)

        records.append(record)
        log.debug("[TEL] Запись: %s", record)
        time.sleep(interval)

    # Сохраняем файл
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = output_dir / TELEMETRY_FILENAME
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, ensure_ascii=False)
        log.info("[TEL] Телеметрия сохранена: %s  (%d записей)", out_path, len(records))
    except Exception as exc:
        log.error("[TEL] Ошибка сохранения телеметрии: %s", exc)


# ---------------------------------------------------------------------------
# Запуск panorama_shoot.py
# ---------------------------------------------------------------------------

_panorama_lock = threading.Lock()
_panorama_proc: subprocess.Popen | None = None


def launch_panorama(
    mav,
    output_dir: Path,
    cam,
    yaw_stop: threading.Event,
    yaw_thread: threading.Thread,
) -> None:
    """
    Запускает panorama_shoot.py, передав output_dir.
    До съёмки останавливает поток коррекции Yaw.
    После завершения панорамы перезапускает его.
    Параллельно запускает поток записи телеметрии.
    Если предыдущий запуск ещё идёт — пропускает вызов.
    """
    global _panorama_proc

    with _panorama_lock:
        if _panorama_proc is not None and _panorama_proc.poll() is None:
            log.info("Съёмка уже идёт (pid=%d), пропуск", _panorama_proc.pid)
            return

        if not PANORAMA_SCRIPT.exists():
            log.error("Скрипт не найден: %s", PANORAMA_SCRIPT)
            return

        # ── Останавливаем коррекцию Yaw на время съёмки ────────────────
        log.info("[YAW] Остановка коррекции Yaw на время панорамы...")
        yaw_stop.set()
        yaw_thread.join(timeout=10)

        # ── Запуск панорамы ────────────────────────────────────────────
        output_dir.mkdir(parents=True, exist_ok=True)
        log.info("▶ Запуск panorama_shoot.py → %s", output_dir)
        _panorama_proc = subprocess.Popen(
            [PYTHON_BIN, str(PANORAMA_SCRIPT), "--output-dir", str(output_dir)],
            cwd=str(SCRIPT_DIR),
        )
        log.info("panorama_shoot.py запущен, pid=%d", _panorama_proc.pid)

        # ── Телеметрия ─────────────────────────────────────────────────
        tel_stop = threading.Event()
        tel_thread = threading.Thread(
            target=telemetry_logger,
            args=(mav, output_dir, _panorama_proc, tel_stop),
            daemon=True,
        )
        tel_thread.start()
        log.info("[TEL] Поток телеметрии запущен")

        # ── Watcher: запускает сшивку и восстанавливает Yaw после съёмки ─────────
        proc_ref   = _panorama_proc
        outdir_ref = output_dir

        def _yaw_restarter():
            proc_ref.wait()   # ждём завершения panorama_shoot.py
            rc = proc_ref.returncode
            log.info("[PAN] panorama_shoot.py завершён (code=%d)", rc)

            # ── Запуск сшивки ────────────────────────────────────────
            if STITCH_SCRIPT.exists():
                log.info("▶ Запуск stitch_panorama.py --auto-cp для %s", outdir_ref)
                try:
                    stitch_proc = subprocess.Popen(
                        [
                            PYTHON_BIN, str(STITCH_SCRIPT),
                            "--auto-cp",
                            str(outdir_ref),
                        ],
                        cwd=str(SCRIPT_DIR),
                    )
                    stitch_proc.wait()  # ждём завершения
                    log.info(
                        "[STITCH] Завершён (code=%d)", stitch_proc.returncode
                    )
                except Exception as exc:
                    log.error("[STITCH] Ошибка запуска: %s", exc)
            else:
                log.warning("[STITCH] Скрипт не найден: %s", STITCH_SCRIPT)

            # ── Восстановка коррекции Yaw ───────────────────────────
            log.info("[YAW] Восстановка коррекции Yaw...")
            yaw_stop.clear()
            new_thread = threading.Thread(
                target=yaw_correction_worker,
                args=(cam, yaw_stop),
                daemon=True,
            )
            new_thread.start()
            log.info("[YAW] Поток коррекции Yaw возобновлён")

        threading.Thread(target=_yaw_restarter, daemon=True).start()



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

        # ── Шаг 2: Lock Mode + Yaw=0, горизонтальное положение ────────────
        set_level_lock_mode(cam)

        # ── Шаг 2а: фоновый поток коррекции Yaw ─────────────────────────────
        yaw_stop = threading.Event()
        yaw_thread = threading.Thread(
            target=yaw_correction_worker, args=(cam, yaw_stop), daemon=True
        )
        yaw_thread.start()

        # ── Шаг 3: MAVLink-соединение ───────────────────────────────────────
        mav = open_mavlink_connection()
        if shutdown_event.is_set():
            break

        # ── Проверка потоков телеметрии и RC ────────────────────────────────
        verify_telemetry_streams(mav)

        log.info("Мониторинг RC Channel 8 (порог: %d)...", RC_CH8_THRESHOLD)

        # Флаг: был ли уже активирован триггер (ждём сброса канала перед повтором)
        triggered = False
        camera_check_interval = 30   # проверять камеру каждые N итераций
        iteration = 0

        # ── Шаг 4: основной цикл запроса RC ─────────────────────────────────
        while not shutdown_event.is_set():
            # Проверяем камеру периодически
            iteration += 1
            if iteration % camera_check_interval == 0:
                if not is_camera_alive(cam):
                    log.warning("Камера не отвечает — возврат к ожиданию подключения")
                    break

            # Читаем пакет RC из потока (автопилот шлёт сам)
            try:
                msg = recv_rc_channels(mav)
            except Exception as exc:
                log.warning("[RC] Ошибка при чтении MAVLink-потока: %s", exc)
                time.sleep(1)
                continue

            if msg is None:
                # Нет пакета в потоке за RC_REPLY_TIMEOUT — ждём дальше
                time.sleep(RC_POLL_INTERVAL)
                continue

            ch8 = get_rc_channel8(msg)
            if ch8 is None:
                time.sleep(RC_POLL_INTERVAL)
                continue

            log.debug("RC ch8 = %d  (triggered=%s)", ch8, triggered)

            if ch8 > RC_CH8_THRESHOLD and not triggered:
                log.info(
                    "RC ch8=%d > %d → запуск панорамы!", ch8, RC_CH8_THRESHOLD
                )
                triggered = True
                out_dir = SCRIPT_DIR / "shots" / datetime.now().strftime("%Y%m%d_%H%M%S")
                launch_panorama(mav, out_dir, cam, yaw_stop, yaw_thread)

            elif ch8 <= RC_REARM_THRESHOLD and triggered:
                log.info("RC ch8=%d ≤ %d → сброс триггера (готов к повторному запуску)",
                         ch8, RC_REARM_THRESHOLD)
                triggered = False

            # Пауза перед следующим запросом
            time.sleep(RC_POLL_INTERVAL)

        # Останавливаем поток коррекции Yaw
        yaw_stop.set()
        yaw_thread.join(timeout=3)

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
