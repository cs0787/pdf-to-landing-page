import os
import re
import io
import base64
import zipfile
import traceback
from flask import Flask, request, render_template, redirect, url_for, flash, Response, jsonify
import fitz  # PyMuPDF
from PIL import Image

# EXPLICIT TEMPLATE PATH RESOLUTION FOR VERCEL SERVERLESS ENVS
template_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), 'templates'))
app = Flask(__name__, template_folder=template_dir)

app.secret_key = 'pdf_converter_secret_key'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # Limit uploads to 50MB

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

# 3. Dynamic Form Field & Submit Overlays
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
                                field_type, placeholder = parts[0], parts[1]
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

# 7. Global CSS Configurations
def get_base_styles():
    return """
        /* GLOBAL SETUP: SMOOTH SCROLL & SNAP */
        html {
            scroll-behavior: smooth;
            scroll-snap-type: y mandatory; /* Snaps vertically */
            overflow-y: scroll;
            height: 100%;
        }
        body {
            margin: 0; padding: 0; width: 100%; height: 100%;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            overflow-x: hidden;
        }
        
        section {
            height: 100vh;
            width: 100vw;
            display: flex;
            justify-content: center;
            align-items: center;
            scroll-snap-align: start; /* Locks section to top */
            position: relative;
            overflow: hidden;
            background-color: #ffffff;
        }
        
        /* 1. CARD STACKING EFFECT [2] */
        section.effect-sticky-cards {
            position: sticky;
            top: var(--sticky-offset, 0px);
            box-shadow: 0 -15px 35px rgba(0,0,0,0.08);
        }
        
        /* 2. PARALLAX BACKGROUND ZOOM */
        section.parallax-section img {
            transform: scale(calc(1 + (var(--reveal-progress, 0) * var(--zoom-scale-offset, 0.2)))) translateY(calc((1 - var(--reveal-progress, 0)) * var(--zoom-translate-offset, 5%)));
            transform-origin: center center;
            will-change: transform;
        }
        
        /* 3. SPLIT SCREEN SLIDE */
        section.split-section .left-half {
            transform: var(--split-left-start, translateY(40%));
            will-change: transform;
        }
        section.split-section .right-half {
            transform: var(--split-right-start, translateY(-40%));
            will-change: transform;
        }
        
        /* 4. CINEMATIC CURTAIN REVEAL */
        section.curtain-section .page-container {
            clip-path: var(--curtain-clip-state, inset(0 50% 0 50%));
            will-change: clip-path;
        }
        
        /* 5. HORIZONTAL SECTION SLIDE */
        section.horizontal-section .page-container {
            transform: var(--slide-transform-state, translateX(100%));
            will-change: transform;
        }
        
        /* 6. DYNAMIC COLOR BLEED */
        section.bleed-section .bleed-bg {
            position: absolute;
            inset: 0;
            z-index: 1;
            opacity: var(--reveal-progress, 0);
            filter: blur(calc((1 - var(--reveal-progress, 0)) * var(--bleed-blur, 20px)));
            background-color: var(--bleed-color, #ff3366);
            will-change: opacity, filter;
        }
        
        /* 7. 3D CUBE ROTATION */
        section.cube-section {
            perspective: var(--cube-perspective, 1000px);
        }
        section.cube-section .page-container {
            transform-style: preserve-3d;
            transform: rotateX(calc((1 - var(--reveal-progress, 0)) * var(--cube-rotate-deg, -45deg))) translateZ(calc((1 - var(--reveal-progress, 0)) * -30vh));
            opacity: calc(0.3 + (var(--reveal-progress, 0) * 0.7));
            will-change: transform, opacity;
        }
        
        /* 8. TEXT MASK REVEAL */
        section.mask-section .mask-text-element {
            transform: scale(calc(1 + (var(--reveal-progress, 0) * (var(--mask-scale, 15) - 1))));
            opacity: var(--reveal-progress, 0);
            will-change: transform, opacity;
        }

        /* COMPATIBLE STATIC SCROLL TRANSITIONS */
        section.effect-fade .page-container {
            opacity: var(--reveal-progress, 0);
            will-change: opacity;
        }

        section.effect-slide-up .page-container {
            opacity: var(--reveal-progress, 0);
            transform: translateY(calc((1 - var(--reveal-progress, 0)) * var(--start-translate, 60px)));
            will-change: opacity, transform;
        }

        section.effect-zoom-in .page-container {
            opacity: var(--reveal-progress, 0);
            transform: scale(calc(0.93 + (var(--reveal-progress, 0) * 0.07)));
            will-change: opacity, transform;
        }

        section.effect-reveal .page-container {
            clip-path: var(--start-clip, inset(100% 0 0 0));
            will-change: clip-path;
        }

        /* BUTTON HOVER CODES [2] */
        .pdf-link { 
            cursor: pointer; 
            text-decoration: none; 
            transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
            transform: translateY(0) scale(1);
        }
        .pdf-link.effect-glow:hover { 
            background-color: rgba(255, 255, 255, 0.12); 
            backdrop-filter: brightness(1.15) contrast(1.05) saturate(1.1); 
            -webkit-backdrop-filter: brightness(1.15) contrast(1.05) saturate(1.1);
            box-shadow: 0 4px 15px rgba(255, 255, 255, 0.1);
            border-radius: 6px;
        }
        .pdf-link.effect-lift:hover { 
            transform: translateY(-3px) scale(1.02); 
            box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.12), 0 8px 10px -6px rgba(0, 0, 0, 0.12);
            background-color: rgba(255, 255, 255, 0.08); 
            border-radius: 6px;
        }
        .pdf-link.effect-pulse:hover { 
            transform: scale(1.03);
            background-color: rgba(255, 255, 255, 0.08); 
            box-shadow: 0 0 10px rgba(59, 130, 246, 0.3);
            border-radius: 6px;
        }
        .pdf-link:active {
            transform: translateY(0) scale(1);
            box-shadow: none;
        }
        .selectable-text::selection { background-color: rgba(59, 130, 246, 0.25); color: transparent; }
        .selectable-text::-webkit-selection { background-color: rgba(59, 130, 246, 0.25); color: transparent; }
        
        /* FORM COMPONENTS */
        .form-input {
            background: rgba(255, 255, 255, 0.85); border: 1px solid #cbd5e1; border-radius: 4px;
            padding: 4px 12px; font-family: inherit; font-size: 14px; outline: none; transition: all 0.2s ease-in-out;
        }
        .form-input:focus { border-color: #3b82f6; box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.15); background: #ffffff; }
        .textarea-field { resize: none; }
        .form-submit-btn {
            background: #2563eb; color: white; border: none; border-radius: 6px;
            font-weight: 600; font-size: 14px; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
            transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1); cursor: pointer;
        }
        .form-submit-btn:hover { background-color: #1d4ed8; transform: translateY(-2px); box-shadow: 0 10px 15px -3px rgba(37, 99, 235, 0.3), 0 4px 6px -2px rgba(37, 99, 235, 0.15); }
        .form-submit-btn:active { transform: translateY(0); box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1); }

        /* BUTTON STYLE TYPES */
        .btn-style-filled {
            background-color: var(--btn-color, #4f46e5); color: #ffffff !important; border: none; border-radius: 6px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.05); font-weight: 600; text-shadow: 0 1px 1px rgba(0,0,0,0.1);
        }
        .btn-style-outline {
            background-color: transparent; color: var(--btn-color, #4f46e5) !important; border: 2px solid var(--btn-color, #4f46e5) !important;
            border-radius: 6px; font-weight: 600;
        }
        .btn-style-underline {
            background-color: transparent; color: var(--btn-color, #4f46e5) !important; border: none !important;
            border-bottom: 2px solid var(--btn-color, #4f46e5) !important; border-radius: 0; font-weight: 600;
        }
        .btn-style-glass {
            background-color: rgba(255, 255, 255, 0.15); backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
            color: #1e293b !important; border: 1px solid rgba(255, 255, 255, 0.3) !important; border-radius: 6px; font-weight: 600;
        }

        /* Multi-page load animations */
        @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
        @keyframes slideUpIn { from { opacity: 0; transform: translateY(50px); } to { opacity: 1; transform: translateY(0); } }
        @keyframes zoomIn { from { opacity: 0; transform: scale(0.93); } to { opacity: 1; transform: scale(1); } }
        @keyframes revealIn { from { clip-path: inset(100% 0 0 0); } to { clip-path: inset(0 0 0 0); } }
        @keyframes clipRevealIn { from { clip-path: circle(0% at 50% 50%); } to { clip-path: circle(150% at 50% 50%); } }
        @keyframes splitLeft { from { transform: translateY(-60px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }
        @keyframes splitRight { from { transform: translateY(60px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }
        @keyframes horizontalSnapIn { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }
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
        
    try:
        pdf_bytes = file.read()
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
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
        data = request.get_json()
        layout_mode = data.get('layout_mode', 'single')
        meta_title = data.get('meta_title', 'My Smart Website')
        meta_desc = data.get('meta_desc', '')
        ga_id = data.get('ga_id', '').strip()
        form_action = data.get('form_action', '').strip()
        
        pages = data.get('pages', [])
        transitions = data.get('transitions', {})
        custom_links = data.get('custom_links', [])
        
        # Group customized links by page index for injection during build loops
        links_by_page = {}
        for link in custom_links:
            p = int(link["page"])
            if p not in links_by_page:
                links_by_page[p] = []
            links_by_page[p].append(link)
            
        section_pages = {}
        for idx, p_data in enumerate(pages):
            section_pages[idx + 1] = idx + 1
            
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
            t_config = transitions.get(str(p_num))
            if isinstance(t_config, dict):
                transition_effect = t_config.get("effect")
                custom_opts = t_config
            else:
                transition_effect = t_config
                custom_opts = {}

            # Map custom CSS variables for scroll timelines
            css_vars = []
            effect_class = ""
            bleed_bg_html = ""
            mask_text_html = ""
            
            # UPGRADED & CORRECTED: Declare transition_attr correctly for both modes
            transition_attr = f'data-transition="{transition_effect}"' if (transition_effect and not is_multipage_mode) else ''
            
            if transition_effect == "sticky-cards":
                effect_class = "effect-sticky-cards"
                sticky_offset = custom_opts.get("offset", "0px")
                css_vars.append(f"--sticky-offset: {sticky_offset}")
                
            elif transition_effect == "parallax-zoom":
                effect_class = "parallax-section"
                zoom_scale = float(custom_opts.get("zoom_scale", "1.2"))
                zoom_translate = custom_opts.get("zoom_translate", "5%")
                zoom_scale_offset = zoom_scale - 1.0
                css_vars.append(f"--zoom-scale-offset: {zoom_scale_offset}")
                css_vars.append(f"--zoom-translate-offset: {zoom_translate}")
                
            elif transition_effect == "split-screen":
                effect_class = "split-section"
                split_direction = custom_opts.get("split_direction", "vertical")
                if split_direction == "vertical":
                    css_vars.append("--split-left-start: translateY(40%)")
                    css_vars.append("--split-right-start: translateY(-40%)")
                else:
                    css_vars.append("--split-left-start: translateX(-40%)")
                    css_vars.append("--split-right-start: translateX(40%)")
                    
            elif transition_effect == "curtain-reveal":
                effect_class = "curtain-section"
                curtain_shape = custom_opts.get("shape", "circle")
                if curtain_shape == "circle":
                    css_vars.append("--curtain-clip-state: circle(calc((1 - var(--reveal-progress, 0)) * 50% + var(--reveal-progress, 0) * 150%) at 50% 50%)")
                elif curtain_shape == "rectangle":
                    css_vars.append("--curtain-clip-state: inset(calc((1 - var(--reveal-progress, 0)) * 50%) calc((1 - var(--reveal-progress, 0)) * 50%) calc((1 - var(--reveal-progress, 0)) * 50%) calc((1 - var(--reveal-progress, 0)) * 50%))")
                elif curtain_shape == "diamond":
                    css_vars.append("--curtain-clip-state: polygon(50% calc((1 - var(--reveal-progress, 0)) * 50%), calc(50% + var(--reveal-progress, 0) * 50%) 50%, 50% calc(50% + var(--reveal-progress, 0) * 50%), calc(50% - var(--reveal-progress, 0) * 50%) 50%)")
                    
            elif transition_effect == "horizontal-slide":
                effect_class = "horizontal-section"
                slide_dir = custom_opts.get("slide_direction", "right-to-left")
                if slide_dir == "right-to-left":
                    css_vars.append("--slide-transform-state: translateX(calc((1 - var(--reveal-progress, 0)) * 100%))")
                else:
                    css_vars.append("--slide-transform-state: translateX(calc((1 - var(--reveal-progress, 0)) * -100%))")
                    
            elif transition_effect == "color-bleed":
                effect_class = "bleed-section"
                color_theme = custom_opts.get("bleed_color", "indigo")
                blur_amount = custom_opts.get("bleed_blur", "20px")
                
                colors_map = {
                    "indigo": "#4f46e5",
                    "emerald": "#059669",
                    "rose": "#e11d48",
                    "slate": "#1e293b",
                    "orange": "#ea580c"
                }
                theme_color = colors_map.get(color_theme, "#4f46e5")
                css_vars.append(f"--bleed-color: {theme_color}")
                css_vars.append(f"--bleed-blur: {blur_amount}")
                bleed_bg_html = '<div class="bleed-bg"></div>'
                
            elif transition_effect == "cube-rotation":
                effect_class = "cube-section"
                cube_persp = custom_opts.get("perspective", "1000px")
                cube_dir = custom_opts.get("cube_direction", "down")
                css_vars.append(f"--cube-perspective: {cube_persp}")
                if cube_dir == "down":
                    css_vars.append("--cube-rotate-deg: -45deg")
                else:
                    css_vars.append("--cube-rotate-deg: 45deg")
                    
            elif transition_effect == "text-mask-reveal":
                effect_class = "mask-section"
                mask_title = custom_opts.get("mask_title", "DISCOVER").upper()
                mask_scale = custom_opts.get("mask_scale", "15")
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

            speed = custom_opts.get("speed", "0.9s")
            css_vars.append(f"--transition-speed: {speed}")
            css_variables_style = "; ".join(css_vars)
            
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
            animation_style = ""
            
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
                sticky_offset = t_config.get("offset", "0px")
            elif isinstance(t_config, str) and t_config == "sticky-cards":
                is_sticky = True
                sticky_offset = "0px"
                
            if is_sticky and not is_multipage_mode:
                sticky_style = f"position: sticky; top: {sticky_offset}; z-index: {p_num}; box-shadow: 0 10px 30px rgba(0,0,0,0.06);"

            # Add CSS variables styles block to the wrapper
            container_style = f"position: relative; width: 100%; max-width: {page_width}px; height: 100%; max-height: 100%; margin: 0 auto; background: transparent; z-index: {p_num};"

            # Map absolute hyperlinks and customized hover states [2]
            link_overlays = []
            page_links = links_by_page.get(p_num, [])
            for l in page_links:
                bbox = l["bbox"]
                left_pct = (bbox[0] / page_width) * 100
                top_pct = (bbox[1] / page_height) * 100
                width_pct = ((bbox[2] - bbox[0]) / page_width) * 100
                height_pct = ((bbox[3] - bbox[1]) / page_height) * 100
                
                is_btn = l.get("is_button", False)
                btn_class = ""
                btn_styles = []
                
                href = l["href"]
                # Resolve scroll targets on multipage bundles
                if is_multipage_mode and href.startswith("#page-"):
                    try:
                        tgt_num = int(href.split("-")[1])
                        href = get_page_filename(tgt_num, section_pages)
                    except Exception:
                        pass
                
                target_attr = 'target="_blank"' if (not href.startswith("#") and not is_multipage_mode) else ''
                
                if is_btn:
                    style_type = l.get("btn_style", "filled")
                    color_theme = l.get("btn_color", "indigo")
                    hover_effect = l.get("hover_effect", "glow")
                    btn_class = f"btn-style-{style_type} effect-{hover_effect}"
                    
                    colors_map = {
                        "indigo": "#4f46e5",
                        "emerald": "#059669",
                        "rose": "#e11d48",
                        "slate": "#1e293b",
                        "orange": "#ea580c"
                    }
                    theme_color = colors_map.get(color_theme, "#4f46e5")
                    btn_styles.append(f"--btn-color: {theme_color};")
                else:
                    hover_class = f'effect-{l.get("hover_effect", "glow")}'
                    btn_class = hover_class
                
                btn_styles_str = " ".join(btn_styles)
                
                # Check for edited text overrides
                display_text = l.get("edited_text", "").strip()
                
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
                    mask_bg = l.get("bg_color", "#ffffff")
                    # Render solid backdrop block to hide original PDF text underneath
                    mask_html = f'<div style="position: absolute; left: {left_pct}%; top: {top_pct}%; width: {width_pct}%; height: {height_pct}%; background-color: {mask_bg}; z-index: 8;"></div>'
                    link_overlays.append(mask_html)
                    
                    link_overlays.append(f"""
                        <a href="{href}" {target_attr} class="pdf-link {btn_class}" style="
                            position: absolute; left: {left_pct}%; top: {top_pct}%; width: {width_pct}%; height: {height_pct}%; 
                            z-index: 10; display: flex; align-items: center; justify-content: center; font-size: {font_size_pct}vw; {btn_styles_str}
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
                        field_type, placeholder = parts[0], parts[1]
                        field_name = placeholder.lower().replace(" ", "_")
                        
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
                    btn_text = text[8:-1]
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
                font_size_pct = (s.get("size", 12) / page_width) * 100
                selectable_spans.append(f'<span class="selectable-text" style="position: absolute; left: {left}%; top: {top}%; width: {width}%; height: {height}%; font-size: {font_size_pct}vw; line-height:1; color:transparent; white-space:nowrap; transform-origin: left top; pointer-events:auto; user-select:text; -webkit-user-select:text;">{text}</span>')

            # Render page content wrapped inside the uniform section block if single page mode
            if not is_multipage_mode:
                return f"""
                <section class="section-wrapper {effect_class}" style="{css_variables_style}; z-index: {p_num}; {sticky_style}">
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
                # Multi-page layout
                return f"""
                <div id="page-{p_num}" class="page-container" style="position: relative; width: 100%; max-width: {page_width}px; margin: 0 auto; background: #ffffff; {animation_style}">
                    <div style="padding-top: {aspect_ratio}%;"></div>
                    {img_elements}
                    <div class="interactive-overlay" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%;">
                        {"".join(link_overlays)}
                        {"\n".join(forms_layer_html)}
                        {"\n".join(selectable_spans)}
                    </div>
                </div>
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
    {ga_script}
    <style>{get_base_styles()}</style>
