#!/usr/bin/env sh
# PARSER — запуск на Linux и macOS.
#
#   bash run.sh          обычный режим (реальные источники)
#   bash run.sh --demo   демо-режим: источник demo, внешние парсеры не нужны
#
# Порт и адрес: PARSER_PORT / PARSER_HOST из окружения или .env,
# по умолчанию 127.0.0.1:8777.

set -eu

cd "$(dirname "$0")"

PYTHONUTF8=1
PYTHONIOENCODING=utf-8
export PYTHONUTF8 PYTHONIOENCODING

DEMO=0
for arg in "$@"; do
    case "$arg" in
        --demo|-d)
            DEMO=1
            ;;
        -h|--help)
            echo "Использование: bash run.sh [--demo]"
            echo "  --demo   включить демонстрационный источник (PARSER_DEMO=1)"
            exit 0
            ;;
        *)
            echo "Неизвестный аргумент: $arg (см. bash run.sh --help)" >&2
            exit 2
            ;;
    esac
done

if [ "$DEMO" = "1" ]; then
    PARSER_DEMO=1
    export PARSER_DEMO
fi

# Значение KEY=... из .env: export, кавычки и #-комментарий поддерживаются.
env_value() {
    [ -f .env ] || return 0
    _line="$(sed -n "s/^[[:space:]]*\(export[[:space:]]\+\)\?$1[[:space:]]*=[[:space:]]*//p" .env \
        | head -n 1 \
        | tr -d '\r')"
    case "$_line" in
        \"*\") _line="${_line#\"}"; _line="${_line%%\"*}" ;;
        \'*\') _line="${_line#\'}"; _line="${_line%%\'*}" ;;
        *)     _line="${_line%%#*}" ;;
    esac
    printf '%s\n' "$_line" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//'
}

if [ -z "${PARSER_PORT:-}" ]; then
    PARSER_PORT="$(env_value PARSER_PORT)"
fi
PARSER_PORT="${PARSER_PORT:-8777}"

if [ -z "${PARSER_HOST:-}" ]; then
    PARSER_HOST="$(env_value PARSER_HOST)"
fi
PARSER_HOST="${PARSER_HOST:-127.0.0.1}"

export PARSER_PORT PARSER_HOST

case "$PARSER_PORT" in
    ''|*[!0-9]*)
        echo "Некорректный PARSER_PORT: '$PARSER_PORT' — нужно число, например 8777." >&2
        exit 2
        ;;
esac

# Интерпретатор: .venv → python3 → python. Кандидат проверяется запуском:
# заглушка Microsoft Store для python3 не должна выигрывать у рабочего python.
PY=""
for candidate in ".venv/bin/python" python3 python; do
    case "$candidate" in
        .venv/*)
            [ -x "$candidate" ] || continue
            ;;
        *)
            command -v "$candidate" >/dev/null 2>&1 || continue
            ;;
    esac
    if "$candidate" -c 'import sys' >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done

if [ -z "$PY" ]; then
    echo "Python не найден." >&2
    echo "Установите Python 3.10+ (python.org, Homebrew, пакетный менеджер) и запустите снова." >&2
    exit 1
fi

if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "Найден Python старше 3.10: $("$PY" --version 2>&1)" >&2
    echo "Установите Python 3.10 или новее и удалите старый .venv, если он есть." >&2
    exit 1
fi

if ! "$PY" -c 'import fastapi, uvicorn, httpx, bs4' >/dev/null 2>&1; then
    echo
    echo "Зависимости не найдены — устанавливаю: fastapi, uvicorn, httpx, beautifulsoup4"
    if ! "$PY" -m pip install -r requirements.txt; then
        echo
        echo "Не удалось установить зависимости." >&2
        echo "Проверьте интернет и выполните вручную:" >&2
        echo "  $PY -m pip install -r requirements.txt" >&2
        exit 1
    fi
fi

mkdir -p data

if [ "$DEMO" = "1" ]; then
    BANNER="PARSER DEMO"
else
    BANNER="PARSER"
fi

echo
echo "  $BANNER  |  http://$PARSER_HOST:$PARSER_PORT"
echo "  Остановить: Ctrl+C"
echo

exec "$PY" -m uvicorn backend.app:app --host "$PARSER_HOST" --port "$PARSER_PORT"
