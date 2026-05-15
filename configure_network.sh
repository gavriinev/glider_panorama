#!/usr/bin/env bash
# ============================================================================
#  configure_network.sh — Настройка policy-based routing
#  для одновременной работы с SIYI Air Unit, SIYI A8 mini
#  и удалёнными устройствами за Air Unit (SSH и т.д.)
#
#  Топология:
#    [Удалённое .31] ◄──radio──► [Air Unit .11] ◄──eth──► [nuc5 eth0 .30]
#                                                         [nuc5 eth1 .32] ◄──► [A8 mini .25]
#
#  Запуск:   sudo ./configure_network.sh
#  Отмена:   sudo ./configure_network.sh --reset
# ============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Конфигурация — измените под ваши интерфейсы и IP
# ---------------------------------------------------------------------------

# Интерфейс и IP для SIYI Air Unit (и всех устройств за ним)
IF_AIR="enp88s0"
IP_AIR_LOCAL="192.168.144.30"     # IP вашего порта

# Устройства, доступные через Air Unit (eth0)
# Air Unit + удалённые устройства, подключённые через радиоканал
AIR_DEVICES=(
    "192.168.144.11"    # SIYI Air Unit
    "192.168.144.31"    # Удалённое устройство (SSH-клиент)
)

TABLE_AIR="siyi_air"
TABLE_AIR_ID="100"

# Интерфейс и IP для SIYI A8 mini
IF_CAM="enxec9a0c162d05"
IP_CAM_LOCAL="192.168.144.32"     # IP вашего порта
IP_CAM_DEVICE="192.168.144.25"   # IP A8 mini
TABLE_CAM="siyi_cam"
TABLE_CAM_ID="101"

# ---------------------------------------------------------------------------
# Цвета
# ---------------------------------------------------------------------------
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'
info()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[✗]${NC} $*"; exit 1; }

# ---------------------------------------------------------------------------
# Проверка прав
# ---------------------------------------------------------------------------
if [ "$EUID" -ne 0 ]; then
    error "Запустите от root:  sudo $0"
fi

# ---------------------------------------------------------------------------
# Режим сброса
# ---------------------------------------------------------------------------
if [ "${1:-}" = "--reset" ]; then
    echo "Сброс policy-based routing..."

    # Удаляем правила для всех Air-устройств
    for dev_ip in "${AIR_DEVICES[@]}"; do
        ip rule del to "$dev_ip" table "$TABLE_AIR" 2>/dev/null && \
            info "Удалено правило для $dev_ip" || true
    done
    ip route flush table "$TABLE_AIR" 2>/dev/null && \
        info "Очищена таблица $TABLE_AIR" || warn "Таблица $TABLE_AIR пуста"

    ip rule  del to "$IP_CAM_DEVICE" table "$TABLE_CAM" 2>/dev/null && \
        info "Удалено правило для A8 mini" || warn "Правило A8 mini не найдено"
    ip route flush table "$TABLE_CAM" 2>/dev/null && \
        info "Очищена таблица $TABLE_CAM" || warn "Таблица $TABLE_CAM пуста"

    # Удаляем записи из rt_tables
    sed -i "/^${TABLE_AIR_ID}\s/d" /etc/iproute2/rt_tables 2>/dev/null
    sed -i "/^${TABLE_CAM_ID}\s/d" /etc/iproute2/rt_tables 2>/dev/null

    info "Сброс завершён"
    exit 0
fi

# ---------------------------------------------------------------------------
# Основная настройка
# ---------------------------------------------------------------------------
echo "=============================================="
echo "  Policy-Based Routing для SIYI устройств"
echo "=============================================="
echo ""
echo "  Через ${IF_AIR} (${IP_AIR_LOCAL}):"
for dev_ip in "${AIR_DEVICES[@]}"; do
    echo "    • ${dev_ip}"
done
echo "  Через ${IF_CAM} (${IP_CAM_LOCAL}):"
echo "    • ${IP_CAM_DEVICE}  (A8 mini)"
echo ""

# 1. Проверяем наличие интерфейсов
for iface in "$IF_AIR" "$IF_CAM"; do
    if ! ip link show "$iface" &> /dev/null; then
        error "Интерфейс $iface не найден. Проверьте имя (ip link show)"
    fi
done
info "Интерфейсы $IF_AIR и $IF_CAM найдены"

# 2. Регистрируем таблицы маршрутизации
RT_FILE="/etc/iproute2/rt_tables"
if ! grep -q "^${TABLE_AIR_ID} " "$RT_FILE" 2>/dev/null; then
    echo "${TABLE_AIR_ID} ${TABLE_AIR}" >> "$RT_FILE"
    info "Таблица '$TABLE_AIR' (id=$TABLE_AIR_ID) зарегистрирована"
else
    info "Таблица '$TABLE_AIR' уже зарегистрирована"
fi