</head>
<body>
    {form_open}
    <main style="width: 100%; max-width: 100%; box-sizing: border-box;">
        {"".join(pages_body_html)}
    </main>
    {form_close}
    
    <!-- HIGH COMPATIBILITY VIEWPORT REACTIVE OBSERVER ENGINE [2] -->
    <script>
    document.documentElement.classList.add('js-enabled');
    document.addEventListener("DOMContentLoaded", () => {{
        const sections = document.querySelectorAll('section');
        
        function updateScroll() {{
            const viewportHeight = window.innerHeight;
            sections.forEach(section => {{
                const rect = section.getBoundingClientRect();
                if (rect.top < viewportHeight && rect.bottom > 0) {{
                    const distance = viewportHeight;
                    const progress = (viewportHeight - rect.top) / distance;
                    const clamped = Math.max(0, Math.min(1, progress));
                    section.style.setProperty('--reveal-progress', clamped);
                }} else if (rect.top >= viewportHeight) {{
                    section.style.setProperty('--reveal-progress', 0);
                }} else if (rect.bottom <= 0) {{
                    section.style.setProperty('--reveal-progress', 1);
                }}
            }});
            ticking = false;
        }}
        
        let ticking = false;
        window.addEventListener('scroll', () => {{
            if (!ticking) {{
                window.requestAnimationFrame(updateScroll);
                ticking = true;
            }
        }});
        
        // Execute immediately on load to hydrate current folds view state
        updateScroll();
    }});
    </script>
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
