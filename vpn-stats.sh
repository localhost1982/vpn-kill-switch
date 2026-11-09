#!/usr/bin/env bash
# Статистика обрывов VPN-L2TP и пропаданий интернета за последние N часов.
# Источник данных: журнал systemd (journalctl). Вывод: таблицы в формате Markdown.
# Запуск: ./vpn-stats.sh [часы] [--events]   (подробности: ./vpn-stats.sh --help)

set -euo pipefail

# Десятичная точка в выводе awk не должна зависеть от локали.
export LC_NUMERIC=C

VPN_NAME="VPN-L2TP"
DEFAULT_HOURS=1

# --- Шаблоны строк журнала (часть после «unit[pid]: ») -----------------------
# Один и тот же набор используется и в grep (подсчёт), и в awk (через ENVIRON).
export RE_START="vpn\\[[^]]*\"VPN-L2TP\"\\]: starting l2tp"
export RE_UP="policy: set 'VPN-L2TP' \\(ppp[0-9]+\\) as default for IPv4"
export RE_FAIL="vpn\\[[^]]*\"VPN-L2TP\"\\]: failed to connect"
export RE_NOAGENT="secrets: failed to request VPN secrets"
export RE_LCP="pppd\\[[0-9]+\\]: No response to [0-9]+ echo-requests"
export RE_TERM="pppd\\[[0-9]+\\]: Terminating on signal"
export RE_CONN="pppd\\[[0-9]+\\]: Connection terminated\\."
export RE_WIFI_RESET="secondaries -> failed \\(reason 'secondary-connection-failed'"
export RE_WIFI_DOWN="device \\(wl[^)]*\\): state change: activated -> "
export RE_SLEEP="kernel: PM: suspend entry"
export RE_WAKE="kernel: PM: suspend exit"
export RE_NM_STOP="systemd\\[1\\]: Stopped NetworkManager\\.service"
export RE_BOOT="kernel: Linux version "
export RE_HELLO="message=recv message_type=avpMsgTypeHello"

usage() {
    cat <<'USAGE'
Использование: vpn-stats.sh [часы] [--events] [-h|--help]

Статистика обрывов VPN-L2TP и пропаданий интернета за последние N часов
(таблицы в формате Markdown, источник данных: журнал systemd).

  часы       период в часах, по умолчанию 1. Допускаются десятичная точка и запятая
             (1.5, 1,5). Шаг 0.5 ч: значение округляется математически до ближайших
             полчаса (0.7 -> 0.5, 0.75 -> 1, 1.2 -> 1, 1.3 -> 1.5). Если после
             округления получился 0 или значение не число, берётся 1 ч.
  --events   после таблиц вывести хронологию событий (подключения, обрывы, сон,
             перезапуски NetworkManager, загрузки, сбои подключения)
  -h, --help показать эту справку

Примеры:
  ./vpn-stats.sh            # последний час
  ./vpn-stats.sh 0.5        # последние полчаса
  ./vpn-stats.sh 1,5        # полтора часа
  ./vpn-stats.sh 3 --events # три часа с хронологией

sudo не нужен, кроме строки со счётчиком заблокированных пакетов kill switch:
без sudo без пароля в ней будет «n/a (нужен sudo)». Пароль скрипт не запрашивает.
USAGE
}

