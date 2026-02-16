import datetime
import requests
import pdfplumber
import io
import logging
from bs4 import BeautifulSoup
from urllib.parse import unquote, urljoin

from config import (
    SCHEDULE_URL, 
    SEARCH_QUERY_PDF, 
    SEMESTER_START_DATE, 
    TZ_SARATOV, 
    TIME_START_TO_PAIR_NUM, 
    HEADERS
)

logger = logging.getLogger(__name__)

# ================= ПАРСЕР (СИНХРОННАЯ ЧАСТЬ) =================

def get_week_parity(target_date=None):
    if target_date is None: target_date = datetime.datetime.now(TZ_SARATOV).date()
    delta = target_date - SEMESTER_START_DATE
    return (delta.days // 7) % 2

def find_all_pdf_links():
    """Находит ВСЕ ссылки на PDF, соответствующие запросу"""
    try:
        response = requests.get(SCHEDULE_URL, headers=HEADERS, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        query = SEARCH_QUERY_PDF.lower().replace(" ", "")
        links = []
        for a in soup.find_all('a', href=True):
            full_url = urljoin(SCHEDULE_URL, a['href'])
            decoded_url = unquote(full_url).lower().replace(" ", "")
            if '.pdf' in decoded_url and query in decoded_url:
                links.append(full_url)
        return list(set(links)) 
    except Exception as e:
        logger.error(f"Поиск ссылок PDF: {e}")
        return []

def process_time_block(rows, pair_num, day_idx, groups_data):
    if not rows or pair_num == -1 or day_idx == -1: return

    target_groups = ["Б-ПИ-101", "Б-ПИ-102"]

    for g_idx, g_name in enumerate(target_groups):
        col_idx = 2 + g_idx
        
        texts = []
        for r in rows:
            if col_idx >= len(r):
                val = None
            else:
                val = r[col_idx]
            
            final_text = None
            
            if val is None:
                if g_name == "Б-ПИ-102":
                    if 2 < len(r) and r[2]:
                        final_text = str(r[2]).strip().replace('\n', ' ')
            elif val == "":
                final_text = None
            else:
                final_text = str(val).strip().replace('\n', ' ')
            
            texts.append(final_text)

        if len(texts) == 1:
            val = texts[0]
            if val:
                if day_idx not in groups_data[g_name][0]: groups_data[g_name][0][day_idx] = {}
                if day_idx not in groups_data[g_name][1]: groups_data[g_name][1][day_idx] = {}
                groups_data[g_name][0][day_idx][pair_num] = val
                groups_data[g_name][1][day_idx][pair_num] = val
        
        elif len(texts) >= 2:
            top_val = texts[0]
            bot_val = texts[1]

            if top_val:
                if day_idx not in groups_data[g_name][0]: groups_data[g_name][0][day_idx] = {}
                groups_data[g_name][0][day_idx][pair_num] = top_val
            
            if bot_val:
                if day_idx not in groups_data[g_name][1]: groups_data[g_name][1][day_idx] = {}
                groups_data[g_name][1][day_idx][pair_num] = bot_val

def parse_pdf_task(link):
    """Задача для одного потока: скачать и распарсить один PDF"""
    groups_data = { "Б-ПИ-101": {0: {}, 1: {}}, "Б-ПИ-102": {0: {}, 1: {}} }

    try:
        response = requests.get(link, headers=HEADERS, timeout=30)
        

        with pdfplumber.open(io.BytesIO(response.content)) as pdf:
            
            
            for page_num, page in enumerate(pdf.pages, 1):
                tables = page.extract_tables({"vertical_strategy": "lines", "horizontal_strategy": "lines"})
                if not tables: 
                    continue

                for table in tables:
                    days_map = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота"]
                    current_day_idx = -1
                    
                    block_rows = []
                    current_pair_num = -1

                    for row in table:
                        if len(row) < 2: continue
                        
                        
                        raw_day = str(row[0]).replace('\n', '').replace(' ', '').lower() if row[0] else ""
                        for idx, d_name in enumerate(days_map):
                            if d_name in raw_day or d_name[::-1] in raw_day:
                                process_time_block(block_rows, current_pair_num, current_day_idx, groups_data)
                                block_rows = []
                                current_pair_num = -1
                                current_day_idx = idx
                                break
                        if current_day_idx == -1: continue

                        
                        raw_time = str(row[1]).replace('\n', ' ').strip() if row[1] else ""
                        new_pair_num = -1
                        for t_str, p_num in TIME_START_TO_PAIR_NUM.items():
                            if t_str in raw_time: new_pair_num = p_num; break
                        
                        if new_pair_num != -1:
                            process_time_block(block_rows, current_pair_num, current_day_idx, groups_data)
                            current_pair_num = new_pair_num
                            block_rows = [row]
                        else:
                            if current_pair_num != -1:
                                block_rows.append(row)

                    process_time_block(block_rows, current_pair_num, current_day_idx, groups_data)

        return groups_data
    except Exception as e:
        logger.error(f"Ошибка парсинга {link}: {e}")
        return None

def normalize_text(text):
    if not text: return ""
    return "".join(text.lower().split())

def is_schedule_changed(old_data, new_data):
    if not old_data or not new_data: return True
    for g_name in ["Б-ПИ-101", "Б-ПИ-102"]:
        old_g = old_data.get(g_name, {})
        new_g = new_data.get(g_name, {})
        for week in [0, 1]:
            old_w = old_g.get(week, {})
            new_w = new_g.get(week, {})
            all_days = set(old_w.keys()) | set(new_w.keys())
            for day in all_days:
                old_d = old_w.get(day, {})
                new_d = new_w.get(day, {})
                all_p = set(old_d.keys()) | set(new_d.keys())
                for pair in all_p:
                    if normalize_text(old_d.get(pair, "")) != normalize_text(new_d.get(pair, "")):
                        return True
    return False
