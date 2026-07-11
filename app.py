import os
import re
import io
import base64
import zipfile
from flask import Flask, request, render_template, redirect, url_for, flash, Response
import fitz  # PyMuPDF
from PIL import Image

app = Flask(__name__)
app.secret_key = 'pdf_converter_secret_key'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # Limit uploads to 50MB

def rects_overlap(r1, r2):
    return not (r1.x1 <= r2.x0 or r2.x1 <= r1.x0 or r1.y1 <= r2.y0 or r2.y1 <= r1.y0)

# 1. Page Indexing: Scans PDF pages to find topic relevance
def index_document_sections(doc):
    section_pages = {
        "home": 1,
        "about": 1,
        "services": 1,
        "pricing": 1,
        "portfolio": 1,
        "contact": len(doc),
    }
    scores = {key: [0] * len(doc) for key in ["about", "services", "pricing", "portfolio", "contact"]}
    
    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text().lower()
        
        for key in scores.keys():
            count = text.count(key)
            if key == "about" and "about us" in text: count += 5
            if key == "contact" and "contact us" in text: count += 5
            if key == "services" and "our services" in text: count += 5
            if key == "pricing" and "pricing plans" in text: count += 5
            scores[key][page_num] = count
            
    for key, page_scores in scores.items():
        max_score = max(page_scores)
        if max_score > 0:
            section_pages[key] = page_scores.index(max_score) + 1
            
    return section_pages

# 2. Page Filename Resolver (For Multi-page zip export)
def get_page_filename(page_num, section_pages):
    if page_num == 1:
        return "index.html"
    for section, num in section_pages.items():
        if num == page_num and section != "home":
            return f"{section}.html"
    return f"page-{page_num}.html"

# 3. Dynamic Form Field & Submit Overlays via design placeholders
def generate_form_fields_layer(page, page_width, page_height):
    fields_html = []
    try:
        dict_data = page.get_text("dict")
        for block in dict_data.get("blocks", []):
            if "lines" in block:
                for line in block["lines"]:
                    for span in line["spans"]:
                        text = span["text"].strip()
                        
                        # Match: [input:text:Placeholder Here]
                        if text.startswith("[input:") and text.endswith("]"):
                            bbox = span["bbox"]
                            parts = text[7:-1].split(":")
                            if len(parts) >= 2:
                                field_type = parts[0]  # text, email, tel, etc.
                                placeholder = parts[1]
                                field_name = placeholder.lower().replace(" ", "_")
                                
                                left = (bbox[0] / page_width) * 100
                                top = (bbox[1] / page_height) * 100
                                width = ((bbox[2] - bbox[0]) / page_width) * 100
                                height = ((bbox[3] - bbox[1]) / page_height) * 100
                                
                                if field_type == "textarea":
                                    fields_html.append(f"""
                                        <textarea name="{field_name}" placeholder="{placeholder}" required class="form-input textarea-field" style="
                                            position: absolute; left: {left}%; top: {top}%; width: {width}%; height: {height}%; z-index: 15;
                                        "></textarea>
                                    """)
                                else:
                                    fields_html.append(f"""
                                        <input type="{field_type}" name="{field_name}" placeholder="{placeholder}" required class="form-input input-field" style="
                                            position: absolute; left: {left}%; top: {top}%; width: {width}%; height: {height}%; z-index: 15;
                                        "/>
                                    """)
                        
                        # Match: [submit:Button Text]
                        elif text.startswith("[submit:") and text.endswith("]"):
                            bbox = span["bbox"]
                            btn_text = text[8:-1]
                            
                            left = (bbox[0] / page_width) * 100
                            top = (bbox[1] / page_height) * 100
                            width = ((bbox[2] - bbox[0]) / page_width) * 100
                            height = ((bbox[3] - bbox[1]) / page_height) * 100
                            
                            fields_html.append(f"""
                                <button type="submit" class="form-submit-btn" style="
                                    position: absolute; left: {left}%; top: {top}%; width: {width}%; height: {height}%; z-index: 15;
                                ">{btn_text}</button>
                            """)
    except Exception:
        pass
    return "\n".join(fields_html)

