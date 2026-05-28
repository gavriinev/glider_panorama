# glider_panorama

Автоматическая панорамная съёмка и склейка на основе SIYI A8 mini.

## Описание

Система управляет карданной камерой (гимбалом) SIYI A8 mini через UDP SDK, последовательно поворачивая камеру по нескольким позициям (по умолчанию: -60°, 0°, +60° по Yaw), делая снимок в каждой, и затем склеивая их в панораму с помощью Hugin CLI.

## Структура проекта

```
glider_panorama/
├── setup.sh                   # Скрипт установки всех зависимостей
├── configure_network.sh       # Настройка сети (policy-based routing + SSH)
├── requirements.txt           # Python-зависимости
├── siyi_sdk.py                # SDK для управления SIYI A8 mini (UDP)
├── panorama_shoot.py          # Съёмка по заданным позициям
├── startup_controller.py      # Автозапуск: Follow Mode + RC → панорама
├── glider-network.service     # systemd-юнит: настройка сети при старте
├── glider-controller.service  # systemd-юнит: запуск контроллера (зависит от сетевого)
├── stitch_panorama.py         # Склейка панорамы (шаблон / авто)
├── 60-0-60.pto                # PTO-шаблон Hugin (калиброванные параметры)
└── shots/                     # Снимки (создаётся автоматически)
    └── YYYYMMDD_HHMMSS/
        ├── LEFT.jpg
        ├── CENTER.jpg
        ├── RIGHT.jpg
        └── panorama_HHMMSS.jpg
```

## Требования

- **ОС:** Ubuntu / Debian / Raspberry Pi OS
- **Python:** 3.9+
- **Сеть:** устройство должно быть в подсети `192.168.144.x` (гимбал по умолчанию на `192.168.144.25`)
- **Камера:** SIYI A8 mini с прошивкой, поддерживающей SIYI SDK

## Быстрая установка

```bash
git clone <repo_url> glider_panorama
cd glider_panorama
chmod +x setup.sh
./setup.sh
```

Скрипт `setup.sh` автоматически:
1. Установит системные пакеты (`hugin-tools`, `enblend`, `ffmpeg`, `libgl1`)
2. Создаст Python venv и установит `opencv-python`, `numpy`
3. Проверит доступность всех CLI-инструментов Hugin
4. Проверит связь с гимбалом по сети

## Использование

### 1. Активация окружения

```bash
source venv/bin/activate
```

### 2. Съёмка

```bash
python3 panorama_shoot.py

# Указать папку вручную (используется startup_controller автоматически):
python3 panorama_shoot.py --output-dir shots/20260527_120000
```

Камера поворачивается по трём позициям, делает снимок в каждой.
Результат сохраняется в `shots/YYYYMMDD_HHMMSS/`.

### 3. Склейка

```bash
# Режим «шаблон» (быстро, ~5 сек)
python3 stitch_panorama.py

# Режим «авто CP» (пересчёт контрольных точек, ~30-60 сек)
python3 stitch_panorama.py --auto-cp

# Указать конкретную папку
python3 stitch_panorama.py shots/20260514_165039/

# Указать папку и имя выходного файла
python3 stitch_panorama.py --auto-cp shots/20260514_165039/ panorama.jpg
```

## Настройка параметров

### panorama_shoot.py

| Параметр | Значение по умолчанию | Описание |
|---|---|---|
| `GIMBAL_IP` | `192.168.144.25` | IP-адрес гимбала |
| `GIMBAL_PORT` | `37260` | UDP-порт SDK |
| `RTSP_URL` | `rtsp://...:8554/main.264` | RTSP-поток камеры |
| `SHOTS` | `[(-60, 0), (0, 0), (60, 0)]` | Позиции (yaw, pitch, label) |
| `STABILIZE_DELAY` | `1.0` | Пауза стабилизации (сек) |
| `ANGLE_TOLERANCE` | `10.0` | Допуск позиционирования (°) |
| `--output-dir` (CLI) | авто (`shots/YYYYMMDD_HHMMSS`) | Папка для снимков и телеметрии |

### stitch_panorama.py

| Параметр | Описание |
|---|---|
| `--template` | Использовать PTO-шаблон (по умолчанию) |
| `--auto-cp` | Автоматически найти контрольные точки |

## Настройка сети

### Топология

