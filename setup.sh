#!/usr/bin/env bash
# ============================================================================
#  setup.sh — Установка зависимостей для glider_panorama
#
#  Запуск:
#    chmod +x setup.sh
#    ./setup.sh
#
#  Поддерживаемые платформы:
#    • Ubuntu / Debian (apt)
#    • Raspberry Pi OS (armhf / aarch64)
# ============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()    { echo -e "${GREEN}[✓]${NC} $*"; }
warn()    { echo -e "${YELLOW}[!]${NC} $*"; }
error()   { echo -e "${RED}[✗]${NC} $*"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=============================================="
echo "  glider_panorama — Установка зависимостей"
echo "=============================================="
echo ""

# -------------------------------------------------------
# 1. Проверяем ОС
# -------------------------------------------------------
if ! command -v apt &> /dev/null; then
    error "Поддерживается только apt-based дистрибутив (Ubuntu / Debian / RPi OS)"
fi

# -------------------------------------------------------
# 2. Системные пакеты
# -------------------------------------------------------
info "Обновление списка пакетов..."
sudo apt update -qq

PACKAGES=(
    python3
    python3-pip
    python3-venv
    hugin-tools        # nona, pto_gen, cpfind, cpclean, autooptimiser, pano_modify
    enblend            # enblend-enfuse
    ffmpeg             # для RTSP-потока через OpenCV
    libgl1             # OpenGL для cv2
)

info "Установка системных пакетов..."
# sudo apt install -y -qq "${PACKAGES[@]}"

# -------------------------------------------------------
# 3. Проверяем наличие Hugin CLI
# -------------------------------------------------------
echo ""
HUGIN_TOOLS=(nona enblend cpfind cpclean autooptimiser pano_modify pto_gen)
ALL_OK=true
for tool in "${HUGIN_TOOLS[@]}"; do
    if command -v "$tool" &> /dev/null; then
        info "$tool  →  $(which $tool)"
    else
        warn "$tool  →  НЕ НАЙДЕН"
        ALL_OK=false
    fi
done

if [ "$ALL_OK" = false ]; then
    warn "Некоторые инструменты Hugin не найдены. Склейка по шаблону (--template) может работать, но --auto-cp потребует все инструменты."
fi

# -------------------------------------------------------
# 4. Python виртуальное окружение
# -------------------------------------------------------
echo ""
VENV_DIR="${SCRIPT_DIR}/venv"

if [ ! -d "$VENV_DIR" ]; then
    info "Создание виртуального окружения: ${VENV_DIR}"
    python3 -m venv "$VENV_DIR"
else
    info "Виртуальное окружение уже существует: ${VENV_DIR}"
fi

info "Активация venv и установка Python-зависимостей..."
source "${VENV_DIR}/bin/activate"
pip install --upgrade pip -q
pip install -r "${SCRIPT_DIR}/requirements.txt" -q

# -------------------------------------------------------
# 5. Проверка Python-пакетов
# -------------------------------------------------------
echo ""
info "Проверка Python-окружения:"
python3 -c "
import cv2
import numpy as np
print(f'  OpenCV:  {cv2.__version__}')
print(f'  NumPy:   {np.__version__}')
print('  ✓ Все Python-зависимости установлены')
"

# -------------------------------------------------------
# 6. Проверка сети (гимбал)
# -------------------------------------------------------
echo ""
GIMBAL_IP="192.168.144.25"
info "Проверка связи с гимбалом (${GIMBAL_IP})..."
if ping -c 1 -W 2 "$GIMBAL_IP" &> /dev/null; then
    info "Гимбал доступен по ${GIMBAL_IP}"
else
    warn "Гимбал не отвечает на ping (${GIMBAL_IP}). Убедитесь, что устройство подключено к той же сети."
fi

# -------------------------------------------------------
# 7. Создаём директорию для снимков
# -------------------------------------------------------
mkdir -p "${SCRIPT_DIR}/shots"
info "Директория для снимков: ${SCRIPT_DIR}/shots/"

# -------------------------------------------------------
# Итог
# -------------------------------------------------------
echo ""
echo "=============================================="
echo "  Установка завершена!"
echo "=============================================="
echo ""
echo "  Активация окружения:"
echo "    source ${VENV_DIR}/bin/activate"
echo ""
echo "  Использование:"
echo "    python3 panorama_shoot.py             # Съёмка (3 позиции)"
echo "    python3 stitch_panorama.py            # Склейка (шаблон)"
echo "    python3 stitch_panorama.py --auto-cp  # Склейка (авто CP)"
echo ""