# 4. Extract Selectable & Searchable Transparent Text Layer
def generate_selectable_text_layer(page, page_width, page_height):
    spans_html = []
    try:
        dict_data = page.get_text("dict")
        for block in dict_data.get("blocks", []):
            if "lines" in block:
                for line in block["lines"]:
                    for span in line["spans"]:
                        text = span["text"]
                        # Skip form placeholder markup
                        if text.strip().startswith("[input:") or text.strip().startswith("[submit:"):
                            continue
                            
                        bbox = span["bbox"]
                        left = (bbox[0] / page_width) * 100
                        top = (bbox[1] / page_height) * 100
                        width = ((bbox[2] - bbox[0]) / page_width) * 100
                        height = ((bbox[3] - bbox[1]) / page_height) * 100
                        font_size_pct = (span["size"] / page_width) * 100
                        
                        spans_html.append(f"""
                            <span class="selectable-text" style="
                                position: absolute; left: {left}%; top: {top}%; width: {width}%; height: {height}%;
                                font-size: {font_size_pct}vw; line-height: 1; color: transparent; white-space: nowrap;
                                transform-origin: left top; pointer-events: auto; user-select: text; -webkit-user-select: text;
                            ">{text}</span>
                        """)
    except Exception:
        pass
    return "\n".join(spans_html)

# 5. Extract Utility links (Raw URLs, Email links, Phones)
def extract_utilities(page):
    detected = []
    text_content = page.get_text()
    url_pattern = re.compile(r'^(https?://)?(www\.)?([a-zA-Z0-9\-]+\.)+[a-zA-Z]{2,6}(/[a-zA-Z0-9\-._~:/?#\[\]@!$&\'()*+,;=]*)?$', re.IGNORECASE)
    email_pattern = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,6}$', re.IGNORECASE)
    phone_pattern = re.compile(r'\b(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b')
    
    words = page.get_text("words")
    for w in words:
        x0, y0, x1, y1, text, _, _, _ = w
        clean_text = text.strip().strip(",.()[]{}:;\"'!?*")
        if len(clean_text) < 4: continue
        
        if url_pattern.match(clean_text) or clean_text.startswith("www.") or clean_text.startswith("http"):
            href = clean_text if clean_text.startswith("http") else "https://" + clean_text
            detected.append({"rect": fitz.Rect(x0, y0, x1, y1), "href": href, "target": 'target="_blank"'})
        elif email_pattern.match(clean_text) or ("@" in clean_text and "." in clean_text):
            detected.append({"rect": fitz.Rect(x0, y0, x1, y1), "href": f"mailto:{clean_text}", "target": ""})
            
    phones = phone_pattern.findall(text_content)
    for num in set(phones):
        rects = page.search_for(num)
        for rect in rects:
            detected.append({"rect": rect, "href": f"tel:{re.sub(r'[^\d+]', '', num)}", "target": ""})
            
    return detected

# 6. Intent-to-Section Button Mapper
def detect_smart_button_intents(page, section_pages, is_multipage):
    detected = []
    current_page = page.number + 1
    intent_map = {
        "home": "home", "welcome": "home", "back to top": "home",
        "about us": "about", "about": "about", "who we are": "about", "our story": "about", "learn more": "about", "read more": "about",
        "services": "services", "what we do": "services", "our services": "services", "features": "services", "shop now": "services", "shop": "services",
        "pricing": "pricing", "plans": "pricing", "pricing plans": "pricing", "buy now": "pricing",
        "portfolio": "portfolio", "our work": "portfolio", "projects": "portfolio",
        "contact us": "contact", "contact": "contact", "get in touch": "contact", "email us": "contact", "get started": "contact", "register": "contact", "join now": "contact", "join": "contact", "apply now": "contact", "apply": "contact", "book now": "contact", "book a call": "contact", "subscribe": "contact", "download": "contact", "download now": "contact", "get templates": "contact"
    }
    
    for phrase, target_section in intent_map.items():
        target_page = section_pages.get(target_section)
        if not target_page: continue
        if current_page == target_page and current_page != 1: continue
        
        matches = page.search_for(phrase)
        for rect in matches:
            href = f"#{target_section}" if not is_multipage else get_page_filename(target_page, section_pages)
            if not is_multipage and target_section == "home":
                href = "#page-1"
            elif not is_multipage:
                href = f"#page-{target_page}"
                
            detected.append({"rect": rect, "href": href, "target": ""})
            
    socials = {"facebook": "https://facebook.com", "instagram": "https://instagram.com", "twitter": "https://twitter.com", "linkedin": "https://linkedin.com", "github": "https://github.com", "youtube": "https://youtube.com"}
    for platform, url in socials.items():
        matches = page.search_for(platform)
        for rect in matches:
            detected.append({"rect": rect, "href": url, "target": 'target="_blank"'})
            
    return detected