```
┌──────────────────┐                     ┌────────────────┐                     ┌──────────────────────┐
│  Удалённое       │     радиоканал      │  SIYI Air Unit │      Ethernet       │  Основное устройство │
│  устройство      │◄───────────────────►│                │◄───────────────────►│                      │
│  192.168.144.31  │   (SIYI link)       │  192.168.144.11│    enp88s0 (.30)    │  enp88s0: .30        │
└──────────────────┘                     └────────────────┘                     │  enxec...: .32       │
                                                                               │                      │
                                                          ┌───────────────┐    │                      │
                                                          │  SIYI A8 mini │◄──►│  enxec...: .32       │
                                                          │ 192.168.144.25│    │                      │
                                                          │  UDP :37260   │    │                      │
                                                          │  RTSP :8554   │    │                      │
                                                          └───────────────┘    └──────────────────────┘
```

Все устройства находятся в одной подсети `192.168.144.0/24`, но подключены через **разные физические порты** основного устройства.

### Проблема: два LAN-порта в одной подсети

Когда два сетевых интерфейса имеют адреса в одной подсети (`.30` и `.32` — оба в `192.168.144.0/24`), Linux создаёт **два маршрута к одной сети** и использует только один из них. Трафик ко второму устройству уходит через неправильный интерфейс и теряется.

Дополнительно, `rp_filter` в strict mode отбрасывает ответные пакеты, пришедшие через «неправильный» интерфейс.

### Решение: policy-based routing

Скрипт `configure_network.sh` создаёт раздельные таблицы маршрутизации и правила, которые направляют трафик к каждому устройству через нужный интерфейс.

#### Как определить имена интерфейсов

```bash
ip -br addr | grep 192.168.144
```

Пример вывода:

```
eth0          UP  192.168.144.30/24    ← порт для Air Unit
enxec9a0c162d05  UP  192.168.144.32/24    ← порт для A8 mini
```

#### Конфигурация (начало `configure_network.sh`)

Откройте скрипт и впишите ваши интерфейсы и IP-адреса:

```bash
# Интерфейс для Air Unit и всех устройств за ним
IF_AIR="eth0"
IP_AIR_LOCAL="192.168.144.30"

# Устройства, доступные через Air Unit
AIR_DEVICES=(
    "192.168.144.11"    # SIYI Air Unit
    "192.168.144.31"    # Удалённое устройство
)

# Интерфейс для A8 mini (прямое подключение)
IF_CAM="enxec9a0c162d05"
IP_CAM_LOCAL="192.168.144.32"
IP_CAM_DEVICE="192.168.144.25"
```

> Чтобы добавить новое устройство за Air Unit, просто допишите его IP в массив `AIR_DEVICES`.

#### Запуск

```bash
# Настроить маршрутизацию + запустить SSH
sudo ./configure_network.sh

# Откатить все изменения
sudo ./configure_network.sh --reset
```

#### Что делает скрипт

| Шаг | Действие |
|---|---|
| 1 | Регистрирует таблицы маршрутизации `siyi_air` и `siyi_cam` |
| 2 | Создаёт host-route `/32` для каждого устройства через нужный интерфейс |
| 3 | Добавляет `ip rule` — направляет трафик по правильным таблицам |
| 4 | Переключает `rp_filter` в loose mode (2) |
| 5 | Проверяет и запускает SSH-сервер |
| 6 | Пингует все устройства для проверки связи |

#### Проверка маршрутов

```bash
# Должен показать enp88s0:
ip route get 192.168.144.11
ip route get 192.168.144.31

# Должен показать enxec...:
ip route get 192.168.144.25
```

#### Сохранение после перезагрузки

Policy-based routing сбрасывается при перезагрузке. Для автозапуска используйте systemd-сервис `glider-network` — см. раздел **«Автозапуск на Raspberry Pi»** ниже.

### SSH-доступ с удалённого устройства

SSH-сервер устанавливается и запускается автоматически скриптом `configure_network.sh`.

```bash
# С удалённого устройства (192.168.144.31):
ssh glider1@192.168.144.30
```

Пароль — пароль пользователя `glider1` (тот же, что используется для `sudo`).

#### Вход по ключу (без пароля)

На удалённом устройстве:

```bash
ssh-keygen -t ed25519
ssh-copy-id glider1@192.168.144.30
# Теперь вход без пароля:
ssh glider1@192.168.144.30
```

### Диагностика сетевых проблем

| Симптом | Причина | Решение |
|---|---|---|
| Пинг до `.25` или `.11` не проходит | Маршрут идёт через неправильный интерфейс | `sudo systemctl restart glider-network` |
| SSH зависает при подключении | Ответные пакеты уходят через другой порт | Добавьте IP клиента в `AIR_DEVICES` |
| После перезагрузки связь пропала | Policy routing сбросился | `glider-network` в systemd восстанавливает автоматически |
| `RTSP timeout` при съёмке | A8 mini недоступна | Проверьте `ip route get 192.168.144.25` |
| `rp_filter` отбрасывает пакеты | strict mode (значение 1) | Скрипт ставит loose mode (2) автоматически |