if ! grep -q "^${TABLE_CAM_ID} " "$RT_FILE" 2>/dev/null; then
    echo "${TABLE_CAM_ID} ${TABLE_CAM}" >> "$RT_FILE"
    info "Таблица '$TABLE_CAM' (id=$TABLE_CAM_ID) зарегистрирована"
else
    info "Таблица '$TABLE_CAM' уже зарегистрирована"
fi

# 3. Очищаем таблицы
ip route flush table "$TABLE_AIR" 2>/dev/null || true
ip route flush table "$TABLE_CAM" 2>/dev/null || true

# 4. Удаляем старые правила
for dev_ip in "${AIR_DEVICES[@]}"; do
    ip rule del to "$dev_ip" table "$TABLE_AIR" 2>/dev/null || true
done
ip rule del to "$IP_CAM_DEVICE" table "$TABLE_CAM" 2>/dev/null || true

# 5. Добавляем маршруты — все Air-устройства через eth0
for dev_ip in "${AIR_DEVICES[@]}"; do
    ip route add "${dev_ip}/32" dev "$IF_AIR" src "$IP_AIR_LOCAL" table "$TABLE_AIR"
    ip rule  add to "$dev_ip" table "$TABLE_AIR" priority 100
    info "Маршрут + правило: ${dev_ip} → ${IF_AIR}"
done

# A8 mini через eth1
ip route add "$IP_CAM_DEVICE/32" dev "$IF_CAM" src "$IP_CAM_LOCAL" table "$TABLE_CAM"
ip rule  add to "$IP_CAM_DEVICE" table "$TABLE_CAM" priority 101
info "Маршрут + правило: ${IP_CAM_DEVICE} → ${IF_CAM}"

# 6. Отключаем rp_filter (strict mode мешает при двух интерфейсах в одной подсети)
for iface in "$IF_AIR" "$IF_CAM"; do
    sysctl -w "net.ipv4.conf.${iface}.rp_filter=2" > /dev/null
done
sysctl -w net.ipv4.conf.all.rp_filter=2 > /dev/null
info "rp_filter → loose mode (2)"

# ---------------------------------------------------------------------------
# 7. SSH — проверяем и запускаем
# ---------------------------------------------------------------------------
echo ""
echo "──────────────────────────────────────────────"
echo "  SSH сервер"
echo "──────────────────────────────────────────────"

if systemctl is-active --quiet sshd 2>/dev/null || systemctl is-active --quiet ssh 2>/dev/null; then
    info "SSH сервер уже запущен"
else
    # Устанавливаем если нет
    if ! command -v sshd &> /dev/null; then
        warn "openssh-server не установлен, устанавливаю..."
        apt update -qq && apt install -y -qq openssh-server
    fi
    # Запускаем
    systemctl enable --now ssh 2>/dev/null || systemctl enable --now sshd 2>/dev/null
    info "SSH сервер запущен и добавлен в автозагрузку"
fi

# Проверяем, слушает ли порт 22
if ss -tlnp | grep -q ':22 '; then
    info "SSH слушает на порту 22"
else
    warn "SSH не слушает на порту 22 — проверьте конфигурацию /etc/ssh/sshd_config"
fi

# ---------------------------------------------------------------------------
# Проверка маршрутизации
# ---------------------------------------------------------------------------
echo ""
echo "──────────────────────────────────────────────"
echo "  Проверка маршрутизации"
echo "──────────────────────────────────────────────"

for dev_ip in "${AIR_DEVICES[@]}"; do
    ROUTE=$(ip route get "$dev_ip" 2>/dev/null | head -1)
    echo "  ${dev_ip}:  $ROUTE"
done
ROUTE_CAM=$(ip route get "$IP_CAM_DEVICE" 2>/dev/null | head -1)
echo "  ${IP_CAM_DEVICE}:  $ROUTE_CAM"

echo ""
echo "──────────────────────────────────────────────"
echo "  Проверка связи (ping)"
echo "──────────────────────────────────────────────"

for dev_ip in "${AIR_DEVICES[@]}"; do
    if ping -c 1 -W 2 -I "$IF_AIR" "$dev_ip" &> /dev/null; then
        info "${dev_ip} — отвечает (через ${IF_AIR})"
    else
        warn "${dev_ip} — не отвечает"
    fi
done

if ping -c 1 -W 2 -I "$IF_CAM" "$IP_CAM_DEVICE" &> /dev/null; then
    info "${IP_CAM_DEVICE} — отвечает (через ${IF_CAM})"
else
    warn "${IP_CAM_DEVICE} — не отвечает"
fi

echo ""
echo "=============================================="
echo "  Настройка завершена!"
echo "=============================================="
echo ""
echo "  SSH-подключение с удалённого устройства:"
echo "    ssh $(whoami)@${IP_AIR_LOCAL}"
echo ""
echo "  Для отмены:  sudo $0 --reset"
echo ""
