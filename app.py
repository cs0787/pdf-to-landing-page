import os
import re
import io
import base64
import zipfile
from flask import Flask, request, render_template, redirect, url_for, flash, Response, jsonify
import fitz  # PyMuPDF
from PIL import Image

app = Flask(__name__)
app.secret_key = 'pdf_converter_secret_key'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # Limit uploads to 50MB

# Helper to check if two bounding boxes overlap
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
        html, body {
            margin: 0; padding: 0; overflow-x: hidden; width: 100%; scroll-behavior: smooth; background-color: #ffffff;
        }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
        .page-container { border-radius: 0; box-shadow: none; }
        
        /* HOVER EFFECTS FOR BUTTONS [2] */
        .pdf-link { 
            cursor: pointer; 
            text-decoration: none; 
            border-radius: 6px;
            transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
            transform: translateY(0) scale(1);
        }
        
        /* 1. Glass Glow Effect */
        .pdf-link.effect-glow:hover { 
            background-color: rgba(255, 255, 255, 0.12); 
            backdrop-filter: brightness(1.15) contrast(1.05) saturate(1.1); 
            -webkit-backdrop-filter: brightness(1.15) contrast(1.05) saturate(1.1);
            box-shadow: 0 4px 15px rgba(255, 255, 255, 0.1);
            border-radius: 6px;
        }
        
        /* 2. 3D Float Lift Effect */
        .pdf-link.effect-lift:hover { 
            transform: translateY(-3px) scale(1.02); 
            box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.12), 0 8px 10px -6px rgba(0, 0, 0, 0.12);
            background-color: rgba(255, 255, 255, 0.08); 
            border-radius: 6px;
        }
        
        /* 3. Soft Pulse Effect */
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

        /* ADVANCED TRANSITIONS [3] */
        .page-container[data-transition] {
            transition: all 0.9s cubic-bezier(0.25, 1, 0.5, 1);
            will-change: transform, opacity, clip-path;
        }
        
        html.js-enabled .page-container[data-transition="split-screen"] {
            opacity: 1 !important; transform: none !important; clip-path: none !important;
        }
        
        /* Clip-Path Reveal */
        html.js-enabled .page-container[data-transition="clip-reveal"] { clip-path: circle(0% at 50% 50%); }
        html.js-enabled .page-container[data-transition="clip-reveal"].is-visible { clip-path: circle(150% at 50% 50%); }
        
        /* Vertical Split-Screen Columns */
        html.js-enabled .page-container[data-transition="split-screen"] .left-half {
            transform: translateY(-80px); opacity: 0;
            transition: transform 0.9s cubic-bezier(0.25, 1, 0.5, 1), opacity 0.9s ease-out;
        }
        html.js-enabled .page-container[data-transition="split-screen"].is-visible .left-half { transform: translateY(0); opacity: 1; }
        
        html.js-enabled .page-container[data-transition="split-screen"] .right-half {
            transform: translateY(80px); opacity: 0;
            transition: transform 0.9s cubic-bezier(0.25, 1, 0.5, 1), opacity 0.9s ease-out;
        }
        html.js-enabled .page-container[data-transition="split-screen"].is-visible .right-half { transform: translateY(0); opacity: 1; }
        
        /* Horizontal Scroll Snap */
        html.js-enabled .page-container[data-transition="horizontal-snap"] { transform: translateX(100%); opacity: 0; }
        html.js-enabled .page-container[data-transition="horizontal-snap"].is-visible { transform: translateX(0); opacity: 1; }

        /* Standard scroll transitions */
        html.js-enabled .page-container[data-transition="fade"] { opacity: 0; }
        html.js-enabled .page-container[data-transition="fade"].is-visible { opacity: 1; }
        
        html.js-enabled .page-container[data-transition="slide-up"] { opacity: 0; transform: translateY(60px); }
        html.js-enabled .page-container[data-transition="slide-up"].is-visible { opacity: 1; transform: translateY(0); }
        
        html.js-enabled .page-container[data-transition="zoom-in"] { opacity: 0; transform: scale(0.93); }
        html.js-enabled .page-container[data-transition="zoom-in"].is-visible { opacity: 1; transform: scale(1); }
        
        html.js-enabled .page-container[data-transition="reveal"] { clip-path: inset(100% 0 0 0); }
        html.js-enabled .page-container[data-transition="reveal"].is-visible { clip-path: inset(0 0 0 0); }

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
        
        pages_data = []
        for page_num in range(len(doc)):
            page = doc[page_num]
            page_width = page.rect.width
            page_height = page.rect.height
            aspect_ratio = (page_height / page_width) * 100
            
            # High-density render
            zoom = 2.0
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat)
            png_bytes = pix.tobytes("png")
            
            # Compress to WebP
            img = Image.open(io.BytesIO(png_bytes))
            webp_io = io.BytesIO()
            img.save(webp_io, format="WEBP", quality=85)
            img_base64 = "data:image/webp;base64," + base64.b64encode(webp_io.getvalue()).decode("utf-8")
            
            # Extract text spans [1]
            spans = []
            dict_data = page.get_text("dict")
            for block in dict_data.get("blocks", []):
                if "lines" in block:
                    for line in block["lines"]:
                        for span in line["spans"]:
                            spans.append({
                                "text": span["text"],
                                "bbox": span["bbox"]  # [x0, y0, x1, y1]
                            })
            
            pages_data.append({
                "number": page_num + 1,
                "width": page_width,
                "height": page_height,
                "aspect_ratio": aspect_ratio,
                "image": img_base64,
                "spans": spans
            })
            
        return jsonify({"pages": pages_data})
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
            
            transition_effect = transitions.get(str(p_num))
            transition_attr = f'data-transition="{transition_effect}"' if (transition_effect and not is_multipage_mode) else ''
            
            # Page entrance animations
            animation_style = ""
            left_animation = ""
            right_animation = ""
            if is_multipage_mode and transition_effect:
                if transition_effect == "fade":
                    animation_style = "animation: fadeIn 0.7s ease-out forwards;"
                elif transition_effect in ["slide-up", "sticky-cards"]:
                    animation_style = "animation: slideUpIn 0.7s cubic-bezier(0.25, 1, 0.5, 1) forwards;"
                elif transition_effect == "zoom-in":
                    animation_style = "animation: zoomIn 0.7s cubic-bezier(0.25, 1, 0.5, 1) forwards;"
                elif transition_effect == "reveal":
                    animation_style = "animation: revealIn 0.7s cubic-bezier(0.25, 1, 0.5, 1) forwards;"
                elif transition_effect == "clip-reveal":
                    animation_style = "animation: clipRevealIn 0.9s cubic-bezier(0.25, 1, 0.5, 1) forwards;"
                elif transition_effect == "split-screen":
                    left_animation = "animation: splitLeft 0.8s cubic-bezier(0.25, 1, 0.5, 1) forwards;"
                    right_animation = "animation: splitRight 0.8s cubic-bezier(0.25, 1, 0.5, 1) forwards;"
                elif transition_effect == "horizontal-snap":
                    animation_style = "animation: horizontalSnapIn 0.8s cubic-bezier(0.25, 1, 0.5, 1) forwards;"

            if transition_effect == "split-screen":
                img_elements = f"""
                <div class="left-half" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; overflow: hidden; clip-path: inset(0 50% 0 0); {left_animation}">
                    <img src="{img_base64}" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; display: block;" alt="Page {p_num} Left" />
                </div>
                <div class="right-half" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; overflow: hidden; clip-path: inset(0 0 0 50%); {right_animation}">
                    <img src="{img_base64}" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; display: block;" alt="Page {p_num} Right" />
                </div>
                """
            else:
                img_elements = f'<img src="{img_base64}" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; display: block;" alt="Page {p_num}" />'

            sticky_style = ""
            if transition_effect == "sticky-cards" and not is_multipage_mode:
                sticky_style = f"position: sticky; top: 0; z-index: {p_num}; box-shadow: 0 -15px 35px rgba(0,0,0,0.08);"

            container_style = f"position: relative; width: 100%; max-width: {page_width}px; margin: 0 auto; background: #ffffff; z-index: {p_num}; {sticky_style} {animation_style}"
            
            # Map absolute hyperlinks and customized hover states [2]
            link_overlays = []
            page_links = links_by_page.get(p_num, [])
            for l in page_links:
                bbox = l["bbox"]
                left_pct = (bbox[0] / page_width) * 100
                top_pct = (bbox[1] / page_height) * 100
                width_pct = ((bbox[2] - bbox[0]) / page_width) * 100
                height_pct = ((bbox[3] - bbox[1]) / page_height) * 100
                
                hover_class = f'effect-{l.get("hover_effect", "glow")}'
                
                href = l["href"]
                # Resolve scroll targets on multipage bundles
                if is_multipage_mode and href.startswith("#page-"):
                    try:
                        tgt_num = int(href.split("-")[1])
                        href = get_page_filename(tgt_num, section_pages)
                    except Exception:
                        pass
                
                target_attr = 'target="_blank"' if (not href.startswith("#") and not is_multipage_mode) else ''
                link_overlays.append(f'<a href="{href}" {target_attr} class="pdf-link {hover_class}" style="position: absolute; left: {left_pct}%; top: {top_pct}%; width: {width_pct}%; height: {height_pct}%; z-index: 10;"></a>')
            
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

            return f"""
            <div id="page-{p_num}" class="page-container" {transition_attr} style="{container_style}">
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
    
    <script>
    document.addEventListener("DOMContentLoaded", () => {{
        const observerOptions = {{
            root: null,
            rootMargin: "0px",
            threshold: 0.02
        }};
        const observer = new IntersectionObserver((entries, observer) => {{
            entries.forEach(entry => {{
                if (entry.isIntersecting) {{
                    const target = entry.target;
                    target.classList.add("is-visible");
                    setTimeout(() => {{
                        const leftHalf = target.querySelector('.left-half');
                        const rightHalf = target.querySelector('.right-half');
                        if (leftHalf) leftHalf.style.transform = "none";
                        if (rightHalf) rightHalf.style.transform = "none";
                        target.style.transform = "none";
                        target.style.clipPath = "none";
                        target.style.opacity = "1";
                    }}, 1000);
                    observer.unobserve(target);
                }}
            }});
        }}, observerOptions);
        document.querySelectorAll(".page-container[data-transition]").forEach(page => {{
            observer.observe(page);
        }});
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
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)