---

## Автозапуск на Raspberry Pi (`startup_controller.py`)

`startup_controller.py` — основной управляющий скрипт, который запускается автоматически при включении RPi и работает без участия оператора.

### Логика работы

```
Включение RPi
      │
      ▼
[1] Ожидание SIYI A8 mini (UDP 192.168.144.25:37260)
      │  Повтор каждые 5 секунд до появления камеры
      ▼
[2] Lock Mode: гимбал стабилизирует горизонт, Yaw=0°
      │  Фоновый поток каждые 5 с корректирует Yaw обратно к 0°
      ▼
[3] Подключение к USB-UART → MAVLink (57600 бод, System=1, Component=1)
      │  REQUEST_DATA_STREAM: RC 10 Hz + ATTITUDE/VFR_HUD/AHRS2 5 Hz
      ▼
[4] Мониторинг RC Channel 8 (пассивный приём, каждые 0.5 с)
      ├── ch8 > 1800  →  остановить Yaw-коррекцию
      │                   создать shots/YYYYMMDD_HHMMSS/
      │                   запустить panorama_shoot.py
      │                   записывать AHRS2/ATTITUDE/VFR_HUD → telemetry.json
      │                   после съёмки — возобновить Yaw-коррекцию
      ├── ch8 ≤ 1500  →  сброс триггера (готов к следующему запуску)
      └── камера пропала  →  вернуться к шагу [1]
```

### Установка зависимостей

```bash
# На Raspberry Pi (пользователь glider1):
cd /home/glider1/glider_panorama

# Установить системные пакеты и создать venv:
chmod +x setup.sh
./setup.sh

# Дополнительные зависимости для startup_controller:
source venv/bin/activate
pip install pymavlink pyserial
```

### Установка systemd-сервисов (автозапуск)

Два сервиса устанавливаются вместе. Порядок запуска фиксирован:
```
[network.target] → [glider-network] → [glider-controller]
```
`glider-network` настраивает маршрутизацию (root) и **автоматически повторяет попытку** каждые 15 с при ошибке (например, если сетевые интерфейсы ещё не поднялись). После первого успешного завершения стартует контроллер.

```bash
# 1. Скопировать оба юнит-файла в systemd
sudo cp /home/glider1/glider_panorama/glider-network.service \
        /home/glider1/glider_panorama/glider-controller.service \
        /etc/systemd/system/

# 2. Перечитать конфигурацию systemd
sudo systemctl daemon-reload

# 3. Включить автозапуск обоих сервисов
sudo systemctl enable glider-network glider-controller

# 4. Запустить немедленно (без перезагрузки)
sudo systemctl start glider-network
sudo systemctl start glider-controller
```

После этого оба сервиса будут стартовать **автоматически при каждом включении** RPi.

### Проверка статуса

```bash
# Статус сетевого сервиса (однократный запуск):
sudo systemctl status glider-network

# Статус контроллера (работает / упал / перезапускается):
sudo systemctl status glider-controller

# Логи сетевой настройки:
journalctl -u glider-network

# Логи контроллера в реальном времени:
journalctl -u glider-controller -f

# Логи контроллера за последние 100 строк:
journalctl -u glider-controller -n 100
```

### Управление сервисами вручную

```bash
# Перезапустить настройку сети вручную (например, после смены маршрутов):
sudo systemctl restart glider-network

# Перезапустить контроллер (после изменения конфига):
sudo systemctl restart glider-controller

# Остановить оба:
sudo systemctl stop glider-controller glider-network

# Убрать из автозапуска:
sudo systemctl disable glider-network glider-controller
```

### Настройка параметров

Откройте `startup_controller.py` и измените константы в секции **«Конфигурация»**:

**Гимбал и камера**

| Параметр | По умолчанию | Описание |
|---|---|---|
| `GIMBAL_CONNECT_RETRY_SEC` | `5` | Пауза между попытками подключения к камере |
| `YAW_TARGET` | `0.0` | Целевой угол Yaw (°) |
| `YAW_CORRECT_INTERVAL` | `5.0` | Интервал коррекции Yaw (сек) |
| `YAW_CORRECT_TOLERANCE` | `3.0` | Допуск коррекции Yaw (°) |

**MAVLink / RC**