# 7. Core Single/Multi-page compiler
def compile_page_elements(doc, page_num, section_pages, zoom_factor, is_multipage):
    page = doc[page_num - 1]
    page_width = page.rect.width
    page_height = page.rect.height
    aspect_ratio = (page_height / page_width) * 100
    
    # Process layouts
    existing_links = page.get_links()
    active_links = []
    existing_rects = []
    
    # 1. Standard Manual links
    for link in existing_links:
        href, target = "", ""
        if "uri" in link:
            href, target = link["uri"], 'target="_blank"'
        elif "page" in link:
            target_page_num = link["page"] + 1
            href = f"#{get_page_filename(target_page_num, section_pages)}" if is_multipage else f"#page-{target_page_num}"
            if is_multipage:
                href = get_page_filename(target_page_num, section_pages)
        if href:
            existing_rects.append(link["from"])
            active_links.append({"rect": link["from"], "href": href, "target": target})
            
    # 2. Text Utilities
    for item in extract_utilities(page):
        if not any(rects_overlap(item["rect"], r) for r in existing_rects):
            active_links.append(item)
            existing_rects.append(item["rect"])
            
    # 3. Intent Buttons
    for btn in detect_smart_button_intents(page, section_pages, is_multipage):
        if not any(rects_overlap(btn["rect"], r) for r in existing_rects):
            active_links.append(btn)
            existing_rects.append(btn["rect"])
            
    # Assemble Layout Layers
    link_overlays = []
    for l in active_links:
        rect = l["rect"]
        left_pct = (rect.x0 / page_width) * 100
        top_pct = (rect.y0 / page_height) * 100
        width_pct = ((rect.x1 - rect.x0) / page_width) * 100
        height_pct = ((rect.y1 - rect.y0) / page_height) * 100
        link_overlays.append(f'<a href="{l["href"]}" {l["target"]} class="pdf-link" style="position: absolute; left: {left_pct}%; top: {top_pct}%; width: {width_pct}%; height: {height_pct}%; z-index: 10;"></a>')
        
    # Render WebP High-density graphic
    mat = fitz.Matrix(zoom_factor, zoom_factor)
    pix = page.get_pixmap(matrix=mat)
    png_bytes = pix.tobytes("png")
    
    # PIL Conversion to compressed WebP
    img = Image.open(io.BytesIO(png_bytes))
    webp_io = io.BytesIO()
    img.save(webp_io, format="WEBP", quality=85)
    img_base64 = base64.b64encode(webp_io.getvalue()).decode("utf-8")
    
    # Selectable Text Overlay
    selectable_layer = generate_selectable_text_layer(page, page_width, page_height)
    # Form input fields overlays
    forms_layer = generate_form_fields_layer(page, page_width, page_height)
    
    lazy_attr = 'loading="lazy"' if page_num > 1 else ''
    
    return f"""
    <div id="page-{page_num}" class="page-container" style="position: relative; width: 100%; max-width: {page_width}px; margin: 0 auto; background: #ffffff;">
        <div style="padding-top: {aspect_ratio}%;"></div>
        <img src="data:image/webp;base64,{img_base64}" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; display: block;" alt="Page {page_num}" {lazy_attr} />
        <div class="interactive-overlay" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%;">
            {"".join(link_overlays)}
            {forms_layer}
            {selectable_layer}
        </div>
    </div>
    """

