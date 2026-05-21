#!/usr/bin/env bash

# Скрипт автоматического развертывания Telegram-бота на Linux (Systemd)
# Все логи, интерактивный ввод и комментарии выполнены на русском языке.

set -e

# Цвета для красивого вывода
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;36m'
NC='\033[0m'

REPO_URL="https://github.com/W1neus/vavilov_schedule_bot.git"
SERVICE_NAME="pdf-tgbot"
SERVICE_PATH="/etc/systemd/system/$SERVICE_NAME.service"

echo -e "${BLUE}====================================================${NC}"
echo -e "${BLUE}    SGAU Vavilov Schedule Bot — Управление          ${NC}"
echo -e "${BLUE}====================================================${NC}"

# --- Проверка root ---
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Ошибка: запустите скрипт с правами root: sudo bash $0${NC}"
    exit 1
fi

REAL_USER=${SUDO_USER:-root}

# --- Определяем INSTALL_DIR ---
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Автоматически мигрируем из старой папки /opt/pdf-tgbot
if [ -d "/opt/pdf-tgbot" ] && [ ! -d "/opt/vavilov_schedule_bot" ]; then
    echo -e "${YELLOW}Обнаружена старая папка /opt/pdf-tgbot → переносим в /opt/vavilov_schedule_bot...${NC}"
    mv "/opt/pdf-tgbot" "/opt/vavilov_schedule_bot"
fi

if [ -f "$SCRIPT_DIR/tgbot/main.py" ] && [ -f "$SCRIPT_DIR/requirements.txt" ]; then
    INSTALL_DIR="$SCRIPT_DIR"
else
    INSTALL_DIR="/opt/vavilov_schedule_bot"
fi

ALREADY_INSTALLED=0
if [ -d "$INSTALL_DIR" ] && [ -f "$INSTALL_DIR/tgbot/main.py" ]; then
    ALREADY_INSTALLED=1
fi

# --- Меню ---
if [ "$ALREADY_INSTALLED" -eq 1 ]; then
    echo -e ""
    echo -e "Обнаружена существующая установка в ${BLUE}$INSTALL_DIR${NC}"
    echo -e ""
    echo -e "  ${GREEN}1)${NC} Обновить бота (быстро: git pull + перезапуск)"
    echo -e "  ${YELLOW}2)${NC} Полная переустановка (пересоздать venv, настроить .env)"
    echo -e "  ${RED}3)${NC} Выйти"
    echo -e ""
    read -p "Выберите действие [1]: " CHOICE
    CHOICE=${CHOICE:-1}
else
    echo -e ""
    echo -e "Установка не обнаружена. Запускаем первоначальную установку..."
    CHOICE=2
fi

# =====================================================================
# РЕЖИМ 1: БЫСТРОЕ ОБНОВЛЕНИЕ
# =====================================================================
if [ "$CHOICE" = "1" ]; then
    echo -e ""
    echo -e "${BLUE}=== Обновление бота ===${NC}"

    cd "$INSTALL_DIR"

    echo -e "${BLUE}[1/3] Скачиваем последние изменения из GitHub...${NC}"
    git config --global --add safe.directory "$INSTALL_DIR" || true
    git fetch origin
    git reset --hard origin/main
    echo -e "${GREEN}Код обновлён!${NC}"

    # Автоматически добавляем настройку MAX_WORKERS в существующий .env, если её там нет
    ENV_PATH="$INSTALL_DIR/tgbot/.env"
    if [ -f "$ENV_PATH" ] && ! grep -q "MAX_WORKERS" "$ENV_PATH"; then
        echo -e "\n# Лимит параллельных потоков для парсинга PDF" >> "$ENV_PATH"
        echo -e "MAX_WORKERS=4" >> "$ENV_PATH"
        echo -e "${YELLOW}Добавлена настройка производительности (по умолчанию 4 потока).${NC}"
    fi

    echo -e "${BLUE}[2/3] Обновляем Python-зависимости (если изменились)...${NC}"
    venv/bin/pip install -q --upgrade pip
    venv/bin/pip install -q -r requirements.txt
    echo -e "${GREEN}Зависимости готовы!${NC}"

    echo -e "${BLUE}[3/3] Перезапускаем сервис...${NC}"
    echo -e "${YELLOW}Подсказка: перезапуск может занять до 15-20 секунд, если старые процессы бота зависли. Пожалуйста, подождите...${NC}"
    systemctl restart "$SERVICE_NAME.service"
    sleep 3
    STATUS=$(systemctl is-active "$SERVICE_NAME.service")
    if [ "$STATUS" = "active" ]; then
        echo -e "${GREEN}✅ Бот успешно обновлён и запущен!${NC}"
    else
        echo -e "${RED}⚠️  Сервис не запустился. Проверьте логи:${NC}"
        echo -e "  ${YELLOW}journalctl -u $SERVICE_NAME -n 30 --no-pager${NC}"
    fi
    echo -e "${GREEN}====================================================${NC}"
    exit 0