| Параметр | По умолчанию | Описание |
|---|---|---|
| `MAVLINK_PORT` | `/dev/ttyUSB0` | Путь к USB-UART адаптеру |
| `MAVLINK_BAUD` | `57600` | Baudrate (ArduPilot / PX4) |
| `MAV_TARGET_SYSTEM` | `1` | System ID автопилота |
| `MAV_TARGET_COMPONENT` | `1` | Component ID автопилота |
| `RC_POLL_INTERVAL` | `0.5` | Период чтения RC из потока (сек) |
| `RC_REPLY_TIMEOUT` | `2.0` | Таймаут ожидания RC-пакета (сек) |
| `RC_CH8_THRESHOLD` | `1800` | Порог активации панорамы |
| `RC_REARM_THRESHOLD` | `1500` | Порог сброса триггера |

**Телеметрия**

| Параметр | По умолчанию | Описание |
|---|---|---|
| `TELEMETRY_POLL_HZ` | `5` | Частота записи телеметрии (Hz) |
| `TELEMETRY_FILENAME` | `telemetry.json` | Имя файла в папке со снимками |

> После изменения параметров перезапустите сервис:
> ```bash
> sudo systemctl restart glider-controller
> ```

### Определение USB-порта адаптера

Если адаптер USB-UART попал на другой номер (`ttyUSB1`, `ttyUSB2` и т.д.):

```bash
# Посмотреть все USB-последовательные порты:
ls /dev/ttyUSB*

# Посмотреть информацию о конкретном порту:
udevadm info /dev/ttyUSB0 | grep -E 'ID_VENDOR|ID_MODEL'

# Следить за появлением нового устройства при подключении:
dmesg | tail -20
```

Затем исправьте `MAVLINK_PORT` в `startup_controller.py` и перезапустите сервис.

### Телеметрия во время съёмки

При каждом запуске панорамы `startup_controller.py`:
1. Останавливает поток коррекции Yaw (чтобы не мешать гимбалу)
2. Параллельно записывает телеметрию автопилота в `telemetry.json`
3. После завершения съёмки автоматически возобновляет коррекцию Yaw

Телеметрия поступает через `REQUEST_DATA_STREAM` (автопилот стримит сам), данные принимаются пассивно.

**Записываемые MAVLink-пакеты:**

| Сообщение | ID | Поля |
|---|---|---|
| `AHRS2` | 178 | roll, pitch, yaw, altitude, lat, lng |
| `ATTITUDE` | 30 | roll, pitch, yaw, rollspeed, pitchspeed, yawspeed |
| `VFR_HUD` | 74 | airspeed, groundspeed, heading, throttle, alt, climb |

**Структура папки после съёмки:**

```
shots/20260527_201500/
├── LEFT.jpg
├── CENTER.jpg
├── RIGHT.jpg
└── telemetry.json    ← AHRS2 / ATTITUDE / VFR_HUD, 5 записей/сек
```

**Формат `telemetry.json`:**
```json
[
  {
    "ts": "2026-05-27T17:15:00.000Z",
    "AHRS2":   { "roll": 0.01, "pitch": -0.02, "yaw": 0.0, ... },
    "ATTITUDE": { "roll": 0.01, "pitch": -0.02, "yaw": 0.0, ... },
    "VFR_HUD": { "airspeed": 12.3, "groundspeed": 11.9, "heading": 270, ... }
  },
  ...
]
```

### Диагностика

| Симптом | Что проверить |
|---|---|
| `glider-network` не стартует / в цикле | `journalctl -u glider-network -f` — ждёт интерфейсы, повтор каждые 15 с |
| `glider-controller` не стартует | `journalctl -u glider-controller -n 50` |
| Камера не находится | `ping 192.168.144.25`, проверьте сеть и статус `glider-network` |
| Гимбал не в Lock Mode | `journalctl -u glider-controller` — ищите строку «Режим гимбала» |
| MAVLink не подключается | `ls /dev/ttyUSB*`, проверьте baudrate |
| RC-пакеты не приходят | Проверьте baudrate; в логах должна быть строка «запрошены RC (10 Hz)» |
| Автопилот не стримит телеметрию | Убедитесь что `REQUEST_DATA_STREAM` поддерживается (ArduPilot) |
| Yaw не возвращается после панорамы | Проверьте логи `[YAW] Поток коррекции Yaw возобновлён` |
| Панорама не запускается | RC ch8 должен превышать 1800 |
| Панорама запускается повторно | ch8 должен упасть ниже 1500 для сброса триггера |
| Нет `telemetry.json` | Проверьте логи `[TEL]` в journalctl |
| Нет venv | Запустите `./setup.sh`, затем `pip install pymavlink pyserial` |

---

## Лицензия

Для внутреннего использования.