def get_base_styles():
    return """
        html { scroll-behavior: smooth; }
        body { margin: 0; padding: 0; background-color: #ffffff; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; overflow-x: hidden; }
        .page-container { border-radius: 0; box-shadow: none; }
        .pdf-link { cursor: pointer; text-decoration: none; }
        .pdf-link:hover { background-color: rgba(59, 130, 246, 0.08); outline: 1.5px dashed rgba(59, 130, 246, 0.5); }
        .selectable-text::selection { background-color: rgba(59, 130, 246, 0.25); color: transparent; }
        .selectable-text::-webkit-selection { background-color: rgba(59, 130, 246, 0.25); color: transparent; }
        
        /* Modern form components */
        .form-input {
            background: rgba(255, 255, 255, 0.85); border: 1px solid #cbd5e1; border-radius: 4px;
            padding: 4px 12px; font-family: inherit; font-size: 14px; outline: none; transition: all 0.2s;
        }
        .form-input:focus {
            border-color: #3b82f6; box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.15); background: #ffffff;
        }
        .textarea-field { resize: none; }
        .form-submit-btn {
            background: #2563eb; color: white; border: none; border-radius: 4px;
            font-weight: 600; font-size: 14px; transition: background-color 0.2s; cursor: pointer;
        }
        .form-submit-btn:hover { background-color: #1d4ed8; }
    """

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        if 'pdf_file' not in request.files:
            flash('No file part')
            return redirect(request.url)
        file = request.files['pdf_file']
        if file.filename == '':
            flash('No selected file')
            return redirect(request.url)
        
        if file and file.filename.lower().endswith('.pdf'):
            try:
                zoom = float(request.form.get('zoom', 2.0))
                layout_mode = request.form.get('layout_mode', 'single')
                meta_title = request.form.get('meta_title', 'My Smart Website')
                meta_desc = request.form.get('meta_desc', '')
                ga_id = request.form.get('ga_id', '').strip()
                form_action = request.form.get('form_action', '').strip()
                
                pdf_bytes = file.read()
                doc = fitz.open(stream=pdf_bytes, filetype="pdf")
                section_pages = index_document_sections(doc)
                
                # Google Analytics tag script injection
                ga_script = f"""
                <script async src="https://www.googletagmanager.com/gtag/js?id={ga_id}"></script>
                <script>
                  window.dataLayer = window.dataLayer || [];
                  function gtag(){{dataLayer.push(arguments);}}
                  gtag('js', new Date()); gtag('config', '{ga_id}');
                </script>
                """ if ga_id else ""
                
                # Wrap pages inside a form if target action is specified
                form_open = f'<form action="{form_action}" method="POST">' if form_action else ''
                form_close = '</form>' if form_action else ''

                # MODE A: Single Page Compiled Output
                if layout_mode == 'single':
                    pages_body_html = []
                    for p in range(1, len(doc) + 1):
                        pages_body_html.append(compile_page_elements(doc, p, section_pages, zoom, is_multipage=False))
                        
                    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{meta_title}</title>
    <meta name="description" content="{meta_desc}">
    {ga_script}
    <style>{get_base_styles()}</style>
</head>
<body>
    {form_open}
    <main style="width: 100%; max-width: 100%; box-sizing: border-box;">
        {"".join(pages_body_html)}
    </main>
    {form_close}
</body>
</html>"""
                    
                    response = Response(
                        html_content.encode('utf-8'),
                        mimetype='application/octet-stream',
                        headers={'Content-Disposition': f'attachment; filename="{os.path.splitext(file.filename)[0]}.html"'}
                    )
                    return response
                
                # MODE B: Multi-page ZIP Directory Output
                else:
                    zip_buffer = io.BytesIO()
                    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                        for p in range(1, len(doc) + 1):
                            page_body = compile_page_elements(doc, p, section_pages, zoom, is_multipage=True)
                            page_filename = get_page_filename(p, section_pages)
                            
                            page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{meta_title} - Page {p}</title>
    <meta name="description" content="{meta_desc}">
    {ga_script}
    <style>{get_base_styles()}</style>
</head>
<body>
    {form_open}
    <main style="width: 100%; max-width: 100%; box-sizing: border-box;">
        {page_body}
    </main>
    {form_close}
</body>
</html>"""
                            zip_file.writestr(page_filename, page_html)
                            
                    zip_buffer.seek(0)
                    response = Response(
                        zip_buffer.getvalue(),
                        mimetype='application/zip',
                        headers={'Content-Disposition': f'attachment; filename="{os.path.splitext(file.filename)[0]}_website.zip"'}
                    )
                    return response
                    
            except Exception as e:
                flash(f"Error compiling project files: {str(e)}")
                return redirect(request.url)
                
    return render_template('index.html')

if __name__ == '__main__':
    app.run(debug=True, port=5000)