#!/usr/bin/env bash
#
# Обновление установки ManDev.
#
# Сценарий: рядом с рабочим каталогом распакован новый релиз. Скрипт переносит
# в него состояние (.env, вложения, сертификаты *.crt), останавливает сервисы,
# меняет каталоги местами и поднимает сервисы заново. Старая версия остаётся
# рядом с отметкой времени — на случай отката.
#
# Запускать нужно копию скрипта из рабочей установки: рабочим считается тот
# каталог, в котором лежит сам скрипт, а не текущий каталог оболочки.
#
#   ./update.sh                        # релиз в ../manual_devices
#   ./update.sh /path/to/new-release   # явный путь к релизу
#   ./update.sh --dry-run              # показать план, ничего не менять
#   ./update.sh --no-restart           # не трогать docker compose
#   ./update.sh --keep-compose         # оставить docker-compose.yaml старой версии
#   ./update.sh --keep-old 2           # сколько прежних версий оставить (по умолчанию 1)
#   ./update.sh --no-prune             # не убирать образы без тега
#   ./update.sh --no-self-upgrade      # не передавать работу скрипту из релиза
#
# Если в релизе лежит другой update.sh, работу продолжает он: исправления в
# самом обновлении приезжают с релизом, а запускается копия из рабочей
# установки — без этого правка применялась бы только со следующего раза.
#
# После успешного запуска скрипт прибирает за собой: удаляет прежние версии
# сверх --keep-old и образы без тега, оставшиеся от прежних сборок. Тома docker
# не трогаются ни при каких условиях — в postgres_data лежит база.
#
set -euo pipefail

# --- Параметры -------------------------------------------------------------

# Рабочая установка — каталог, где лежит сам скрипт. Исключение одно:
# самообновление (ниже) передаёт его явно, потому что там работает уже копия
# из релиза, и своим каталогом она считала бы релиз.
INSTALL_DIR="${MANDEV_INSTALL_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
RELEASE_DIR=""
DRY_RUN=0
RESTART=1
KEEP_COMPOSE=0

# Сколько прежних версий оставить рядом. Каждая хранит свою копию вложений,
# поэтому накапливаться им нельзя: один ролик на 20 МБ в пяти копиях — это
# уже сто. Одна копия остаётся на случай отката.
KEEP_OLD=1
PRUNE_IMAGES=1
SELF_UPGRADE=1

# Аргументы как есть: самообновление передаёт их дальше, а разбор ниже их
# «съедает» сдвигами.
ORIGINAL_ARGS=("$@")

# Состояние, которое переносится из рабочей установки в новый релиз.
# Сертификаты в этот список не входят: они переносятся по расширению, а не по
# имени, — см. ниже.
STATE_REQUIRED=(".env")
STATE_OPTIONAL=("media" "logs")

usage() {
    # Справка — это шапка файла: печатаем комментарий со третьей строки и до
    # первой строки кода. По номерам строк не привязываемся — шапка меняется.
    sed -n '3,${/^[^#]/q; s/^# \{0,1\}//p;}' "${BASH_SOURCE[0]}"
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)      DRY_RUN=1 ;;
        --no-restart)   RESTART=0 ;;
        --keep-compose) KEEP_COMPOSE=1 ;;
        --keep-old)     KEEP_OLD="${2:-1}"; shift ;;
        --no-prune)     PRUNE_IMAGES=0 ;;
        --no-self-upgrade) SELF_UPGRADE=0 ;;
        -h|--help)      usage 0 ;;
        -*)             echo "Неизвестный параметр: $1" >&2; usage 1 ;;
        *)              RELEASE_DIR="$1" ;;
    esac
    shift
done

: "${RELEASE_DIR:=$(dirname "$INSTALL_DIR")/manual_devices}"

[[ "$KEEP_OLD" =~ ^[0-9]+$ ]] || { echo "--keep-old ожидает число, получено: $KEEP_OLD" >&2; exit 1; }

