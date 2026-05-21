import math
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont

def get_fonts():
    font_candidates = [
        ("arial.ttf", "arialbd.ttf"),  # Windows / macOS
        ("LiberationSans-Regular.ttf", "LiberationSans-Bold.ttf"),  # Linux (Ubuntu/Debian)
        ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"),  # Linux (DejaVu)
        ("FreeSans.ttf", "FreeSansBold.ttf"),  # Linux (FreeSans)
        ("Helvetica.ttc", "Helvetica.ttc"),  # macOS
    ]
    
    font_main = None
    font_bold = None
    font_title = None
    font_day = None
    
    for reg, bld in font_candidates:
        try:
            font_main = ImageFont.truetype(reg, 16)
            font_bold = ImageFont.truetype(bld, 16)
            font_title = ImageFont.truetype(bld, 26)
            font_day = ImageFont.truetype(bld, 20)
            break
        except IOError:
            continue
            
    if font_main is None:
        print("Warning: Suitable TrueType fonts not found, using default.")
        font_main = ImageFont.load_default()
        font_bold = ImageFont.load_default()
        font_title = ImageFont.load_default()
        font_day = ImageFont.load_default()
        
    return font_main, font_bold, font_title, font_day


colors = {
    "bg_color": (250, 250, 250),
    "text_color": (30, 30, 30),
    "header_bg": (41, 128, 185), 
    "header_text": (255, 255, 255),
    "day_bg": (200, 220, 240), 
    "row_bg_1": (255, 255, 255),
    "row_bg_2": (245, 248, 252),
    "line_color": (180, 180, 180)
}

week_map = {
    "all": "Каждую",
    "numerator": "Верхняя",
    "denominator": "Нижняя"
}

def get_text_width(text, font):
    if hasattr(font, 'getlength'):
        return font.getlength(text)
    return font.getbbox(text)[2] - font.getbbox(text)[0]

def wrap_text_to_pixels(text, font, max_width):
    words = text.split()
    lines = []
    current_line = []
    
    for word in words:
        test_line = ' '.join(current_line + [word]) if current_line else word
        if get_text_width(test_line, font) <= max_width:
            current_line.append(word)
        else:
            if current_line:
                lines.append(' '.join(current_line))
                current_line = [word]
            else:
                lines.append(word)
                current_line = []
    if current_line:
        lines.append(' '.join(current_line))
    return lines

def parse_day_data(day, col_widths, font_main, show_week=True):
    day_name = day.get("name", "").capitalize()
    if "date" in day:
        try:
            d_obj = datetime.strptime(day["date"], "%Y-%m-%d")
            day_name += f" ({d_obj.strftime('%d.%m.%Y')})"
        except:
            day_name += f" ({day['date']})"

    lessons = day["lessons"]
    
    # Группировка занятий с одинаковым временем в одну строку
    from collections import OrderedDict
    time_groups = OrderedDict()
    for l in lessons:
        key = (l['time_from'], l['time_to'])
        if key not in time_groups:
            time_groups[key] = []
        time_groups[key].append(l)
    
    parsed_rows = []
    block_h = 40 + 40  # day_header + table_header
    
    for (t_from, t_to), group_lessons in time_groups.items():
        time_str = f"{t_from}\n{t_to}"
        week_str = week_map.get(group_lessons[0]['week'], group_lessons[0]['week'])
        
        max_w = col_widths["subject"] - 20
        
        all_subj_lines = []
        for i, l in enumerate(group_lessons):
            wrapped = wrap_text_to_pixels(l['subject'], font_main, max_w)
            if i > 0:
                all_subj_lines.append("")
            all_subj_lines.extend(wrapped)
        
        subj_lines = len(all_subj_lines)
        row_h = max(50, subj_lines * 22 + 20)
        block_h += row_h
        
        parsed_rows.append({
            "time": time_str,
            "week": week_str if show_week else "",
            "subject": "\n".join(all_subj_lines),
            "height": row_h
        })
        
    return {
        "name": day_name,
        "rows": parsed_rows,
        "block_h": block_h
    }

