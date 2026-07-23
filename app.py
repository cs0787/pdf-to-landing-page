import os
import re
import io
import base64
import zipfile
import traceback
import html
from flask import Flask, request, render_template, redirect, url_for, flash, Response, jsonify
import fitz  # PyMuPDF
from PIL import Image

# EXPLICIT TEMPLATE PATH RESOLUTION FOR VERCEL SERVERLESS ENVS
template_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), 'templates'))
app = Flask(__name__, template_folder=template_dir)

app.secret_key = 'pdf_converter_secret_key'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # Limit uploads to 50MB

ALLOWED_TRANSITIONS = {
    'sticky-cards', 'parallax-zoom', 'split-screen', 'curtain-reveal',
    'horizontal-slide', 'color-bleed', 'cube-rotation', 'text-mask-reveal',
    '3d-flip', 'orbital-portal', 'fade', 'slide-up', 'zoom-in', 'reveal'
}


def escape_html(value):
    return html.escape(str(value or ''), quote=True)


def safe_href(value):
    """Keep exported hotspots useful while excluding executable URL schemes."""
    href = str(value or '').strip()
    if href.startswith('#page-'):
        return href
    if re.match(r'^(https?://|mailto:|tel:)', href, re.IGNORECASE):
        return href
    return '#'


def safe_choice(value, choices, fallback):
    return value if value in choices else fallback

def rects_overlap(r1, r2):
    return not (r1.x1 <= r2.x0 or r2.x1 <= r1.x0 or r1.y1 <= r2.y0 or r2.y1 <= r1.y0)