# --- Разбор аргумента «часы» ---------------------------------------------------
# Результат: глобальные HALFHOURS (число получасов, >= 1) и NOTE (пояснение или пусто).
parse_hours() {
    local raw=$1 norm
    NOTE=""
    norm=${raw//,/.}
    if [[ ! $norm =~ ^([0-9]+\.?[0-9]*|\.[0-9]+)$ ]]; then
        NOTE="Параметр «${raw}» не распознан, использую ${DEFAULT_HOURS} ч."
        HALFHOURS=$((DEFAULT_HOURS * 2))
        return
    fi
    # Математическое округление до ближайших 0.5 ч: int(h*2 + 0.5).
    HALFHOURS=$(awk -v h="$norm" 'BEGIN { printf "%d", h * 2 + 0.5 }')
    if (( HALFHOURS == 0 )); then
        NOTE="Значение ${raw} округляется до 0, использую ${DEFAULT_HOURS} ч."
        HALFHOURS=$((DEFAULT_HOURS * 2))
    fi
}

# Подпись периода: 0.5 ч, 1 ч, 1.5 ч, 2 ч (без «.0»).
period_label() {
    local hh=$1
    if (( hh % 2 == 0 )); then
        printf '%d ч' $((hh / 2))
    else
        printf '%d.5 ч' $((hh / 2))
    fi
}

# Количество строк журнала, подходящих под регулярное выражение (всегда одно число).
count() {
    grep -cE -- "$1" "$JOURNAL" || true
}

# Секунды -> «12 с» / «3 мин 05 с» / «1 ч 02 мин».
fmt_duration() {
    local s=$1
    if (( s < 60 )); then
        printf '%d с' "$s"
    elif (( s < 3600 )); then
        printf '%d мин %02d с' $((s / 60)) $((s % 60))
    else
        printf '%d ч %02d мин' $((s / 3600)) $(((s % 3600) / 60))
    fi
}

# Эпоха -> локальное время.
fmt_time() { date -d "@$1" +%H:%M:%S; }
fmt_datetime() { date -d "@$1" '+%Y-%m-%d %H:%M'; }

# Подключён ли VPN прямо сейчас (0 = да).
vpn_active() {
    local names
    names=$(nmcli -t -f NAME connection show --active 2>/dev/null || true)
    grep -Fxq -- "$VPN_NAME" <<<"$names"
}

# Статус юнита systemd -> «активен» / «не активен» / «недоступен» (если нет ответа).
unit_state() {
    local out
    out=$("$@" 2>/dev/null || true)
    case $out in
        active) echo "активен" ;;
        "")     echo "недоступен" ;;
        *)      echo "не активен" ;;
    esac
}

# Число заблокированных kill switch пакетов; без sudo без пароля -> «n/a (нужен sudo)».
killswitch_drops() {
    local out pkts=""
    if out=$(sudo -n nft list chain inet killswitch output 2>/dev/null); then
        pkts=$(grep -E 'drop' <<<"$out" | grep -oE 'counter packets [0-9]+' | head -n1 | awk '{ print $3 }' || true)
    fi
    if [[ -n $pkts ]]; then
        echo "$pkts"
    else
        echo "n/a (нужен sudo)"
    fi
}

