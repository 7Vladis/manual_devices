#!/usr/bin/env bash
#
# Обновление установки ManDev.
#
# Сценарий: рядом с рабочим каталогом распакован новый релиз. Скрипт переносит
# в него состояние (.env, вложения, сертификат LDAP), останавливает сервисы,
# меняет каталоги местами и поднимает сервисы заново. Старая версия остаётся
# рядом с отметкой времени — на случай отката.
#
#   ./update.sh                        # релиз в ../manual_devices
#   ./update.sh /path/to/new-release   # явный путь к релизу
#   ./update.sh --dry-run              # показать план, ничего не менять
#   ./update.sh --no-restart           # не трогать docker compose
#   ./update.sh --keep-compose         # оставить docker-compose.yaml старой версии
#
set -euo pipefail

# --- Параметры -------------------------------------------------------------

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELEASE_DIR=""
DRY_RUN=0
RESTART=1
KEEP_COMPOSE=0

# Состояние, которое переносится из рабочей установки в новый релиз.
STATE_REQUIRED=(".env")
STATE_OPTIONAL=("media" "ldap_cert.crt" "logs")

usage() {
    sed -n '3,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)      DRY_RUN=1 ;;
        --no-restart)   RESTART=0 ;;
        --keep-compose) KEEP_COMPOSE=1 ;;
        -h|--help)      usage 0 ;;
        -*)             echo "Неизвестный параметр: $1" >&2; usage 1 ;;
        *)              RELEASE_DIR="$1" ;;
    esac
    shift
done

: "${RELEASE_DIR:=$(dirname "$INSTALL_DIR")/manual_devices}"

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

[[ "$RELEASE_DIR" != "$INSTALL_DIR" ]] || fail "Каталог релиза совпадает с рабочим каталогом: $INSTALL_DIR"
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

info "Обновление завершено"
ok "Рабочая версия:   $INSTALL_DIR"
ok "Прежняя версия:   $BACKUP_DIR (удалите, когда убедитесь, что всё работает)"
[[ $RESTART -eq 0 ]] && warn "Сервисы не перезапускались — сделайте это вручную: docker compose up -d --build"
exit 0
