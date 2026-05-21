import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, unquote, quote, urlparse, urlunparse
import concurrent.futures
import multiprocessing
import json
import sys
import re

from pdf_parser import parse_pdf_to_json

BASE_URL = "https://www.vavilovsar.ru"
START_URL = "https://www.vavilovsar.ru/ucheba/raspisanie-zanyatii"

# Отключение предупреждений SSL (у сайта университета проблемы с сертификатом)
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Загрузка настроек производительности из .env
import os
from dotenv import load_dotenv
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
dotenv_path = os.path.join(BASE_DIR, 'tgbot', '.env')
load_dotenv(dotenv_path)

MAX_WORKERS = int(os.getenv("MAX_WORKERS", "4"))

def encode_url(url):
    """Корректно кодирует путь URL (пробелы, кириллицу), сохраняя схему и хост."""
    parsed = urlparse(url)
    encoded_path = quote(unquote(parsed.path), safe='/:@!$&\'()*+,;=')
    return urlunparse(parsed._replace(path=encoded_path))

def get_soup(url):
    try:
        response = requests.get(url, timeout=10, verify=False)
        response.raise_for_status()
        return BeautifulSoup(response.content, 'html.parser')
    except Exception as e:
        print(f"Error fetching {url}: {e}", file=sys.stderr)
        return None

def find_institutes():
    soup = get_soup(START_URL)
    if not soup:
        return []
    
    institutes = []
    # Find links that contain '/uk' and '/institut-'
    for a in soup.find_all('a', href=True):
        href = a['href']
        if '/uk' in href and '/institut-' in href:
            if 'ekzamenacionnaya' not in href and 'nedeli-' not in href:
                full_url = urljoin(BASE_URL, href)
                parts = href.strip('/').split('/')
                uk_name = "unknown_uk"
                inst_name = "unknown_inst"
                for p in parts:
                    if p.startswith('uk'):
                        uk_name = p
                    elif p.startswith('institut-'):
                        inst_name = p
                
                if not any(i['url'] == full_url for i in institutes):
                    institutes.append({
                        'uk': uk_name,
                        'institute': inst_name,
                        'url': full_url
                    })
    return institutes

def find_all_form_pdfs(institute):
    """Находит PDF ссылки для всех форм обучения (очная, заочная, очно-заочная)."""
    soup = get_soup(institute['url'])
    if not soup:
        return []
    
    # Находим все ссылки на формы обучения на странице института
    form_urls = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        if 'forma-obucheniya' in href:
            full_url = urljoin(BASE_URL, href)
            # Определяем тип формы из URL
            if 'ochno-zaochnaya' in href:
                form_type = 'ochno-zaochnaya-forma-obucheniya'
            elif 'zaochnaya' in href:
                form_type = 'zaochnaya-forma-obucheniya'
            elif 'ochnaya' in href:
                form_type = 'ochnaya-forma-obucheniya'
            else:
                continue
            
            if not any(f['url'] == full_url for f in form_urls):
                form_urls.append({'url': full_url, 'form': form_type})
    
    if not form_urls:
        # Fallback: ищем только очную форму по старой логике
        for a in soup.find_all('a', href=True):
            if 'очная' in a.text.lower() or 'ochnaya-forma-obucheniya' in a['href']:
                form_urls.append({
                    'url': urljoin(BASE_URL, a['href']),
                    'form': 'ochnaya-forma-obucheniya'
                })
                break
    
    all_pdfs = []
    
    # Regex для файлов отсканированных страниц
    scan_pattern = re.compile(r'_\d+\s*-\s*\d{4}\.pdf$', re.IGNORECASE)
    # Regex для файлов с датой в формате "с ДД.ММ.ГГГГ" (4-значный год) — исключаем из очной формы
    date_full_year_pattern = re.compile(r'с\s+\d{2}\.\d{2}\.\d{4}', re.IGNORECASE)
    
    for form_info in form_urls:
        form_soup = get_soup(form_info['url'])
        if not form_soup:
            continue
        
        form_type = form_info['form']
        seen_urls = set()
        
        for a in form_soup.find_all('a', href=True):
            href = a['href']
            if not href.endswith('.pdf'):
                continue
            
            # Пропускаем нерелевантные PDF
            if 'politika' in href or 'reglament' in href:
                continue
            
            decoded_href = unquote(href)
            link_text = a.get_text(strip=True)
            
            # Пропускаем отсканированные страницы
            if scan_pattern.search(decoded_href):
                continue
            
            # Для очной формы: пропускаем файлы с датой "с ДД.ММ.ГГГГ" (4-значный год)
            if form_type == 'ochnaya-forma-obucheniya':
                if date_full_year_pattern.search(decoded_href) or date_full_year_pattern.search(link_text):
                    print(f"  [SKIP] Очная форма, файл с датой: {link_text}")
                    continue
            
            full_pdf_url = encode_url(urljoin(BASE_URL, href))
            if full_pdf_url not in seen_urls:
                seen_urls.add(full_pdf_url)
                all_pdfs.append({
                    'uk': institute['uk'],
                    'institute': institute['institute'],
                    'form': form_type,
                    'pdf_url': full_pdf_url
                })
                
        # Если не найдено ни одного PDF файла, добавляем пустую форму
        if not seen_urls:
            all_pdfs.append({
                'uk': institute['uk'],
                'institute': institute['institute'],
                'form': form_type,
                'pdf_url': None
            })
    
    return all_pdfs

