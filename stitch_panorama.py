"""
stitch_panorama.py
------------------
Склеивает три снимка (LEFT, CENTER, RIGHT) в панораму.

Два режима:
  --template (по умолчанию)
      Использует готовый PTO-шаблон с зафиксированными контрольными точками.
      Быстро, стабильно, подходит когда сцена и камера не меняются.

  --auto-cp
      Каждый раз пересчитывает контрольные точки автоматически:
        pto_gen → cpfind → cpclean → autooptimiser → pano_modify → nona → enblend
      Медленнее, но адаптируется к любой сцене.

Зависимости: hugin-tools, enblend  (sudo apt install hugin-tools enblend)
Использование:
    python3 stitch_panorama.py                          # шаблон, последняя папка
    python3 stitch_panorama.py --auto-cp                # автоCP, последняя папка
    python3 stitch_panorama.py shots/20260514_165039/   # шаблон, указанная папка
    python3 stitch_panorama.py --auto-cp shots/.../ panorama.jpg
"""

import subprocess
import sys
import re
import shutil
import tempfile
from pathlib import Path
from datetime import datetime

# ---------------------------------------------------------------------------
# Разбор аргументов
# ---------------------------------------------------------------------------
args = sys.argv[1:]
AUTO_CP = "--auto-cp" in args
if AUTO_CP:
    args.remove("--auto-cp")

# PTO-шаблон (нужен только в режиме --template)
PTO_TEMPLATE = Path("/home/nuc5/rpi_siyi/CENTER - RIGHT.pto")


def find_latest_shots_dir() -> Path:
    shots_root = Path("./shots")
    if not shots_root.exists():
        return Path(".")
    for d in sorted(shots_root.iterdir(), reverse=True):
        if d.is_dir() and (d / "CENTER.jpg").exists():
            return d
    return Path(".")


SHOTS_DIR   = Path(args[0]) if len(args) > 0 else find_latest_shots_dir()
OUTPUT_FILE = Path(args[1]) if len(args) > 1 else \
              SHOTS_DIR / f"panorama_{datetime.now().strftime('%H%M%S')}.jpg"

# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def run(cmd: list, description: str) -> None:
    """Запускает команду, выводит прогресс и бросает исключение при ошибке."""
    print(f"\n▶ {description}")
    print("  " + " ".join(str(c) for c in cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout.strip():
        for line in result.stdout.strip().splitlines():
            print(f"  {line}")
    if result.stderr.strip():
        for line in result.stderr.strip().splitlines():
            print(f"  [stderr] {line}")
    if result.returncode != 0:
        raise RuntimeError(
            f"Ошибка (код {result.returncode}): {' '.join(str(c) for c in cmd)}\n"
            f"{result.stderr}"
        )
    print("  ✓ OK")


# ---------------------------------------------------------------------------
# Режим 1: шаблон (быстро)
# ---------------------------------------------------------------------------

def stitch_with_template(images: dict, work_dir: Path) -> Path:
    """
    Использует готовый PTO-шаблон. Только заменяет пути к файлам,
    контрольные точки остаются из шаблона.
    """
    if not PTO_TEMPLATE.exists():
        raise FileNotFoundError(f"PTO-шаблон не найден: {PTO_TEMPLATE}")

    pto_work = work_dir / "project.pto"

    # Подставляем актуальные пути
    content = PTO_TEMPLATE.read_text(encoding="utf-8")
    for name, path in images.items():
        content = re.sub(
            r'n"[^"]*' + re.escape(f"{name}.jpg") + r'"',
            f'n"{path.resolve()}"',
            content,
        )
    pto_work.write_text(content, encoding="utf-8")
    print(f"  PTO (шаблон) сохранён: {pto_work}")

    return pto_work


# ---------------------------------------------------------------------------
# Режим 2: автоматические контрольные точки
# ---------------------------------------------------------------------------

def stitch_with_auto_cp(images: dict, work_dir: Path) -> Path:
    """
    Полный автоматический пайплайн:
    pto_gen → cpfind → cpclean → autooptimiser → pano_modify
    """
    img_list = [str(images[n].resolve()) for n in ("LEFT", "CENTER", "RIGHT")]
    pto_init   = work_dir / "01_init.pto"
    pto_cp     = work_dir / "02_cp.pto"
    pto_clean  = work_dir / "03_clean.pto"
    pto_opt    = work_dir / "04_opt.pto"
    pto_final  = work_dir / "05_final.pto"

    # 1. Генерируем начальный PTO
    #    Если есть шаблон — берём из него параметры объектива (FOV, дисторсия),
    #    чтобы не тратить время на их оптимизацию заново.
    if PTO_TEMPLATE.exists():
        # Копируем шаблон и заменяем пути — контрольные точки удалим далее
        content = PTO_TEMPLATE.read_text(encoding="utf-8")
        for name, path in images.items():
            content = re.sub(
                r'n"[^"]*' + re.escape(f"{name}.jpg") + r'"',
                f'n"{path.resolve()}"',
                content,
            )
        # Удаляем старые контрольные точки (строки, начинающиеся с "c ")
        lines = [l for l in content.splitlines() if not l.startswith("c ")]
        pto_init.write_text("\n".join(lines), encoding="utf-8")
        print(f"  Базовый PTO из шаблона (без CP): {pto_init}")
    else:
        # Нет шаблона — генерируем полностью с нуля
        run(
            ["pto_gen", "--projection=1", "--fov=79.6", "-o", str(pto_init)] + img_list,
            "pto_gen: генерация начального проекта"
        )

    # 2. cpfind — поиск контрольных точек (SIFT feature matching)
    run(
        [
            "cpfind",
            "--multirow",          # алгоритм для нескольких рядов
            "--celeste",           # игнорировать небо (облака)
            "-o", str(pto_cp),
            str(pto_init),
        ],
        "cpfind: поиск контрольных точек (SIFT)"
    )

    # 3. cpclean — убираем выбросы
    run(
        ["cpclean", "-o", str(pto_clean), str(pto_cp)],
        "cpclean: фильтрация выбросов"
    )

    # 4. autooptimiser — оптимизация положения камер
    run(
        [
            "autooptimiser",
            "-a",   # оптимизировать углы
            "-l",   # выровнять горизонт
            "-s",   # выровнять по эталонному изображению
            "-o", str(pto_opt),
            str(pto_clean),
        ],
        "autooptimiser: оптимизация проекции"
    )

    # 5. pano_modify — рассчитать размер выходного холста
    run(
        [
            "pano_modify",
            "--canvas=AUTO",
            "--crop=AUTO",
            "-o", str(pto_final),
            str(pto_opt),
        ],
        "pano_modify: вычисление выходного размера"
    )

    return pto_final


# ---------------------------------------------------------------------------
# Общий финальный рендеринг: nona + enblend
# ---------------------------------------------------------------------------

def render(pto: Path, work_dir: Path) -> Path:
    prefix   = str(work_dir / "remapped")
    tmp_out  = work_dir / "panorama.jpg"

    run(
        ["nona", "-o", prefix, "-m", "TIFF_m", str(pto)],
        "nona: перепроецирование кадров"
    )

    tiff_files = sorted(work_dir.glob("remapped*.tif"))
    if not tiff_files:
        raise RuntimeError("nona не создал TIFF-файлы")
    print(f"  Перепроецировано слоёв: {len(tiff_files)}")

    run(
        [
            "enblend",
            "--compression=100",
            "-o", str(tmp_out),
            "--",
            *[str(f) for f in tiff_files],
        ],
        "enblend: финальное сшивание"
    )

    return tmp_out


# ---------------------------------------------------------------------------
# Основной сценарий
# ---------------------------------------------------------------------------

def main():
    mode_str = "автоматические CP (cpfind)" if AUTO_CP else "шаблон PTO"
    print("=" * 62)
    print(f"  Panorama Stitcher — SIYI A8 mini  [{mode_str}]")
    print("=" * 62)

    # Проверяем наличие снимков
    images = {}
    for name in ("LEFT", "CENTER", "RIGHT"):
        p = SHOTS_DIR / f"{name}.jpg"
        if not p.exists():
            print(f"[ОШИБКА] Файл не найден: {p}")
            sys.exit(1)
        images[name] = p

    print(f"\nИсходные снимки : {SHOTS_DIR.resolve()}")
    print(f"Выходной файл   : {OUTPUT_FILE.resolve()}")
    print(f"Режим           : {mode_str}")

    work_dir = Path(tempfile.mkdtemp(prefix="siyi_stitch_"))
    print(f"Рабочая папка   : {work_dir}")

    try:
        # Строим PTO в зависимости от режима
        if AUTO_CP:
            pto = stitch_with_auto_cp(images, work_dir)
        else:
            pto = stitch_with_template(images, work_dir)

        # Рендеринг
        tmp_out = render(pto, work_dir)

        # Копируем результат
        OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tmp_out, OUTPUT_FILE)
        size_mb = OUTPUT_FILE.stat().st_size / 1024 / 1024
        print(f"\n✅ Панорама готова : {OUTPUT_FILE.resolve()}")
        print(f"   Размер файла   : {size_mb:.1f} МБ")

    except Exception as e:
        print(f"\n❌ Ошибка: {e}")
        sys.exit(1)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