# --- awk: разбор журнала ------------------------------------------------------
# На вход: журнал в формате short-unix. На выходе строки с табуляцией:
#   E <эпоха> <описание>      событие для режима --events
#   D <эпоха> <причина>       обрыв VPN (LCP / сон / команда / прочее)
#   G <итого> <макс> <с> <по> простои: суммарно, самый долгий (секунды) и его границы
# Параметры: -v now=<эпоха> -v active=<0|1>; шаблоны берутся из окружения (RE_*).
read -r -d '' AWK_PROG <<'AWK' || true
function add(t, kind, pid) { n++; T[n] = t; K[n] = kind; P[n] = pid }
function pppd_pid(   s) {
    if (match($0, /pppd\[[0-9]+\]/)) { s = substr($0, RSTART + 5, RLENGTH - 6); return s }
    return ""
}
function close_gap(end,   dur) {
    dur = gacc
    total += dur
    if (dur > longest) { longest = dur; lstart = gstart; lend = end }
}
BEGIN {
    re_up = ENVIRON["RE_UP"];       re_fail = ENVIRON["RE_FAIL"]
    re_noagent = ENVIRON["RE_NOAGENT"]
    re_lcp = ENVIRON["RE_LCP"];     re_term = ENVIRON["RE_TERM"]
    re_conn = ENVIRON["RE_CONN"];   re_sleep = ENVIRON["RE_SLEEP"]
    re_wake = ENVIRON["RE_WAKE"];   re_nm = ENVIRON["RE_NM_STOP"]
    re_boot = ENVIRON["RE_BOOT"]
}
# Первый проход: собираем значимые события в хронологическом порядке.
{
    t = $1 + 0
    if      ($0 ~ re_up)      add(t, "UP", "")
    else if ($0 ~ re_lcp)     add(t, "LCP", pppd_pid())
    else if ($0 ~ re_term)    add(t, "TERM", pppd_pid())
    else if ($0 ~ re_conn)    add(t, "CONN", pppd_pid())
    else if ($0 ~ re_fail)    add(t, "FAIL", "")
    else if ($0 ~ re_noagent) add(t, "NOAGENT", "")
    else if ($0 ~ re_sleep)   add(t, "SLEEP", "")
    else if ($0 ~ re_wake)    add(t, "WAKE", "")
    else if ($0 ~ re_nm)      add(t, "NMSTOP", "")
    else if ($0 ~ re_boot)    add(t, "BOOT", "")
}
END {
    # Моменты ухода в сон нужны, чтобы отличить «Terminating» из-за сна от ручного.
    ns = 0
    for (i = 1; i <= n; i++) if (K[i] == "SLEEP") SL[++ns] = T[i]

    # Классификация обрывов. «Connection terminated» после «No response» или
    # «Terminating» того же pppd - продолжение того же обрыва (isdup).
    for (i = 1; i <= n; i++) {
        k = K[i]; isdup[i] = 0; R[i] = ""
        if (k == "LCP") { R[i] = "LCP"; seen[P[i]] = 1 }
        else if (k == "TERM") {
            R[i] = "команда"
            for (j = 1; j <= ns; j++) {
                d = T[i] - SL[j]; if (d < 0) d = -d
                if (d <= 5) { R[i] = "сон"; break }
            }
            seen[P[i]] = 1
        } else if (k == "CONN") {
            if (P[i] in seen) isdup[i] = 1; else R[i] = "прочее"
            seen[P[i]] = 1
        }
    }

    # Конечный автомат простоя. known: известно ли состояние VPN (до первого
    # события VPN в окне ничего не считаем); down: VPN лежит; asleep: ноутбук спит.
    known = 0; down = 0; asleep = 0; total = 0; longest = 0; lstart = 0; lend = 0
    for (i = 1; i <= n; i++) {
        k = K[i]; t = T[i]
        if (k == "UP") {
            if (!known) { known = 1; down = 0 }
            else if (down) {
                if (!asleep) gacc += t - cs
                close_gap(t); down = 0
            }
        } else if ((k == "LCP" || k == "TERM" || (k == "CONN" && !isdup[i])) && !down) {
            known = 1; down = 1; gstart = t; gacc = 0; cs = t
        } else if (k == "SLEEP") {
            if (!asleep) { if (down) gacc += t - cs; asleep = 1 }
        } else if (k == "WAKE") {
            if (asleep) { asleep = 0; if (down) cs = t }
        }
    }
    # Незакрытый простой: считаем до «сейчас», только если VPN сейчас не подключён.
    if (down && !active) {
        if (!asleep) gacc += now - cs
        close_gap(now)
    }

    # Вывод событий и обрывов.
    for (i = 1; i <= n; i++) {
        k = K[i]; t = T[i]; txt = ""
        if (k == "UP") txt = "VPN подключён"
        else if (k == "LCP" || k == "TERM" || (k == "CONN" && !isdup[i])) {
            if (R[i] == "LCP") txt = "обрыв VPN: сервер перестал отвечать (LCP)"
            else if (R[i] == "сон") txt = "обрыв VPN: уход в сон"
            else if (R[i] == "команда") txt = "обрыв VPN: остановлен по команде"
            else txt = "обрыв VPN: туннель завершён (прочее)"
            printf "D\t%.0f\t%s\n", int(t), R[i]
        }
        else if (k == "SLEEP") txt = "уход в сон"
        else if (k == "WAKE") txt = "выход из сна"
        else if (k == "NMSTOP") txt = "перезапуск NetworkManager"
        else if (k == "BOOT") txt = "загрузка системы"
        else if (k == "FAIL") txt = "не удалось подключить VPN (таймаут)"
        else if (k == "NOAGENT") txt = "не удалось запросить пароль VPN (нет агента)"
        if (txt != "") printf "E\t%.0f\t%s\n", int(t), txt
    }
    printf "G\t%.0f\t%.0f\t%.0f\t%.0f\n", total, longest, lstart, lend
}
AWK