# 1. Topic Scanner: Scores each page to locate matching website sections
def index_document_sections(doc):
    section_pages = {
        "home": 1,
        "about": 1,
        "services": 1,
        "pricing": 1,
        "portfolio": 1,
        "contact": len(doc),  # Default contact to the last page
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

# 3. Dynamic Form Field & Submit Overlays from raw placeholders
def generate_form_fields_layer(page, page_width, page_height):
    fields_html = []
    try:
        dict_data = page.get_text("dict")
        for block in dict_data.get("blocks", []):
            if "lines" in block:
                for line in block["lines"]:
                    for span in line["spans"]:
                        text = span["text"].strip()
                        
                        if text.startswith("[input:") and text.endswith("]"):
                            bbox = span["bbox"]
                            parts = text[7:-1].split(":")
                            if len(parts) >= 2:
                                field_type = parts[0]  # text, email, textarea, etc.
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

# 4. Extract Selectable & Searchable Transparent Text Layer [1]
def generate_selectable_text_layer(page, page_width, page_height):
    spans_html = []
    try:
        dict_data = page.get_text("dict")
        for block in dict_data.get("blocks", []):
            if "lines" in block:
                for line in block["lines"]:
                    for span in line["spans"]:
                        text = span["text"]
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

# 5. Extract Utility links (Raw URLs, Emails, Phones)
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

# 7. Global CSS Configurations [2,3]
def get_base_styles():
    return """
        :root { color-scheme: light; }
        * { box-sizing: border-box; }
        html { height: 100%; scroll-behavior: smooth; scroll-snap-type: y mandatory; background: #070b18; }
        body { margin: 0; min-height: 100%; overflow-x: hidden; background: #070b18; color: #fff; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
        .section-wrapper { min-height: 100vh; width: 100%; display: grid; place-items: center; position: relative; overflow: clip; scroll-snap-align: start; isolation: isolate; background: radial-gradient(circle at 50% 100%, #172554 0%, #0f172a 35%, #070b18 75%); padding: 2.5rem 1rem; perspective: var(--cube-perspective, 1400px); }
        .section-wrapper::before { content: ""; position: absolute; inset: 0; z-index: -2; background: radial-gradient(circle at 15% 20%, color-mix(in srgb, var(--bleed-color, #6366f1) 30%, transparent), transparent 33%), radial-gradient(circle at 80% 75%, rgba(20, 184, 166, .14), transparent 28%); opacity: .85; }
        .section-wrapper::after { content: ""; position: absolute; inset: 0; z-index: -1; opacity: .25; background-image: linear-gradient(rgba(255,255,255,.13) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,.13) 1px, transparent 1px); background-size: 72px 72px; mask-image: linear-gradient(to bottom, transparent, #000 22%, #000 78%, transparent); }
        .page-container { position: relative; isolation: isolate; transform-style: preserve-3d; will-change: transform, opacity, filter, clip-path; transition: transform var(--transition-speed, .9s) cubic-bezier(.16,1,.3,1), opacity var(--transition-speed, .9s) ease, filter var(--transition-speed, .9s) ease, clip-path var(--transition-speed, .9s) cubic-bezier(.16,1,.3,1); box-shadow: 0 35px 90px rgba(0,0,0,.42), 0 0 0 1px rgba(255,255,255,.13); }
        .page-container::before { content: ""; position: absolute; inset: -1px; z-index: -1; border-radius: inherit; background: linear-gradient(135deg, rgba(255,255,255,.65), transparent 26%, transparent 70%, rgba(99,102,241,.55)); filter: blur(12px); opacity: .32; }
        .page-container img { -webkit-user-drag: none; }
        .interactive-overlay { transform: translateZ(2px); }
        .section-wrapper:not(.is-visible) .page-container[data-transition="fade"] { opacity: 0; }
        .section-wrapper:not(.is-visible) .page-container[data-transition="slide-up"] { opacity: 0; transform: translateY(70px) scale(.96); }
        .section-wrapper:not(.is-visible) .page-container[data-transition="zoom-in"] { opacity: 0; transform: scale(.82); filter: blur(12px); }
        .section-wrapper:not(.is-visible) .page-container[data-transition="reveal"] { clip-path: inset(100% 0 0 0 round 20px); }
        .section-wrapper:not(.is-visible) .page-container[data-transition="curtain-reveal"] { clip-path: polygon(50% 50%,50% 50%,50% 50%,50% 50%); }
        .section-wrapper:not(.is-visible) .page-container[data-transition="horizontal-slide"] { opacity: 0; transform: translateX(var(--slide-distance, 120%)) rotateY(var(--slide-tilt, -14deg)); }
        .section-wrapper:not(.is-visible) .page-container[data-transition="cube-rotation"] { opacity: 0; transform: var(--cube-start, rotateX(-58deg) rotateY(8deg) translateZ(-180px)); }
        .section-wrapper:not(.is-visible) .page-container[data-transition="3d-flip"] { opacity: 0; transform: rotateY(var(--flip-start, 78deg)) translateZ(-130px); transform-origin: var(--flip-origin, left center); }
        .section-wrapper:not(.is-visible) .page-container[data-transition="orbital-portal"] { opacity: 0; transform: scale(.28) rotateZ(-16deg) translateY(60px); filter: blur(15px) saturate(1.7); clip-path: circle(0 at 50% 50%); }
        .section-wrapper.effect-sticky-cards { position: sticky; top: var(--sticky-offset, 0px); box-shadow: 0 -16px 50px rgba(0,0,0,.38); }
        .section-wrapper.parallax-section img { animation: gentle-zoom 9s ease-in-out infinite alternate; transform-origin: center; }
        .section-wrapper.split-section .left-half { transform: translateX(-15%); transition: transform var(--transition-speed, .9s) cubic-bezier(.16,1,.3,1); }
        .section-wrapper.split-section .right-half { transform: translateX(15%); transition: transform var(--transition-speed, .9s) cubic-bezier(.16,1,.3,1); }
        .section-wrapper.split-section.is-visible .left-half, .section-wrapper.split-section.is-visible .right-half { transform: translateX(0); }
        .section-wrapper.bleed-section .bleed-bg { position: absolute; inset: -10%; z-index: -1; background: radial-gradient(circle at 50% 50%, var(--bleed-color,#6366f1), transparent 58%); filter: blur(var(--bleed-blur,35px)); opacity: 0; transform: scale(.7); transition: opacity var(--transition-speed,.9s) ease, transform var(--transition-speed,.9s) ease; }
        .section-wrapper.bleed-section.is-visible .bleed-bg { opacity: .72; transform: scale(1.25); }
        .mask-text-element { display: grid; place-items: center; width: 100%; height: 100%; pointer-events: none; font-size: clamp(3rem, 16vw, 14rem); color: rgba(255,255,255,.95); text-shadow: 0 0 60px rgba(129,140,248,.8); mix-blend-mode: screen; transition: transform 1.1s cubic-bezier(.16,1,.3,1), opacity .8s ease; }
        .mask-section:not(.is-visible) .mask-text-element { transform: scale(.45); opacity: 0; }
        .mask-section.is-visible .mask-text-element { transform: scale(var(--mask-scale, 2.1)); opacity: .15; }
        @keyframes gentle-zoom { from { transform: scale(1); } to { transform: scale(var(--zoom-scale,1.12)) translateY(var(--zoom-translate,-1.5%)); } }
        .pdf-link { cursor: pointer; text-decoration: none; transition: transform .3s cubic-bezier(.16,1,.3,1), box-shadow .3s ease, background-color .3s ease, filter .3s ease; transform: translateZ(8px); }
        .pdf-link.effect-glow:hover { background: rgba(255,255,255,.14); backdrop-filter: blur(10px) brightness(1.25); box-shadow: 0 0 0 1px rgba(255,255,255,.48), 0 0 28px rgba(129,140,248,.6); border-radius: 8px; }
        .pdf-link.effect-lift:hover { transform: translateY(-5px) translateZ(20px) scale(1.025); background: rgba(255,255,255,.08); box-shadow: 0 22px 30px -14px rgba(0,0,0,.65); border-radius: 8px; }
        .pdf-link.effect-pulse:hover { animation: link-pulse .95s infinite alternate; background: rgba(255,255,255,.09); border-radius: 8px; }
        @keyframes link-pulse { to { transform: scale(1.05) translateZ(16px); box-shadow: 0 0 28px rgba(34,211,238,.65); } }
        .form-input { background: rgba(255,255,255,.92); color: #0f172a; border: 1px solid #cbd5e1; border-radius: 7px; padding: 4px 12px; font: inherit; font-size: 14px; outline: none; transition: .2s ease; }
        .form-input:focus { border-color: #818cf8; box-shadow: 0 0 0 3px rgba(129,140,248,.28); }
        .textarea-field { resize: none; }
        .form-submit-btn { background: linear-gradient(135deg,#6366f1,#a855f7); color:#fff; border:0; border-radius:7px; font-weight:700; cursor:pointer; box-shadow:0 10px 22px rgba(99,102,241,.35); transition:.25s ease; }
        .form-submit-btn:hover { transform:translateY(-3px); filter:brightness(1.1); box-shadow:0 17px 28px rgba(99,102,241,.46); }
        .btn-style-filled { background-color:var(--btn-color,#6366f1); color:#fff!important; border:0; border-radius:8px; box-shadow:0 8px 20px rgba(0,0,0,.22); font-weight:700; }
        .btn-style-outline { background:transparent; color:var(--btn-color,#6366f1)!important; border:2px solid var(--btn-color,#6366f1)!important; border-radius:8px; font-weight:700; }
        .btn-style-underline { background:transparent; color:var(--btn-color,#6366f1)!important; border:0!important; border-bottom:2px solid var(--btn-color,#6366f1)!important; font-weight:700; }
        .btn-style-glass { background:rgba(255,255,255,.18); color:#fff!important; border:1px solid rgba(255,255,255,.48)!important; border-radius:8px; backdrop-filter:blur(12px); font-weight:700; }
        .selectable-text::selection { background:rgba(129,140,248,.38); color:transparent; }
        @media (prefers-reduced-motion: reduce) { *,*::before,*::after { animation-duration:.01ms!important; animation-iteration-count:1!important; transition-duration:.01ms!important; scroll-behavior:auto!important; } }
    """


def get_runtime_script():
    return """
    <script>
    (function () {
        const sections = document.querySelectorAll('.section-wrapper');
        if (!('IntersectionObserver' in window)) {
            sections.forEach(section => section.classList.add('is-visible'));
            return;
        }
        const observer = new IntersectionObserver((entries) => {
            entries.forEach(({ target, isIntersecting }) => {
                if (isIntersecting) target.classList.add('is-visible');
            });
        }, { threshold: 0.22, rootMargin: '0px 0px -8% 0px' });
        sections.forEach(section => observer.observe(section));
    }());
    </script>
    """

# ----------------- APP ROUTES -----------------

# INDEX HOME PAGE ROUTER
@app.route('/')
def home():
    return render_template('index.html')

# Phase 1: Heavy rendering and span extraction
@app.route('/process', methods=['POST'])
def process_pdf():
    if 'pdf_file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    file = request.files['pdf_file']
    if file.filename == '':
        return jsonify({"error": "Empty filename"}), 400
    if not file.filename.lower().endswith('.pdf'):
        return jsonify({"error": "Please upload a PDF file."}), 400
        
    try:
        pdf_bytes = file.read()
        if not pdf_bytes:
            return jsonify({"error": "The uploaded PDF is empty."}), 400
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        if len(doc) == 0:
            return jsonify({"error": "The uploaded PDF has no pages."}), 400
        section_pages = index_document_sections(doc)
        
        pages_data = []
        global_auto_links = []
        
        for page_num in range(len(doc)):
            page = doc[page_num]
            p_num = page_num + 1
            page_width = page.rect.width
            page_height = page.rect.height
            aspect_ratio = (page_height / page_width) * 100
            
            # High-density WebP render
            zoom = 2.0
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat)
            png_bytes = pix.tobytes("png")
            
            img = Image.open(io.BytesIO(png_bytes))
            webp_io = io.BytesIO()
            img.save(webp_io, format="WEBP", quality=85)
            img_base64 = "data:image/webp;base64," + base64.b64encode(webp_io.getvalue()).decode("utf-8")
            
            # Extract standard text spans [1]
            spans = []
            dict_data = page.get_text("dict")
            for block in dict_data.get("blocks", []):
                if "lines" in block:
                    for line in block["lines"]:
                        for span in line["spans"]:
                            spans.append({
                                "text": span["text"],
                                "bbox": span["bbox"],
                                "font_size": span["size"]  # Hydrate actual layout size
                            })
            
            # UNIFIED RUN: Process and attach all smart detected layers directly as custom_links
            existing_rects = []
            
            # A. Native PDF Links (Canva/Figma manually added hotspots)
            native_links = page.get_links()
            for link in native_links:
                href, target_blank = "", True
                if "uri" in link:
                    href = link["uri"]
                elif "page" in link:
                    href = f"#page-{link['page'] + 1}"
                    target_blank = False
                if href:
                    rect = link["from"]
                    existing_rects.append(rect)
                    global_auto_links.append({
                        "page": p_num,
                        "bbox": [rect.x0, rect.y0, rect.x1, rect.y1],
                        "href": href,
                        "hover_effect": "glow",
                        "text": page.get_text("text", clip=rect).strip() or "Manual PDF Link",
                        "target_blank": target_blank,
                        "is_button": False,
                        "font_size": rect.y1 - rect.y0
                    })
                    
            # B. Smart Text Utilities (URLs, Emails, Phones)
            utilities = extract_utilities(page)
            for item in utilities:
                if not any(rects_overlap(item["rect"], r) for r in existing_rects):
                    rect = item["rect"]
                    existing_rects.append(rect)
                    global_auto_links.append({
                        "page": p_num,
                        "bbox": [rect.x0, rect.y0, rect.x1, rect.y1],
                        "href": item["href"],
                        "hover_effect": "glow",
                        "text": page.get_text("text", clip=rect).strip() or "Utility Link",
                        "target_blank": item.get("target") == 'target="_blank"',
                        "is_button": False,
                        "font_size": rect.y1 - rect.y0
                    })
                    
            # C. Heuristic Button Intent Router (Contact Us, About, Home)
            smart_buttons = detect_smart_button_intents(page, section_pages, is_multipage=False)
            for btn in smart_buttons:
                if not any(rects_overlap(btn["rect"], r) for r in existing_rects):
                    rect = btn["rect"]
                    existing_rects.append(rect)
                    global_auto_links.append({
                        "page": p_num,
                        "bbox": [rect.x0, rect.y0, rect.x1, rect.y1],
                        "href": btn["href"],
                        "hover_effect": "glow",
                        "text": page.get_text("text", clip=rect).strip() or "Intelligent Button",
                        "target_blank": False,
                        "is_button": False,
                        "font_size": rect.y1 - rect.y0
                    })
            
            pages_data.append({
                "number": p_num,
                "width": page_width,
                "height": page_height,
                "aspect_ratio": aspect_ratio,
                "image": img_base64,
                "spans": spans
            })
            
        return jsonify({
            "pages": pages_data,
            "auto_links": global_auto_links
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Phase 2: Stateless visual compiler
@app.route('/compile', methods=['POST'])
def compile_site():
    try:
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"error": "Export payload must be a JSON object."}), 400
        layout_mode = safe_choice(data.get('layout_mode', 'single'), {'single', 'multipage'}, 'single')
        meta_title = escape_html(data.get('meta_title', 'My Smart Website'))
        meta_desc = escape_html(data.get('meta_desc', ''))
        ga_id = re.sub(r'[^A-Za-z0-9_-]', '', str(data.get('ga_id', '')).strip())
        form_action = safe_href(str(data.get('form_action', '')).strip())
        if form_action == '#':
            form_action = ''
        
        pages = data.get('pages', [])
        transitions = data.get('transitions', {})
        custom_links = data.get('custom_links', [])
        if not isinstance(pages, list) or not pages:
            return jsonify({"error": "No rendered PDF pages were supplied for export."}), 400
        if not isinstance(transitions, dict):
            transitions = {}
        if not isinstance(custom_links, list):
            custom_links = []
        
        # Group customized links by page index for injection during build loops
        links_by_page = {}
        for link in custom_links:
            if not isinstance(link, dict):
                continue
            try:
                p = int(link["page"])
            except (KeyError, TypeError, ValueError):
                continue
            if p < 1 or p > len(pages):
                continue
            if p not in links_by_page:
                links_by_page[p] = []
            links_by_page[p].append(link)
            
        # The editor currently exports a generic page sequence. Keep the first
        # document as index.html and use stable page-N.html names thereafter.
        section_pages = {"home": 1}
            
        ga_script = f"""
        <script async src="https://www.googletagmanager.com/gtag/js?id={ga_id}"></script>
        <script>
          window.dataLayer = window.dataLayer || [];
          function gtag(){{dataLayer.push(arguments);}}
          gtag('js', new Date()); gtag('config', '{ga_id}');
        </script>
        """ if ga_id else ""
        
        form_open = f'<form action="{form_action}" method="POST" style="margin:0;padding:0;">' if form_action else ''
        form_close = '</form>' if form_action else ''

        def build_page_html(p_data, p_num, is_multipage_mode):
            page_width = p_data["width"]
            page_height = p_data["height"]
            img_base64 = p_data["image"]
            aspect_ratio = p_data["aspect_ratio"]
            
            # Extract configured transition variables
            t_config = transitions.get(str(p_num), transitions.get(p_num))
            if isinstance(t_config, dict):
                transition_effect = t_config.get("effect")
                custom_opts = t_config
            else:
                transition_effect = t_config
                custom_opts = {}
            transition_effect = transition_effect if transition_effect in ALLOWED_TRANSITIONS else ''

            # Map custom CSS variables for scroll timelines
            css_vars = []
            effect_class = ""
            bleed_bg_html = ""
            mask_text_html = ""
            
            if transition_effect == "sticky-cards":
                effect_class = "effect-sticky-cards"
                sticky_offset = safe_choice(custom_opts.get("offset"), {"0px", "20px", "40px", "60px"}, "0px")
                css_vars.append(f"--sticky-offset: {sticky_offset}")
                
            elif transition_effect == "parallax-zoom":
                effect_class = "parallax-section"
                zoom_scale = safe_choice(custom_opts.get("zoom_scale"), {"1.06", "1.12", "1.2", "1.3"}, "1.12")
                zoom_translate = safe_choice(custom_opts.get("zoom_translate"), {"-3%", "-1.5%", "1.5%", "3%"}, "-1.5%")
                css_vars.append(f"--zoom-scale: {zoom_scale}")
                css_vars.append(f"--zoom-translate: {zoom_translate}")
                
            elif transition_effect == "split-screen":
                effect_class = "split-section"
                split_direction = custom_opts.get("split_direction", "vertical")
                if split_direction == "vertical":
                    css_vars.append("--split-start: translateY(40%)")
                else:
                    css_vars.append("--split-start: translateX(40%)")
                    
            elif transition_effect == "curtain-reveal":
                effect_class = "curtain-section"
                curtain_shape = safe_choice(custom_opts.get("shape"), {"circle", "rectangle", "diamond"}, "circle")
                if curtain_shape == "circle":
                    css_vars.append("--curtain-start: circle(0% at 50% 50%)")
                elif curtain_shape == "rectangle":
                    css_vars.append("--curtain-start: inset(50% 50% 50% 50%)")
                elif curtain_shape == "diamond":
                    css_vars.append("--curtain-start: polygon(50% 50%, 50% 50%, 50% 50%, 50% 50%)")
                    
            elif transition_effect == "horizontal-slide":
                effect_class = "horizontal-section"
                slide_dir = safe_choice(custom_opts.get("slide_direction"), {"right-to-left", "left-to-right"}, "right-to-left")
                if slide_dir == "right-to-left":
                    css_vars.append("--slide-distance: 120%")
                    css_vars.append("--slide-tilt: -14deg")
                else:
                    css_vars.append("--slide-distance: -120%")
                    css_vars.append("--slide-tilt: 14deg")
                    
            elif transition_effect == "color-bleed":
                effect_class = "bleed-section"
                color_theme = safe_choice(custom_opts.get("bleed_color"), {"indigo", "emerald", "rose", "slate", "orange", "cyan"}, "indigo")
                blur_amount = safe_choice(custom_opts.get("bleed_blur"), {"20px", "35px", "55px"}, "35px")
                
                colors_map = {
                    "indigo": "#4f46e5",
                    "emerald": "#059669",
                    "rose": "#e11d48",
                    "slate": "#1e293b",
                    "orange": "#ea580c",
                    "cyan": "#0891b2"
                }
                theme_color = colors_map.get(color_theme, "#ff3366")
                css_vars.append(f"--bleed-color: {theme_color}")
                css_vars.append(f"--bleed-blur: {blur_amount}")
                bleed_bg_html = '<div class="bleed-bg"></div>'
                
            elif transition_effect == "cube-rotation":
                effect_class = "cube-section"
                cube_persp = safe_choice(custom_opts.get("perspective"), {"800px", "1000px", "1400px", "1800px"}, "1400px")
                cube_dir = custom_opts.get("cube_direction", "down")
                css_vars.append(f"--cube-perspective: {cube_persp}")
                if cube_dir == "down":
                    css_vars.append("--cube-start: rotateX(-58deg) rotateY(8deg) translateZ(-180px)")
                else:
                    css_vars.append("--cube-start: rotateX(58deg) rotateY(-8deg) translateZ(-180px)")

            elif transition_effect == "3d-flip":
                effect_class = "flip-section"
                flip_direction = safe_choice(custom_opts.get("flip_direction"), {"left", "right"}, "left")
                css_vars.append("--flip-start: 78deg" if flip_direction == "left" else "--flip-start: -78deg")
                css_vars.append("--flip-origin: left center" if flip_direction == "left" else "--flip-origin: right center")

            elif transition_effect == "orbital-portal":
                effect_class = "portal-section"
                    
            elif transition_effect == "text-mask-reveal":
                effect_class = "mask-section"
                mask_title = escape_html(custom_opts.get("mask_title", "DISCOVER").upper())
                mask_scale = safe_choice(str(custom_opts.get("mask_scale", "2.1")), {"1.5", "2.1", "2.8"}, "2.1")
                css_vars.append(f"--mask-scale: {mask_scale}")
                mask_text_html = f"""
                <div class="mask-text-element" style="position: absolute; z-index: 20; color: #1e293b; font-weight: 900; font-family: -apple-system, sans-serif; letter-spacing: -2px;">
                    {mask_title}
                </div>
                """
            
            # Map standard scroll transitions fallback styles
            elif transition_effect == "fade":
                effect_class = "effect-fade"
            elif transition_effect == "slide-up":
                effect_class = "effect-slide-up"
                css_vars.append("--start-translate: translateY(60px)")
            elif transition_effect == "zoom-in":
                effect_class = "effect-zoom-in"
                css_vars.append("--start-translate: scale(0.93)")
            elif transition_effect == "reveal":
                effect_class = "effect-reveal"
                css_vars.append("--start-clip: inset(100% 0 0 0)")

            speed = safe_choice(custom_opts.get("speed"), {"0.55s", "0.9s", "1.35s", "1.8s"}, "0.9s")
            css_vars.append(f"--transition-speed: {speed}")
            css_variables_style = "; ".join(css_vars)
            transition_attr = f'data-transition="{transition_effect}"' if transition_effect else ''
            
            # Apply Vertical Split-screen (Clipped Columns)
            if transition_effect == "split-screen":
                img_elements = f"""
                <div class="left-half" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; overflow: hidden; clip-path: inset(0 50% 0 0);">
                    <img src="{img_base64}" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; display: block; object-fit: contain;" alt="Page {p_num} Left" />
                </div>
                <div class="right-half" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; overflow: hidden; clip-path: inset(0 0 0 50%);">
                    <img src="{img_base64}" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; display: block; object-fit: contain;" alt="Page {p_num} Right" />
                </div>
                """
            else:
                img_elements = f'<img src="{img_base64}" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; display: block; object-fit: contain;" alt="Page {p_num}" />'

            # STICKY STACKING CARDS INTEGRATION ENGINE [2]
            sticky_style = ""
            is_sticky = False
            sticky_offset = "0px"
            
            # Case 1: The next page transitions using sticky cards (this page must stick as the base)
            next_page_num = p_num + 1
            next_page_config = transitions.get(str(next_page_num))
            if isinstance(next_page_config, dict) and next_page_config.get("effect") == "sticky-cards":
                is_sticky = True
                sticky_offset = "0px"
            elif isinstance(next_page_config, str) and next_page_config == "sticky-cards":
                is_sticky = True
                sticky_offset = "0px"
                
            # Case 2: This page itself transitions using sticky cards (this page is a deck card and must stick)
            if isinstance(t_config, dict) and t_config.get("effect") == "sticky-cards":
                is_sticky = True
                sticky_offset = safe_choice(t_config.get("offset"), {"0px", "20px", "40px", "60px"}, "0px")
            elif isinstance(t_config, str) and t_config == "sticky-cards":
                is_sticky = True
                sticky_offset = "0px"
                
            if is_sticky and not is_multipage_mode:
                sticky_style = f"position: sticky; top: {sticky_offset}; z-index: {p_num}; box-shadow: 0 10px 30px rgba(0,0,0,0.06);"

            # Add CSS variables styles block to the wrapper
            container_style = f"position: relative; width: 100%; max-width: {page_width}px; height: 100%; max-height: 100%; margin: 0 auto; background: transparent; z-index: {p_num};"
            animation_style = ""

            # Map absolute hyperlinks and customized hover states [2]
            link_overlays = []
            page_links = links_by_page.get(p_num, [])
            for l in page_links:
                bbox = l.get("bbox", [])
                if not isinstance(bbox, list) or len(bbox) != 4:
                    continue
                try:
                    bbox = [float(value) for value in bbox]
                except (TypeError, ValueError):
                    continue
                left_pct = (bbox[0] / page_width) * 100
                top_pct = (bbox[1] / page_height) * 100
                width_pct = ((bbox[2] - bbox[0]) / page_width) * 100
                height_pct = ((bbox[3] - bbox[1]) / page_height) * 100
                
                is_btn = l.get("is_button", False)
                btn_class = ""
                btn_styles = []
                
                href = safe_href(l.get("href", "#"))
                # Resolve scroll targets on multipage bundles
                if is_multipage_mode and href.startswith("#page-"):
                    try:
                        tgt_num = int(href.split("-")[1])
                        href = get_page_filename(tgt_num, section_pages)
                    except Exception:
                        pass
                
                target_attr = 'target="_blank" rel="noopener noreferrer"' if re.match(r'^https?://', href, re.IGNORECASE) else ''
                href = escape_html(href)
                
                if is_btn:
                    style_type = safe_choice(l.get("btn_style"), {"filled", "outline", "underline", "glass"}, "filled")
                    color_theme = safe_choice(l.get("btn_color"), {"indigo", "emerald", "rose", "slate", "orange", "cyan"}, "indigo")
                    hover_effect = safe_choice(l.get("hover_effect"), {"glow", "lift", "pulse", "none"}, "glow")
                    btn_class = f"btn-style-{style_type} effect-{hover_effect}"
                    
                    colors_map = {
                        "indigo": "#4f46e5",
                        "emerald": "#059669",
                        "rose": "#e11d48",
                        "slate": "#1e293b",
                        "orange": "#ea580c",
                        "cyan": "#0891b2"
                    }
                    theme_color = colors_map.get(color_theme, "#4f46e5")
                    btn_styles.append(f"--btn-color: {theme_color};")
                else:
                    hover_class = f'effect-{safe_choice(l.get("hover_effect"), {"glow", "lift", "pulse", "none"}, "glow")}'
                    btn_class = hover_class
                
                btn_styles_str = " ".join(btn_styles)
                
                # Check for edited text overrides
                display_text = escape_html(str(l.get("edited_text", "")).strip())
                
                # Dynamic failsafe types conversion of font_size mapping
                f_size = l.get("font_size")
                if f_size is None:
                    f_size = 14
                try:
                    f_size = float(f_size)
                except (TypeError, ValueError):
                    f_size = 14
                    
                font_size_pct = (f_size / page_width) * 100
                
                if display_text:
                    mask_bg = str(l.get("bg_color", "#ffffff"))
                    if not re.match(r'^#[0-9a-fA-F]{6}$', mask_bg):
                        mask_bg = "#ffffff"
                    # Render solid backdrop block to hide original PDF text underneath
                    mask_html = f'<div style="position: absolute; left: {left_pct}%; top: {top_pct}%; width: {width_pct}%; height: {height_pct}%; background-color: {mask_bg}; z-index: 8;"></div>'
                    link_overlays.append(mask_html)
                    
                    link_overlays.append(f"""
                        <a href="{href}" {target_attr} class="pdf-link {btn_class}" style="
                            position: absolute; left: {left_pct}%; top: {top_pct}%; width: {width_pct}%; height: {height_pct}%; 
                            z-index: 10; display: flex; align-items: center; justify-content: center; color: #111827; font-size: {font_size_pct}vw; {btn_styles_str}
                        ">{display_text}</a>
                    """)
                else:
                    link_overlays.append(f'<a href="{href}" {target_attr} class="pdf-link {btn_class}" style="position: absolute; left: {left_pct}%; top: {top_pct}%; width: {width_pct}%; height: {height_pct}%; z-index: 10; {btn_styles_str}"></a>')
            
            # Map standard placeholder inputs
            forms_layer_html = []
            for s in p_data.get("spans", []):
                text = s["text"].strip()
                if text.startswith("[input:") and text.endswith("]"):
                    bbox = s["bbox"]
                    parts = text[7:-1].split(":")
                    if len(parts) >= 2:
                        field_type = safe_choice(parts[0], {"text", "email", "tel", "url", "number", "password", "textarea"}, "text")
                        placeholder = escape_html(parts[1])
                        field_name = re.sub(r'[^a-z0-9_]', '', placeholder.lower().replace(" ", "_")) or "field"
                        
                        left = (bbox[0] / page_width) * 100
                        top = (bbox[1] / page_height) * 100
                        width = ((bbox[2] - bbox[0]) / page_width) * 100
                        height = ((bbox[3] - bbox[1]) / page_height) * 100
                        
                        if field_type == "textarea":
                            forms_layer_html.append(f'<textarea name="{field_name}" placeholder="{placeholder}" required class="form-input textarea-field" style="position: absolute; left: {left}%; top: {top}%; width: {width}%; height: {height}%; z-index: 15;"></textarea>')
                        else:
                            forms_layer_html.append(f'<input type="{field_type}" name="{field_name}" placeholder="{placeholder}" required class="form-input input-field" style="position: absolute; left: {left}%; top: {top}%; width: {width}%; height: {height}%; z-index: 15;"/>')
                elif text.startswith("[submit:") and text.endswith("]"):
                    bbox = s["bbox"]
                    btn_text = escape_html(text[8:-1])
                    left = (bbox[0] / page_width) * 100
                    top = (bbox[1] / page_height) * 100
                    width = ((bbox[2] - bbox[0]) / page_width) * 100
                    height = ((bbox[3] - bbox[1]) / page_height) * 100
                    forms_layer_html.append(f'<button type="submit" class="form-submit-btn" style="position: absolute; left: {left}%; top: {top}%; width: {width}%; height: {height}%; z-index: 15;">{btn_text}</button>')

            # Searchable selectable text overlay [1]
            selectable_spans = []
            for s in p_data.get("spans", []):
                text = s["text"]
                if text.strip().startswith("[input:") or text.strip().startswith("[submit:"): continue
                bbox = s["bbox"]
                left = (bbox[0] / page_width) * 100
                top = (bbox[1] / page_height) * 100
                width = ((bbox[2] - bbox[0]) / page_width) * 100
                height = ((bbox[3] - bbox[1]) / page_height) * 100
                font_size = s.get("font_size", s.get("size", 12))
                try:
                    font_size = float(font_size)
                except (TypeError, ValueError):
                    font_size = 12
                font_size_pct = (font_size / page_width) * 100
                selectable_spans.append(f'<span class="selectable-text" style="position: absolute; left: {left}%; top: {top}%; width: {width}%; height: {height}%; font-size: {font_size_pct}vw; line-height:1; color:transparent; white-space:nowrap; transform-origin: left top; pointer-events:auto; user-select:text; -webkit-user-select:text;">{escape_html(text)}</span>')

            # Render page content wrapped inside the uniform section block if single page mode
            if not is_multipage_mode:
                return f"""
                <section class="section-wrapper {effect_class}" style="{css_variables_style}; z-index: {p_num};">
                    {bleed_bg_html}
                    {mask_text_html}
                    <div id="page-{p_num}" class="page-container" {transition_attr} style="{container_style}">
                        <div style="padding-top: {aspect_ratio}%;"></div>
                        {img_elements}
                        <div class="interactive-overlay" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%;">
                            {"".join(link_overlays)}
                            {"\n".join(forms_layer_html)}
                            {"\n".join(selectable_spans)}
                        </div>
                    </div>
                </section>
                """
            else:
                # Multi-page layout: retain the same opening transition treatment on each file.
                return f"""
                <section class="section-wrapper {effect_class}" style="{css_variables_style};">
                    {bleed_bg_html}
                    {mask_text_html}
                    <div id="page-{p_num}" class="page-container" {transition_attr} style="position: relative; width: 100%; max-width: {page_width}px; margin: 0 auto; background: #ffffff; {animation_style}">
                        <div style="padding-top: {aspect_ratio}%;"></div>
                        {img_elements}
                        <div class="interactive-overlay" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%;">
                            {"".join(link_overlays)}
                            {"\n".join(forms_layer_html)}
                            {"\n".join(selectable_spans)}
                        </div>
                    </div>
                </section>
                """
        
        # EXPORT OPTION A: Single Page Compiled Output
        if layout_mode == 'single':
            pages_body_html = []
            for idx, p_data in enumerate(pages):
                p_num = idx + 1
                pages_body_html.append(build_page_html(p_data, p_num, is_multipage_mode=False))
                
            html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{meta_title}</title>
    <meta name="description" content="{meta_desc}">
    <script>document.documentElement.classList.add('js-enabled');</script>
    {ga_script}
    <style>{get_base_styles()}</style>