def scrape_all_pdf_links():
    institutes = find_institutes()
    print(f"Found {len(institutes)} institutes.")
    all_pdfs = []
    
    # Использование ThreadPool для быстрого параллельного сбора ссылок
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        results = executor.map(find_all_form_pdfs, institutes)
        for pdf_list in results:
            all_pdfs.extend(pdf_list)
            
    return all_pdfs

def parse_pdf_worker(pdf_info):
    url = pdf_info['pdf_url']
    form = pdf_info['form']
    
    # Определяем form_type для парсера
    if 'ochno-zaochnaya' in form:
        form_type = 'ochno-zaochnaya'
    elif 'zaochnaya' in form:
        form_type = 'zaochnaya'
    else:
        form_type = 'ochnaya'
    
    print(f"Parsing [{form_type}] {url}...")
    try:
        groups_data = parse_pdf_to_json(url, form_type=form_type)
    except Exception as e:
        print(f"Error parsing {url}: {e}", file=sys.stderr)
        groups_data = []
        
    return {
        'uk': pdf_info['uk'],
        'institute': pdf_info['institute'],
        'form': pdf_info['form'],
        'groups': groups_data
    }

def main():
    import os
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = os.path.join(BASE_DIR, "data")
    os.makedirs(DATA_DIR, exist_ok=True)
    pdf_cache_path = os.path.join(DATA_DIR, "pdf_cache.json")
    schedule_path = os.path.join(DATA_DIR, "schedule.json")

    print("Scraping PDF links...")
    pdfs = scrape_all_pdf_links()
    
    # Загрузка кэша
    try:
        with open(pdf_cache_path, "r", encoding="utf-8") as f:
            pdf_cache = json.load(f)
    except:
        pdf_cache = {}
        
    try:
        with open(schedule_path, "r", encoding="utf-8") as f:
            old_schedule = json.load(f)
    except:
        old_schedule = []
        
    def get_old_groups(uk, inst, form):
        for u in old_schedule:
            if u.get("name") == uk:
                for i in u.get("institutes", []):
                    if i.get("name") == inst:
                        for f in i.get("forms", []):
                            if f.get("name") == form:
                                return f.get("groups", [])
        return []
        
    form_pdfs = {}
    for pdf in pdfs:
        key = f"{pdf['uk']}_{pdf['institute']}_{pdf['form']}"
        if key not in form_pdfs:
            form_pdfs[key] = []
        form_pdfs[key].append(pdf)

    pdfs_to_parse = []
    skipped_results = []
    
    for key, pdf_list in form_pdfs.items():
        valid_pdfs = [p for p in pdf_list if p['pdf_url'] is not None]
        current_urls = sorted([p['pdf_url'] for p in valid_pdfs])
        
        uk = pdf_list[0]['uk']
        inst = pdf_list[0]['institute']
        form = pdf_list[0]['form']
        
        if not valid_pdfs:
            # Form is empty (no PDFs)
            skipped_results.append({
                'uk': uk,
                'institute': inst,
                'form': form,
                'groups': []
            })
            continue
            
        if key in pdf_cache and isinstance(pdf_cache[key], list) and pdf_cache[key] == current_urls:
            old_groups = get_old_groups(uk, inst, form)
            if old_groups:
                skipped_results.append({
                    'uk': uk,
                    'institute': inst,
                    'form': form,
                    'groups': old_groups
                })
            else:
                pdfs_to_parse.extend(valid_pdfs)
        else:
            pdfs_to_parse.extend(valid_pdfs)
            
    print(f"Skipped {len(skipped_results)} form categories. Need to parse {len(pdfs_to_parse)} PDFs.")
    
    parsed_results = []
    
    if pdfs_to_parse:
        # Используем настройку MAX_WORKERS для ограничения воркеров под разные серверы
        workers = min(MAX_WORKERS, multiprocessing.cpu_count(), len(pdfs_to_parse))
        with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as executor:
            for result in executor.map(parse_pdf_worker, pdfs_to_parse):
                parsed_results.append(result)
                    
        for key, pdf_list in form_pdfs.items():
            if any(p in pdfs_to_parse for p in pdf_list):
                pdf_cache[key] = sorted([p['pdf_url'] for p in pdf_list])
            
        with open(pdf_cache_path, "w", encoding="utf-8") as f:
            json.dump(pdf_cache, f, ensure_ascii=False, indent=2)
                
    all_results = skipped_results + parsed_results
            
    print("Aggregating results...")
    hierarchy = {}
    for res in all_results:
        uk = res['uk']
        inst = res['institute']
        form = res['form']
        groups = res['groups']
        
        if uk not in hierarchy:
            hierarchy[uk] = {}
        if inst not in hierarchy[uk]:
            hierarchy[uk][inst] = {}
        if form not in hierarchy[uk][inst]:
            hierarchy[uk][inst][form] = {}
            
        for g in groups:
            g_name = g['name']
            if g_name not in hierarchy[uk][inst][form]:
                hierarchy[uk][inst][form][g_name] = g
            else:
                # Merge schedules for zaochnaya (multiple PDFs = multiple weeks)
                if 'zaochnaya' in form and 'ochno' not in form:
                    existing_days = hierarchy[uk][inst][form][g_name]['schedule'].get('days', [])
                    new_days = g['schedule'].get('days', [])
                    
                    # Merge and remove duplicates (by date)
                    seen_dates = {d.get('date') for d in existing_days if d.get('date')}
                    for d in new_days:
                        if d.get('date') not in seen_dates:
                            existing_days.append(d)
                            seen_dates.add(d.get('date'))
                            
                    # Sort days by date
                    try:
                        existing_days.sort(key=lambda x: x.get('date', ''))
                    except Exception:
                        pass
                else:
                    # For ochnaya, if we have duplicate groups across PDFs, we overwrite
                    # (it usually means an updated file or a file parsed later)
                    hierarchy[uk][inst][form][g_name] = g
        
    final_json = []
    for uk, insts in hierarchy.items():
        uk_obj = {
            "name": uk,
            "institutes": []
        }
        for inst, forms in insts.items():
            inst_obj = {
                "name": inst,
                "forms": []
            }
            for form, groups_dict in forms.items():
                form_obj = {
                    "name": form,
                    "groups": list(groups_dict.values())
                }
                inst_obj["forms"].append(form_obj)
            uk_obj["institutes"].append(inst_obj)
        final_json.append(uk_obj)

    final_json.sort(key=lambda x: x['name'])
    for uk_obj in final_json:
        uk_obj['institutes'].sort(key=lambda x: x['name'])
        for inst_obj in uk_obj['institutes']:
            def form_sort_key(f):
                n = f['name']
                if 'ochno-zaochnaya' in n: return 2
                if 'zaochnaya' in n: return 1
                return 0
            inst_obj['forms'].sort(key=form_sort_key)
            for form_obj in inst_obj['forms']:
                form_obj['groups'].sort(key=lambda g: g.get('name', ''))
        
    with open(schedule_path, "w", encoding="utf-8") as f:
        json.dump(final_json, f, ensure_ascii=False, indent=2)
        
    print(f"Готово! Результат сохранен в файл: {schedule_path}")

if __name__ == "__main__":
    main()