# --- Передача работы скрипту из релиза --------------------------------------
#
# Скрипт обновления — такая же часть релиза, как и код: он тоже исправляется.
# Но запускают копию из рабочей установки, то есть предыдущую, и её правки не
# знают. Поэтому, если в релизе update.sh отличается, дальше работает он — с
# теми же аргументами и с явно переданным рабочим каталогом.
#
# Это не расширение доверия: код этого релиза через минуту и так станет
# сервисом. Отключается `--no-self-upgrade`.
if [[ $SELF_UPGRADE -eq 1 && -z "${MANDEV_SELF_UPGRADED:-}" \
      && -d "$RELEASE_DIR" && -f "$RELEASE_DIR/update.sh" \
      && -f "$INSTALL_DIR/update.sh" ]] \
   && ! cmp -s "$RELEASE_DIR/update.sh" "$INSTALL_DIR/update.sh"; then
    printf '\033[0;36m==>\033[0m %s\n' "В релизе другой update.sh — продолжаю им"
    export MANDEV_SELF_UPGRADED=1
    export MANDEV_INSTALL_DIR="$INSTALL_DIR"
    exec bash "$RELEASE_DIR/update.sh" "${ORIGINAL_ARGS[@]}"
fi

# --- Вывод -----------------------------------------------------------------

info()  { printf '\033[0;36m==>\033[0m %s\n' "$*"; }
ok()    { printf '\033[0;32m  ✓\033[0m %s\n' "$*"; }
warn()  { printf '\033[0;33m  !\033[0m %s\n' "$*" >&2; }
fail()  { printf '\033[0;31mОшибка:\033[0m %s\n' "$*" >&2; exit 1; }

run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '      [dry-run] %s\n' "$*"
    else
        "$@"
    fi
}

# --- Проверки до любых изменений -------------------------------------------

info "Проверка предусловий"

[[ -d "$RELEASE_DIR" ]] || fail "Каталог нового релиза не найден: $RELEASE_DIR"
RELEASE_DIR="$(cd "$RELEASE_DIR" && pwd)"

[[ "$RELEASE_DIR" != "$INSTALL_DIR" ]] || fail "каталог релиза совпадает с рабочим: $INSTALL_DIR
       Рабочим считается каталог, в котором лежит сам скрипт. Так бывает, если
       запущена копия update.sh из нового релиза или если рабочая установка
       называется manual_devices — тогда укажите путь к релизу явно:
           ./update.sh /путь/к/новому/релизу"
[[ -f "$RELEASE_DIR/manage.py" ]] || fail "В каталоге релиза нет manage.py — это не проект ManDev: $RELEASE_DIR"
[[ -f "$INSTALL_DIR/manage.py" ]] || fail "В рабочем каталоге нет manage.py: $INSTALL_DIR"

for item in "${STATE_REQUIRED[@]}"; do
    [[ -e "$INSTALL_DIR/$item" ]] || fail "В рабочей установке нет обязательного файла «$item» — обновление отменено."
done

PARENT_DIR="$(dirname "$INSTALL_DIR")"
[[ -w "$PARENT_DIR" ]] || fail "Нет прав на запись в $PARENT_DIR — переименование каталогов невозможно."

BACKUP_DIR="${INSTALL_DIR}_old_$(date +%Y%m%d-%H%M%S)"
[[ -e "$BACKUP_DIR" ]] && fail "Каталог резервной копии уже существует: $BACKUP_DIR"

COMPOSE=""
if [[ $RESTART -eq 1 ]]; then
    if docker compose version >/dev/null 2>&1; then
        COMPOSE="docker compose"
    elif command -v docker-compose >/dev/null 2>&1; then
        COMPOSE="docker-compose"
    else
        warn "docker compose не найден — сервисы не будут остановлены и запущены."
        RESTART=0
    fi