fi

# =====================================================================
# РЕЖИМ 3: ВЫХОД
# =====================================================================
if [ "$CHOICE" = "3" ]; then
    echo -e "Выход."
    exit 0
fi

# =====================================================================
# РЕЖИМ 2: ПОЛНАЯ УСТАНОВКА / ПЕРЕУСТАНОВКА
# =====================================================================
echo -e ""
echo -e "${BLUE}=== Полная установка ===${NC}"

# --- Клонирование или обновление репозитория ---
if [ ! -d "$INSTALL_DIR" ]; then
    echo -e "${BLUE}Клонирование репозитория в $INSTALL_DIR...${NC}"
    git clone "$REPO_URL" "$INSTALL_DIR"
elif [ "$INSTALL_DIR" != "$SCRIPT_DIR" ]; then
    echo -e "${YELLOW}Папка уже существует. Обновляем код...${NC}"
    cd "$INSTALL_DIR"
    git config --global --add safe.directory "$INSTALL_DIR" || true
    git fetch origin
    git reset --hard origin/main
fi

# --- Системные зависимости ---
echo -e "${BLUE}Установка системных пакетов...${NC}"
if command -v apt-get &> /dev/null; then
    apt-get update -y -q
    apt-get install -y -q python3 python3-pip python3-venv git build-essential libjpeg-dev zlib1g-dev
elif command -v dnf &> /dev/null; then
    dnf install -y python3 python3-pip git gcc python3-devel libjpeg-devel zlib-devel
else
    echo -e "${YELLOW}Предупреждение: пакетный менеджер не найден. Убедитесь, что python3, pip, venv и git установлены.${NC}"
fi

# --- Виртуальное окружение ---
echo -e "${BLUE}Создание виртуального окружения Python...${NC}"
cd "$INSTALL_DIR"
python3 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt
echo -e "${GREEN}Зависимости установлены!${NC}"

# --- Настройка .env ---
ENV_PATH="$INSTALL_DIR/tgbot/.env"

if [ -f "$ENV_PATH" ]; then
    echo -e "${GREEN}Файл .env уже существует.${NC}"
    read -p "Перезаписать настройки (BOT_TOKEN, ADMIN_IDS)? (y/n) [n]: " REWRITE_ENV
    REWRITE_ENV=${REWRITE_ENV:-n}
else
    REWRITE_ENV="y"
fi