</head>
<body>
    {form_open}
    <main style="width: 100%; max-width: 100%; box-sizing: border-box;">
        {"".join(pages_body_html)}
    </main>
    {form_close}
    {get_runtime_script()}
</body>
</html>"""
            
            return Response(
                html_content.encode('utf-8'),
                mimetype='application/octet-stream',
                headers={'Content-Disposition': f'attachment; filename="index.html"'}
            )
            
        # EXPORT OPTION B: Multi-page ZIP Output
        else:
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                for idx, p_data in enumerate(pages):
                    p_num = idx + 1
                    page_body = build_page_html(p_data, p_num, is_multipage_mode=True)
                    page_filename = get_page_filename(p_num, section_pages)
                    
                    page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{meta_title} - Page {p_num}</title>
    <meta name="description" content="{meta_desc}">
    <script>document.documentElement.classList.add('js-enabled');</script>
    {ga_script}
    <style>{get_base_styles()}</style>
</head>
<body>
    {form_open}
    <main style="width: 100%; max-width: 100%; box-sizing: border-box;">
        {page_body}
    </main>
    {form_close}
    {get_runtime_script()}
</body>
</html>"""
                    zip_file.writestr(page_filename, page_html)
                    
            zip_buffer.seek(0)
            return Response(
                zip_buffer.getvalue(),
                mimetype='application/zip',
                headers={'Content-Disposition': f'attachment; filename="website_files.zip"'}
            )
    except Exception as e:
        # High fidelity traceback debugger logging
        traceback_msg = traceback.format_exc()
        print(traceback_msg)
        return jsonify({"error": f"Compiler Error: {str(e)}"}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)