fi

ok "Рабочая установка: $INSTALL_DIR"
ok "Новый релиз:       $RELEASE_DIR"
ok "Резервная копия:   $BACKUP_DIR"
[[ $DRY_RUN -eq 1 ]] && warn "Режим --dry-run: изменения не применяются."

# --- Перенос состояния в новый релиз ---------------------------------------

info "Перенос состояния в новый релиз"

copy_state() {
    local item="$1"
    local src="$INSTALL_DIR/$item"
    local dst="$RELEASE_DIR/$item"

    if [[ ! -e "$src" ]]; then
        return 1
    fi
    if [[ -e "$dst" ]]; then
        run rm -rf "$dst"
    fi
    run cp -a "$src" "$dst"
    return 0
}

for item in "${STATE_REQUIRED[@]}"; do
    copy_state "$item" && ok "перенесено: $item"
done

for item in "${STATE_OPTIONAL[@]}"; do
    if copy_state "$item"; then
        ok "перенесено: $item"
    else
        warn "пропущено (нет в рабочей установке): $item"
    fi
done

# Сертификаты переносятся по расширению, а не по имени. `ldap_cert.crt` —
# только пример из .env.example: путь к нему задаёт AUTH_LDAP_CA_FILE, имя
# бывает любым, и рядом может лежать сертификат другой службы. Потерять его
# при обновлении нельзя — служба просто перестанет подключаться.
shopt -s nullglob
CERTS=("$INSTALL_DIR"/*.crt)
shopt -u nullglob

if (( ${#CERTS[@]} )); then
    for cert in "${CERTS[@]}"; do
        name="$(basename "$cert")"
        copy_state "$name" && ok "перенесено: $name"
    done
else
    warn "пропущено (нет в рабочей установке): сертификаты *.crt"
fi

# docker-compose.yaml — часть релиза: в нём появляются новые сервисы.
# Старую версию сохраняем рядом, но по умолчанию не навязываем.
if [[ -f "$INSTALL_DIR/docker-compose.yaml" ]]; then
    if [[ $KEEP_COMPOSE -eq 1 ]]; then
        run cp -a "$INSTALL_DIR/docker-compose.yaml" "$RELEASE_DIR/docker-compose.yaml"
        warn "docker-compose.yaml взят из старой установки (--keep-compose): новые сервисы релиза могут отсутствовать."
    else
        run cp -a "$INSTALL_DIR/docker-compose.yaml" "$RELEASE_DIR/docker-compose.yaml.previous"
        ok "docker-compose.yaml взят из релиза; прежний сохранён как docker-compose.yaml.previous"
    fi
fi

# --- Остановка сервисов ----------------------------------------------------

if [[ $RESTART -eq 1 ]]; then
    info "Остановка сервисов"
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '      [dry-run] (cd %s && %s down)\n' "$INSTALL_DIR" "$COMPOSE"
    else
        (cd "$INSTALL_DIR" && $COMPOSE down) || warn "Не удалось остановить сервисы — продолжаю."
    fi
fi

# --- Переключение каталогов с откатом ---------------------------------------

info "Переключение версий"

SWITCH_STAGE=0

rollback() {
    local code=$?
    [[ $code -eq 0 ]] && return 0

    warn "Сбой обновления (код $code) — выполняю откат."
    case $SWITCH_STAGE in
        2)
            # Новый релиз уже занял рабочее имя — вернуть обратно.
            [[ -e "$INSTALL_DIR" ]] && mv "$INSTALL_DIR" "$RELEASE_DIR" || true
            [[ -e "$BACKUP_DIR" ]] && mv "$BACKUP_DIR" "$INSTALL_DIR" || true
            ;;
        1)
            # Рабочий каталог переименован, новый ещё не встал на его место.
            [[ -e "$BACKUP_DIR" ]] && mv "$BACKUP_DIR" "$INSTALL_DIR" || true
            ;;
    esac
    warn "Откат завершён. Рабочая версия: $INSTALL_DIR"
    exit "$code"
}
trap rollback EXIT

run mv "$INSTALL_DIR" "$BACKUP_DIR"
SWITCH_STAGE=1
ok "прежняя версия → $(basename "$BACKUP_DIR")"

run mv "$RELEASE_DIR" "$INSTALL_DIR"
SWITCH_STAGE=2
ok "новый релиз → $(basename "$INSTALL_DIR")"

trap - EXIT

# --- Запуск сервисов -------------------------------------------------------

if [[ $RESTART -eq 1 ]]; then
    info "Сборка и запуск сервисов"
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '      [dry-run] (cd %s && %s up -d --build)\n' "$INSTALL_DIR" "$COMPOSE"
    else
        if ! (cd "$INSTALL_DIR" && $COMPOSE up -d --build); then
            warn "Сервисы не поднялись. Прежняя версия лежит в $BACKUP_DIR."
            warn "Откат вручную:  mv '$INSTALL_DIR' '$RELEASE_DIR' && mv '$BACKUP_DIR' '$INSTALL_DIR'"
            exit 1
        fi
    fi
fi

# --- Уборка ----------------------------------------------------------------
#
# Место съедают две вещи. Первая — прежние версии: каждая хранит полную копию
# вложений, и после пятого обновления на диске лежит пять копий всех файлов.
# Вторая — образы docker: каждая сборка выпускает новый образ, а прежний
# остаётся без тега и висит мёртвым грузом.
#
# Тома не трогаем ни при каких условиях: база лежит в postgres_data, и
# `docker volume prune` или `docker system prune --volumes` её бы снесли.
# Здесь вызывается только `docker image prune` — он убирает образы без тега,
# не занятые ни одним контейнером, и до томов не добирается.

info "Уборка"

shopt -s nullglob
OLD_DIRS=("${INSTALL_DIR}"_old_*)
shopt -u nullglob

if (( ${#OLD_DIRS[@]} > KEEP_OLD )); then
    # Имена кончаются отметкой времени, поэтому обычная сортировка ставит
    # свежие в конец: удаляем всё, кроме последних KEEP_OLD.
    mapfile -t OLD_DIRS < <(printf '%s\n' "${OLD_DIRS[@]}" | sort)
    DOOMED=("${OLD_DIRS[@]:0:${#OLD_DIRS[@]}-KEEP_OLD}")

    for dir in "${DOOMED[@]}"; do
        # Страховка перед rm -rf: удаляем только то, что похоже на нашу
        # прежнюю версию, а не любой каталог с подходящим именем.
        if [[ ! -f "$dir/manage.py" ]]; then
            warn "пропущено (не похоже на версию ManDev): $dir"
            continue
        fi
        size="$(du -sh "$dir" 2>/dev/null | cut -f1)"
        run rm -rf "$dir"
        ok "удалена прежняя версия: $(basename "$dir") (${size:-?})"
    done
else
    ok "прежних версий: ${#OLD_DIRS[@]} — удалять нечего (оставляем $KEEP_OLD)"
fi

if [[ $PRUNE_IMAGES -eq 1 && -n "$COMPOSE" ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '      [dry-run] docker image prune -f\n'
    else
        freed="$(docker image prune -f 2>/dev/null | tail -n 1)"
        ok "образы без тега убраны${freed:+ — $freed}"
    fi
else
    [[ $PRUNE_IMAGES -eq 0 ]] && ok "образы не трогаем (--no-prune)"
fi

info "Обновление завершено"
ok "Рабочая версия:   $INSTALL_DIR"
ok "Прежняя версия:   $BACKUP_DIR (удалите, когда убедитесь, что всё работает)"
[[ $RESTART -eq 0 ]] && warn "Сервисы не перезапускались — сделайте это вручную: docker compose up -d --build"
exit 0