# --- Основной код -------------------------------------------------------------
SHOW_EVENTS=0
ARG_HOURS=""
HAVE_ARG=0

while (( $# > 0 )); do
    case $1 in
        -h|--help) usage; exit 0 ;;
        --events)  SHOW_EVENTS=1 ;;
        -[0-9.,]*|[0-9.,]*|[!-]*)
            # Отрицательные числа и любой не-флаг считаем значением «часы»,
            # чтобы «-1» получило пояснение, а не ошибку.
            if (( HAVE_ARG )); then
                echo "Лишний аргумент: $1" >&2
                usage >&2
                exit 2
            fi
            ARG_HOURS=$1; HAVE_ARG=1 ;;
        *)
            echo "Неизвестный параметр: $1" >&2
            usage >&2
            exit 2 ;;
    esac
    shift
done

if (( HAVE_ARG )); then
    parse_hours "$ARG_HOURS"
else
    NOTE=""
    HALFHOURS=$((DEFAULT_HOURS * 2))
fi
MINUTES=$((HALFHOURS * 30))

JOURNAL=$(mktemp)
trap 'rm -f "$JOURNAL"' EXIT

NOW=$(date +%s)
if ! journalctl --since "-${MINUTES}min" -o short-unix --no-pager -q >"$JOURNAL" 2>/dev/null; then
    echo "Не удалось прочитать журнал (journalctl)." >&2
    exit 1
fi

START=$((NOW - MINUTES * 60))
if [[ $(date -d "@$START" +%F) == "$(date -d "@$NOW" +%F)" ]]; then
    PERIOD="$(fmt_datetime "$START") – $(date -d "@$NOW" +%H:%M)"
else
    PERIOD="$(fmt_datetime "$START") – $(fmt_datetime "$NOW")"
fi

if vpn_active; then VPN_ACTIVE=1; else VPN_ACTIVE=0; fi

# Разбор журнала автоматом: события, обрывы, простои.
RESULT=$(awk -v now="$NOW" -v active="$VPN_ACTIVE" "$AWK_PROG" "$JOURNAL")

# Счётчики.
LCP=$(count "$RE_LCP")
SIGNAL=$(count "$RE_TERM")
CONN=$(count "$RE_CONN")
TOTAL=$(( CONN > LCP + SIGNAL ? CONN : LCP + SIGNAL ))
OTHER=$((TOTAL - LCP - SIGNAL))
STARTS=$(count "$RE_START")
OKS=$(count "$RE_UP")
FAILS=$(count "$RE_FAIL")
NOAGENTS=$(count "$RE_NOAGENT")
HELLOS=$(count "$RE_HELLO")
WIFI_DOWN=$(count "$RE_WIFI_DOWN")
WIFI_RESET=$(count "$RE_WIFI_RESET")
SLEEPS=$(count "$RE_SLEEP")
NM_RESTARTS=$(count "$RE_NM_STOP")
BOOTS=$(count "$RE_BOOT")