if [ "$REWRITE_ENV" = "y" ] || [ "$REWRITE_ENV" = "Y" ]; then
    echo -e "${YELLOW}=== Настройка конфигурации ===${NC}"

    ATTEMPTS=0
    while [ -z "$INPUT_TOKEN" ]; do
        read -p "Токен Telegram бота (BOT_TOKEN): " INPUT_TOKEN
        if [ -z "$INPUT_TOKEN" ]; then
            echo -e "${RED}Токен не может быть пустым!${NC}"
            ATTEMPTS=$((ATTEMPTS+1))
            if [ "$ATTEMPTS" -ge 5 ]; then
                echo -e "${RED}Не удалось прочитать ввод. Скачайте скрипт и запустите локально:${NC}"
                echo -e "  curl -LO https://raw.githubusercontent.com/W1neus/vavilov_schedule_bot/main/setup.sh && sudo bash setup.sh"
                exit 1
            fi
        fi
    done

    ATTEMPTS=0
    while [ -z "$INPUT_ADMINS" ]; do
        read -p "ID администраторов через запятую (например: 123456789,987654321): " INPUT_ADMINS
        if [ -z "$INPUT_ADMINS" ]; then
            echo -e "${RED}Поле не может быть пустым!${NC}"
            ATTEMPTS=$((ATTEMPTS+1))
            if [ "$ATTEMPTS" -ge 5 ]; then
                echo -e "${RED}Не удалось прочитать ввод.${NC}"
                exit 1
            fi
        fi
    done

    # Настройка производительности парсера
    echo -e ""
    echo -e "${YELLOW}=== Настройка производительности ===${NC}"
    echo -e "Выберите мощность вашего сервера для парсинга расписания:"
    echo -e "  ${GREEN}1)${NC} Слабый VPS (1 ядро, до 1 Гб RAM) -> 1 поток (медленно, но 100% стабильно)"
    echo -e "  ${GREEN}2)${NC} Средний сервер (2-4 ядра, 2 Гб+ RAM) -> 4 потока (${YELLOW}рекомендуется${NC})"
    echo -e "  ${GREEN}3)${NC} Мощный сервер (8+ ядер) -> 8 потоков (очень быстро)"
    echo -e ""
    read -p "Выберите вариант [2]: " PERF_CHOICE
    PERF_CHOICE=${PERF_CHOICE:-2}
    
    if [ "$PERF_CHOICE" = "1" ]; then
        INPUT_WORKERS=1
    elif [ "$PERF_CHOICE" = "3" ]; then
        INPUT_WORKERS=8
    else
        INPUT_WORKERS=4
    fi
    echo -e "Выбран лимит: ${BLUE}$INPUT_WORKERS${NC} параллельных потоков."

    cat > "$ENV_PATH" << EOF
# Токен Telegram-бота
BOT_TOKEN=$INPUT_TOKEN

# ID администраторов через запятую
ADMIN_IDS=$INPUT_ADMINS

# Лимит параллельных потоков для парсинга PDF
MAX_WORKERS=$INPUT_WORKERS
EOF
    echo -e "${GREEN}Конфигурация сохранена в $ENV_PATH${NC}"
fi

# --- Права доступа ---
chown -R "$REAL_USER:$REAL_USER" "$INSTALL_DIR"
chmod 600 "$ENV_PATH"

# --- Systemd сервис ---
echo -e "${BLUE}Настройка Systemd сервиса...${NC}"
cat > "$SERVICE_PATH" << EOF
[Unit]
Description=Telegram PDF Parser Bot Service
After=network.target

[Service]
Type=simple
User=$REAL_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/tgbot/main.py
Restart=always
RestartSec=5
StandardOutput=syslog
StandardError=syslog
SyslogIdentifier=$SERVICE_NAME

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME.service"
echo -e "${YELLOW}Подсказка: перезапуск может занять до 15-20 секунд, если старые процессы бота зависли. Пожалуйста, подождите...${NC}"
systemctl restart "$SERVICE_NAME.service"

sleep 3
STATUS=$(systemctl is-active "$SERVICE_NAME.service")

echo -e "${GREEN}====================================================${NC}"
if [ "$STATUS" = "active" ]; then
    echo -e "${GREEN}🎉 Установка завершена! Бот запущен и работает.${NC}"
else
    echo -e "${YELLOW}⚠️  Установка завершена, но сервис не поднялся. Проверьте логи:${NC}"
    echo -e "   ${YELLOW}journalctl -u $SERVICE_NAME -n 50 --no-pager${NC}"
fi
echo -e "${GREEN}====================================================${NC}"
echo -e "📍 Директория: ${BLUE}$INSTALL_DIR${NC}"
echo -e "👤 Пользователь: ${BLUE}$REAL_USER${NC}"
echo -e "🤖 Сервис: ${BLUE}$SERVICE_NAME${NC}"
echo -e ""
echo -e "📌 Полезные команды:"
echo -e "  Статус:    ${YELLOW}systemctl status $SERVICE_NAME${NC}"
echo -e "  Перезапуск: ${YELLOW}systemctl restart $SERVICE_NAME${NC}"
echo -e "  Логи:      ${YELLOW}journalctl -u $SERVICE_NAME -f -n 100${NC}"
echo -e "  Обновить:  ${YELLOW}curl -LO https://raw.githubusercontent.com/W1neus/vavilov_schedule_bot/main/setup.sh && sudo bash setup.sh${NC}"
echo -e "===================================================="
