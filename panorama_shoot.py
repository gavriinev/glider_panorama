"""
panorama_shoot.py
-----------------
Поворачивает гимбал A8 mini по трём позициям Yaw (-120°, 0°, +120°),
в каждой позиции:
  1. Посылает команду фото → снимок сохраняется на TF-карту гимбала
  2. Захватывает кадр из RTSP-потока → сохраняет на компьютер локально

Зависимости: opencv-python  (pip install opencv-python)
Использование:
  python3 panorama_shoot.py
  python3 panorama_shoot.py --output-dir shots/20260527_120000
"""

import time
import sys
import os
import argparse
from datetime import datetime
from pathlib import Path

import cv2

# Импортируем наш SDK
from siyi_sdk import SIYICamera, GimbalAttitude

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------
GIMBAL_IP   = "192.168.144.25"
GIMBAL_PORT = 37260

# RTSP-адрес основного потока A8 mini
RTSP_URL = f"rtsp://{GIMBAL_IP}:8554/main.264"

# Позиции съёмки: (yaw, pitch, метка)
SHOTS = [
    (-60.0, -0.0, "left"),
    (   0.0, 0.0, "center"),
    ( 60.0, 0.0, "right"),
]

# Время ожидания стабилизации гимбала после поворота (сек)
STABILIZE_DELAY = 1.0

# Допустимая погрешность угла (градусы) для проверки достижения позиции
ANGLE_TOLERANCE = 10.0

# Папка для сохранения снимков (можно переопределить через --output-dir)
OUTPUT_DIR = Path("./shots") / datetime.now().strftime("%Y%m%d_%H%M%S")
 
# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def wait_for_angle(cam: SIYICamera, target_yaw: float,
                   timeout: float = 8.0) -> bool:
    """
    Ждём, пока гимбал не достигнет целевого угла yaw
    с точностью ANGLE_TOLERANCE градусов.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        att = cam.get_attitude()
        if att is None:
            time.sleep(0.2)
            continue
        err = abs(att.yaw - target_yaw)
        print(f"  Yaw: {att.yaw:+.1f}°  (цель: {target_yaw:+.1f}°, "
              f"ошибка: {err:.1f}°)")
        if err <= ANGLE_TOLERANCE:
            return True
        time.sleep(0.3)
    return False


def capture_rtsp_frame(rtsp_url: str, retries: int = 5) -> "cv2.Mat | None":
    """
    Открывает RTSP-поток, захватывает один кадр и возвращает его.
    Пробует несколько раз при ошибке.
    """
    for attempt in range(1, retries + 1):
        cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            print(f"  [RTSP] Попытка {attempt}: не удалось открыть поток")
            time.sleep(1.0)
            continue

        # Пропускаем несколько кадров для «прогрева» потока
        for _ in range(5):
            cap.read()

        ret, frame = cap.read()
        cap.release()

        if ret and frame is not None:
            return frame

        print(f"  [RTSP] Попытка {attempt}: кадр не получен")
        time.sleep(1.0)

    return None


def save_frame(frame, label: str, yaw: float) -> Path:
    """Сохраняет кадр в OUTPUT_DIR без подписи, в максимальном качестве.
    Имя файла – LEFT, CENTER или RIGHT (в зависимости от label).
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # Используем 100‑й уровень JPEG‑качества (максимум)
    filename = OUTPUT_DIR / f"{label.upper()}.jpg"
    cv2.imwrite(str(filename), frame, [cv2.IMWRITE_JPEG_QUALITY, 100])
    return filename


# annotate_frame удалена – сохранение без наложения подписи


# ---------------------------------------------------------------------------
# Главный сценарий
# ---------------------------------------------------------------------------

def main():
    print("=" * 55)
    print("  SIYI A8 mini — Панорамная съёмка (3 позиции)")
    print("=" * 55)

    cam = SIYICamera(ip=GIMBAL_IP, port=GIMBAL_PORT)
    cam.connect()

    # --- Проверяем связь ---
    fw = cam.get_firmware_version()
    print(f"\nПрошивка гимбала: {fw}")

    att = cam.get_attitude()
    if att is None:
        print("[ОШИБКА] Нет ответа от гимбала. Проверьте соединение.")
        cam.disconnect()
        sys.exit(1)

    print(f"Текущее положение: {att}")

    # --- Устанавливаем Lock-режим (для точного позиционирования) ---
    print("\nУстановка режима Lock...")
    cam.set_mode_lock()
    time.sleep(0.5)

    results = []

    for yaw_target, pitch_target, label in SHOTS:
        print(f"\n{'─' * 45}")
        print(f"▶  Позиция: {label.upper()}  "
              f"(Yaw={yaw_target:+.0f}°, Pitch={pitch_target:+.0f}°)")
        print(f"{'─' * 45}")

        # 1. Отправляем команду поворота
        print(f"  Поворот к Yaw={yaw_target:+.0f}°...")
        cam.set_angle(yaw=yaw_target, pitch=pitch_target)

        # 2. Ждём достижения угла
        reached = wait_for_angle(cam, yaw_target)
        if not reached:
            print(f"  [!] Гимбал не достиг цели за отведённое время")

        # 3. Дополнительная пауза для механической стабилизации
        print(f"  Стабилизация ({STABILIZE_DELAY}с)...")
        time.sleep(STABILIZE_DELAY)

        # 4. Читаем актуальный угол
        att = cam.get_attitude()
        actual_yaw   = att.yaw   if att else yaw_target
        actual_pitch = att.pitch if att else pitch_target
        print(f"  Фактическое положение: Yaw={actual_yaw:+.1f}°, "
              f"Pitch={actual_pitch:+.1f}°")

        # 5. Команда фото → сохраняется на TF-карту гимбала
        print("  Фото на TF-карту гимбала...")
        cam.take_photo()
        time.sleep(0.5)   # небольшая пауза перед захватом кадра

        # 6. Захват кадра из RTSP-потока → локальный файл
        print("  Захват кадра из RTSP-потока...")
        frame = capture_rtsp_frame(RTSP_URL)

        if frame is not None:
            saved_path = save_frame(frame, label, yaw_target)
            print(f"  ✓ Сохранено: {saved_path}")
            results.append((label, yaw_target, saved_path, True))
        else:
            print("  ✗ Кадр не получен (RTSP недоступен?)")
            results.append((label, yaw_target, None, False))

    # --- Возвращаем гимбал в центр ---
    print(f"\n{'─' * 45}")
    print("Возврат в центр (Yaw=0°, Pitch=0°)...")
    cam.center()
    time.sleep(2)

    cam.disconnect()

    # --- Итог ---
    print(f"\n{'=' * 55}")
    print("  Результаты:")
    print(f"{'=' * 55}")
    for label, yaw, path, ok in results:
        status = f"✓  {path}" if ok else "✗  не сохранён"
        print(f"  {label:8s} (Yaw={yaw:+.0f}°)  →  {status}")

    ok_count = sum(1 for *_, ok in results if ok)
    print(f"\nСохранено {ok_count}/{len(SHOTS)} снимков в: {OUTPUT_DIR.resolve()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SIYI A8 mini панорамная съёмка")
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Папка для сохранения снимков (no default: shots/YYYYMMDD_HHMMSS)"
    )
    args = parser.parse_args()

    if args.output_dir:
        OUTPUT_DIR = Path(args.output_dir)

    main()