# Простои.
IFS=$'\t' read -r _ DOWN_TOTAL DOWN_MAX GAP_FROM GAP_TO < <(awk -F'\t' '$1 == "G"' <<<"$RESULT")
DOWN_TOTAL_TXT=$(fmt_duration "$DOWN_TOTAL")
DOWN_MAX_TXT=$(fmt_duration "$DOWN_MAX")
if (( DOWN_MAX > 0 )); then
    DOWN_MAX_TXT+=" ($(fmt_time "$GAP_FROM") – $(fmt_time "$GAP_TO"))"
fi

# Последний Hello от сервера.
HELLO_TXT="$HELLOS"
if (( HELLOS > 0 )); then
    LAST_HELLO=$(grep -E -- "$RE_HELLO" "$JOURNAL" | tail -n1 | cut -d' ' -f1 || true)
    HELLO_TXT+=" (последний $(fmt_time "${LAST_HELLO%%.*}"))"
fi

# Последние 5 обрывов, самые свежие первыми.
RECENT=""
while IFS=$'\t' read -r _ ts reason; do
    [[ -n $ts ]] || continue
    RECENT+="${RECENT:+, }$(fmt_time "$ts") ($reason)"
done < <(awk -F'\t' '$1 == "D"' <<<"$RESULT" | tail -n 5 | tac)
[[ -n $RECENT ]] || RECENT="—"

# --- Вывод --------------------------------------------------------------------
if [[ -n $NOTE ]]; then
    printf '%s\n\n' "$NOTE"
fi

printf '**Период:** %s (%s)\n\n' "$PERIOD" "$(period_label "$HALFHOURS")"

echo "### Статистика"
echo
echo "| Показатель | Значение |"
echo "|---|---:|"
echo "| Разрывов VPN всего | $TOTAL |"
echo "|   • сервер перестал отвечать (LCP) | $LCP |"
echo "|   • по команде: сон, nmcli down, сторож | $SIGNAL |"
echo "|   • прочие | $OTHER |"
echo "| Попыток подключения VPN | $STARTS |"
echo "| Успешных подключений | $OKS |"
echo "| Неудачных подключений (таймаут) | $FAILS |"
echo "| Ошибок запроса пароля (нет агента) | $NOAGENTS |"
echo "| Суммарно без VPN | $DOWN_TOTAL_TXT |"
echo "| Самый долгий простой | $DOWN_MAX_TXT |"
echo "| Hello от сервера L2TP | $HELLO_TXT |"
echo "| Wi-Fi: отключений | $WIFI_DOWN |"
echo "| Wi-Fi: сбросов из-за неудачи VPN | $WIFI_RESET |"
echo "| Уходов в сон | $SLEEPS |"
echo "| Перезапусков NetworkManager | $NM_RESTARTS |"
echo "| Загрузок системы | $BOOTS |"
echo "| Последние обрывы | $RECENT |"
echo

if (( VPN_ACTIVE )); then VPN_NOW="подключён"; else VPN_NOW="не подключён"; fi
echo "### Сейчас"
echo
echo "| Показатель | Значение |"
echo "|---|---:|"
echo "| $VPN_NAME | $VPN_NOW |"
echo "| Kill switch (vpn-killswitch.service) | $(unit_state systemctl is-active vpn-killswitch.service) |"
echo "| Сторож (old-vpn-watchdog.service) | $(unit_state systemctl --user is-active old-vpn-watchdog.service) |"
echo "| Заблокировано kill switch, пакетов | $(killswitch_drops) |"

if (( SHOW_EVENTS )); then
    echo
    echo "**События:**"
    echo
    EV_COUNT=0
    while IFS=$'\t' read -r _ ts text; do
        [[ -n $ts ]] || continue
        EV_COUNT=$((EV_COUNT + 1))
        printf -- '- %s  %s\n' "$(fmt_time "$ts")" "$text"
    done < <(awk -F'\t' '$1 == "E"' <<<"$RESULT")
    if (( EV_COUNT == 0 )); then
        echo "- событий нет"
    fi
fi

exit 0