def draw_day_block(draw, x_start, y_start, day_data, col_widths, fonts, target_height=None, show_week=True):
    font_main, font_bold, font_title, font_day = fonts
    block_w = sum(col_widths.values())
    total_h = target_height if target_height else day_data["block_h"]
    
    draw.rectangle([x_start, y_start, x_start + block_w, y_start + total_h], fill=colors["row_bg_1"], outline=colors["line_color"])
    
    y_offset = y_start
    
    draw.rectangle([x_start, y_offset, x_start + block_w, y_offset + 40], fill=colors["day_bg"], outline=colors["line_color"])
    draw.text((x_start + 15, y_offset + 8), day_data["name"], font=font_day, fill=colors["text_color"])
    y_offset += 40
    
    draw.rectangle([x_start, y_offset, x_start + block_w, y_offset + 40], fill=colors["header_bg"], outline=colors["line_color"])
    draw.text((x_start + 10, y_offset + 10), "Время", font=font_bold, fill=colors["header_text"])
    
    subj_x_start = x_start + col_widths["time"] + 10
    if show_week:
        draw.text((x_start + col_widths["time"] + 10, y_offset + 10), "Неделя", font=font_bold, fill=colors["header_text"])
        subj_x_start += col_widths["week"]
        
    draw.text((subj_x_start, y_offset + 10), "Предмет", font=font_bold, fill=colors["header_text"])
    y_offset += 40

    for i, row in enumerate(day_data["rows"]):
        bg = colors["row_bg_1"] if i % 2 == 0 else colors["row_bg_2"]
        h = row['height']
        
        if i == len(day_data["rows"]) - 1 and target_height:
            h = total_h - (y_offset - y_start)
            
        draw.rectangle([x_start, y_offset, x_start + block_w, y_offset + h], fill=bg, outline=colors["line_color"])
        
        draw.text((x_start + 10, y_offset + 10), row['time'], font=font_main, fill=colors["text_color"])
        
        subj_x_start = x_start + col_widths["time"] + 10
        if show_week:
            draw.text((x_start + col_widths["time"] + 10, y_offset + (h - 20)//2 - 5), row['week'], font=font_main, fill=colors["text_color"])
            subj_x_start += col_widths["week"]
            
        draw.text((subj_x_start, y_offset + 10), row['subject'], font=font_main, fill=colors["text_color"])
        
        y_offset += h
        
    x1 = x_start + col_widths["time"]
    draw.line([(x1, y_start + 40), (x1, y_start + total_h)], fill=colors["line_color"])
    if show_week:
        x2 = x_start + col_widths["time"] + col_widths["week"]
        draw.line([(x2, y_start + 40), (x2, y_start + total_h)], fill=colors["line_color"])

def create_schedule_image_horizontal(group_name, days_list, output_filename="schedule_horiz.png", filter_parity=None, is_zaochnaya=False):
    fonts = get_fonts()
    font_main = fonts[0]
    
    filtered_days = []
    if is_zaochnaya:
        filtered_days = [d for d in days_list if d.get("lessons")]
        show_week = False
        col_widths = {"time": 70, "subject": 500}
        filter_parity = None
    elif filter_parity:
        for d in days_list:
            d_copy = dict(d)
            d_copy["lessons"] = [l for l in d["lessons"] if l["week"] in ["all", filter_parity]]
            if d_copy["lessons"]:
                filtered_days.append(d_copy)
        show_week = False
        col_widths = {"time": 70, "subject": 500}
    else:
        filtered_days = [d for d in days_list if d.get("lessons")]
        show_week = True
        col_widths = {"time": 70, "week": 130, "subject": 370}
        
    block_w = sum(col_widths.values())
    padding = 20
    gap = 20
    
    parsed_days = [parse_day_data(d, col_widths, font_main, show_week=show_week) for d in filtered_days]
    if not parsed_days:
        print("Нет пар для отображения.")
        return
        
    cols_count = min(3, len(parsed_days))
    rows_count = math.ceil(len(parsed_days) / cols_count)
    
    row_heights = []
    for r in range(rows_count):
        row_days = parsed_days[r*cols_count : (r+1)*cols_count]
        max_h = max(d["block_h"] for d in row_days)
        row_heights.append(max_h)
        
    img_width = padding * 2 + cols_count * block_w + (cols_count - 1) * gap
    
    title_text = f"Расписание: {group_name} | Вся неделя"
    if filter_parity:
        week_type = "Нижняя" if filter_parity == "denominator" else "Верхняя"
        title_text += f" ({week_type})"
        
    max_title_w = img_width - 2 * padding
    title_lines = wrap_text_to_pixels(title_text, fonts[2], max_title_w)
    top_margin = max(60, 15 + len(title_lines) * 34)
    img_height = top_margin + sum(row_heights) + (rows_count - 1) * gap + padding
    img = Image.new("RGB", (img_width, img_height), colors["bg_color"])
    draw = ImageDraw.Draw(img)
    for li, tl in enumerate(title_lines):
        draw.text((padding, 10 + li * 32), tl, font=fonts[2], fill=colors["text_color"])
    
    for i, day in enumerate(parsed_days):
        r = i // cols_count
        c = i % cols_count
        x_start = padding + c * (block_w + gap)
        y_start = top_margin + sum(row_heights[:r]) + r * gap
        draw_day_block(draw, x_start, y_start, day, col_widths, fonts, target_height=row_heights[r], show_week=show_week)
        
    img.save(output_filename)
    print(f"Saved to {output_filename}")

def create_schedule_image_vertical(group_name, day, output_filename="schedule_vert.png", filter_parity=None, is_zaochnaya=False):
    fonts = get_fonts()
    font_main = fonts[0]
    
    if is_zaochnaya:
        show_week = False
        col_widths = {"time": 80, "subject": 635}
    elif filter_parity:
        day = dict(day)
        day["lessons"] = [l for l in day["lessons"] if l["week"] in ["all", filter_parity]]
        show_week = False
        col_widths = {"time": 80, "subject": 635}
    else:
        show_week = True
        col_widths = {"time": 80, "week": 135, "subject": 500}
        
    block_w = sum(col_widths.values())
    padding = 20
    
    parsed_day = parse_day_data(day, col_widths, font_main, show_week=show_week)
    
    img_width = padding * 2 + block_w
    tmp_title = f"Расписание: {group_name} | {parsed_day['name']}"
    tmp_lines = wrap_text_to_pixels(tmp_title, fonts[2], img_width - 2 * padding)
    top_margin = max(60, 15 + len(tmp_lines) * 34)
    img_height = top_margin + parsed_day["block_h"] + padding
    
    img = Image.new("RGB", (img_width, img_height), colors["bg_color"])
    draw = ImageDraw.Draw(img)
    
    title_text = f"Расписание: {group_name} | {parsed_day['name']}"
    max_title_w = img_width - 2 * padding
    title_lines = wrap_text_to_pixels(title_text, fonts[2], max_title_w)
    for li, tl in enumerate(title_lines):
        draw.text((padding, 10 + li * 32), tl, font=fonts[2], fill=colors["text_color"])
    
    draw_day_block(draw, padding, top_margin, parsed_day, col_widths, fonts, show_week=show_week)
    
    img.save(output_filename)
    print(f"Saved to {output_filename}")
