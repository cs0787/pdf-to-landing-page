import os
import re
import base64
from flask import Flask, request, render_template, send_file, redirect, url_for, flash
import fitz  # PyMuPDF
from io import BytesIO

app = Flask(__name__)
app.secret_key = 'pdf_converter_secret_key'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # Limit uploads to 50MB

# Helper to check if two bounding boxes overlap (to avoid duplicating native links)
def rects_overlap(r1, r2):
    return not (r1.x1 <= r2.x0 or r2.x1 <= r1.x0 or r1.y1 <= r2.y0 or r2.y1 <= r1.y0)

# 1. Smart Navigation Router: Index which pages represent which topics
def index_document_sections(doc):
    section_pages = {
        "home": 1,
        "about": 1,
        "services": 1,
        "features": 1,
        "contact": len(doc),  # Default contact to the last page of the site
        "pricing": 1,
        "portfolio": 1,
    }
    
    # Analyze text on pages to find the most relevant section pages
    scores = {key: [0] * len(doc) for key in ["about", "services", "features", "contact", "pricing", "portfolio"]}
    
    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text().lower()
        
        for key in scores.keys():
            count = text.count(key)
            # Give heavier relevance scores to distinct section phrases
            if key == "about" and "about us" in text:
                count += 5
            if key == "contact" and "contact us" in text:
                count += 5
            if key == "services" and "our services" in text:
                count += 5
                
            scores[key][page_num] = count
            
    # Map each section to the highest-scoring page index
    for key, page_scores in scores.items():
        max_score = max(page_scores)
        if max_score > 0:
            best_page = page_scores.index(max_score) + 1  # 1-based index
            section_pages[key] = best_page
            
    return section_pages

# 2. Extract Plain Text URLs and Emails from the Page
def extract_raw_text_urls(page):
    detected = []
    words = page.get_text("words")  # Returns layout (x0, y0, x1, y1, "text", block_no, line_no, word_no)
    
    # Pattern structures for links and emails
    url_pattern = re.compile(
        r'^(https?://)?(www\.)?([a-zA-Z0-9\-]+\.)+[a-zA-Z]{2,6}(/[a-zA-Z0-9\-._~:/?#\[\]@!$&\'()*+,;=]*)?$',
        re.IGNORECASE
    )
    email_pattern = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,6}$', re.IGNORECASE)
    
    for w in words:
        x0, y0, x1, y1, text, _, _, _ = w
        clean_text = text.strip().strip(",.()[]{}:;\"'!?*")
        
        if len(clean_text) < 4:
            continue
            
        # Match against web addresses
        if url_pattern.match(clean_text) or clean_text.startswith("www.") or clean_text.startswith("http"):
            href = clean_text
            if not href.startswith("http://") and not href.startswith("https://"):
                href = "https://" + href
            detected.append({
                "rect": fitz.Rect(x0, y0, x1, y1),
                "href": href,
                "target": 'target="_blank"'
            })
        # Match against emails
        elif email_pattern.match(clean_text) or ("@" in clean_text and "." in clean_text):
            detected.append({
                "rect": fitz.Rect(x0, y0, x1, y1),
                "href": f"mailto:{clean_text}",
                "target": ""
            })
            
    return detected

# 3. Detect Menu/Button Navigation Labels on the page
def detect_button_phrases(page, section_pages):
    detected = []
    current_page = page.number + 1
    
    # Layout Navigation Triggers -> mapped targets
    nav_triggers = {
        "home": ["home", "welcome"],
        "about": ["about us", "about", "who we are", "our story"],
        "services": ["services", "what we do", "our services", "features"],
        "pricing": ["pricing", "plans", "pricing plans"],
        "portfolio": ["portfolio", "our work", "projects"],
        "contact": ["contact us", "contact", "get in touch", "email us"]
    }
    
    for section_name, phrases in nav_triggers.items():
        target_page = section_pages.get(section_name)
        if not target_page:
            continue
            
        # Avoid linking pages to themselves (e.g. "About" heading on Page 2 shouldn't navigate to Page 2)
        # Exception is Page 1, which typically contains header navigation menus
        if current_page == target_page and current_page != 1:
            continue
            
        for phrase in phrases:
            matches = page.search_for(phrase)  # Case-insensitive by default
            for rect in matches:
                detected.append({
                    "rect": rect,
                    "href": f"#page-{target_page}",
                    "target": ""
                })
                
    return detected


def convert_pdf_to_html(pdf_bytes, zoom_factor=2.0):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages_html = []
    
    # Pre-index navigation sections across the entire PDF
    section_pages = index_document_sections(doc)
    
    for page_num in range(len(doc)):
        page = doc[page_num]
        page_width = page.rect.width
        page_height = page.rect.height
        aspect_ratio = (page_height / page_width) * 100
        
        # 1. Collect Manual/Native PDF Links first (highest priority)
        existing_links = page.get_links()
        active_links = []
        existing_rects = []
        
        for link in existing_links:
            href = ""
            target = ""
            if "uri" in link:
                href = link["uri"]
                target = 'target="_blank"'
            elif "page" in link:
                target_page_num = link["page"] + 1
                href = f"#page-{target_page_num}"
                target = ""
                
            if href:
                rect = link["from"]
                existing_rects.append(rect)
                active_links.append({
                    "rect": rect,
                    "href": href,
                    "target": target
                })
                
        # 2. Auto-Detect Plain Text URLs / Emails
        raw_urls = extract_raw_text_urls(page)
        for url_link in raw_urls:
            # Only add if it doesn't collide with existing manual links
            if not any(rects_overlap(url_link["rect"], r) for r in existing_rects):
                active_links.append(url_link)
                existing_rects.append(url_link["rect"])
                
        # 3. Auto-Detect "Button" Phrases (like Home, Contact Us, About Us)
        button_links = detect_button_phrases(page, section_pages)
        for btn in button_links:
            # Only add if it doesn't collide with existing links or URLs
            if not any(rects_overlap(btn["rect"], r) for r in existing_rects):
                active_links.append(btn)
                existing_rects.append(btn["rect"])

        # Compile CSS percentage overlays for all finalized links
        link_overlays = []
        for l in active_links:
            rect = l["rect"]
            left_pct = (rect.x0 / page_width) * 100
            top_pct = (rect.y0 / page_height) * 100
            width_pct = ((rect.x1 - rect.x0) / page_width) * 100
            height_pct = ((rect.y1 - rect.y0) / page_height) * 100
            
            link_overlays.append(f"""
                <a href="{l['href']}" {l['target']} class="pdf-link" style="
                    position: absolute;
                    left: {left_pct}%;
                    top: {top_pct}%;
                    width: {width_pct}%;
                    height: {height_pct}%;
                    z-index: 10;
                " title="{l['href']}"></a>
            """)
        
        links_html = "\n".join(link_overlays)
        
        # Render high-resolution page layout
        mat = fitz.Matrix(zoom_factor, zoom_factor)
        pix = page.get_pixmap(matrix=mat)
        img_data = pix.tobytes("png")
        img_base64 = base64.b64encode(img_data).decode("utf-8")
        
        pages_html.append(f"""
        <div id="page-{page_num + 1}" class="page-container" style="position: relative; width: 100%; max-width: {page_width}px; margin: 0 auto; background: #ffffff;">
            <div style="padding-top: {aspect_ratio}%;"></div>
            <img src="data:image/png;base64,{img_base64}" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; display: block;" alt="Page {page_num + 1}" />
            <div class="links-layer" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%;">
                {links_html}
            </div>
        </div>
        """)
        
    full_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>My Smart Static Website</title>
    <style>
        html {{
            scroll-behavior: smooth;
        }}
        body {{
            margin: 0;
            padding: 0;
            background-color: #ffffff;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            overflow-x: hidden;
        }}
        .page-container {{
            border-radius: 0;
            box-shadow: none;
        }}
        .pdf-link {{
            cursor: pointer;
            text-decoration: none;
            transition: background-color 0.15s ease-in-out;
        }}
        .pdf-link:hover {{
            background-color: rgba(59, 130, 246, 0.15); /* Modern soft blue hover box */
            outline: 1.5px dashed rgba(59, 130, 246, 0.6);
        }}
    </style>
</head>
<body>
    <main style="width: 100%; max-width: 100%; box-sizing: border-box;">
        {"".join(pages_html)}
    </main>
</body>
</html>
"""
    return full_html

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        if 'pdf_file' not in request.files:
            flash('No file was uploaded.')
            return redirect(request.url)
        file = request.files['pdf_file']
        if file.filename == '':
            flash('No selected file')
            return redirect(request.url)
        
        if file and file.filename.lower().endswith('.pdf'):
            try:
                zoom = float(request.form.get('zoom', 2.0))
                pdf_bytes = file.read()
                html_code = convert_pdf_to_html(pdf_bytes, zoom_factor=zoom)
                
                return send_file(
                    BytesIO(html_code.encode('utf-8')),
                    mimetype='text/html',
                    as_attachment=True,
                    download_name=f"{os.path.splitext(file.filename)[0]}.html"
                )
            except Exception as e:
                flash(f"Error converting file: {str(e)}")
                return redirect(request.url)
                
    return render_template('index.html')

if __name__ == '__main__':
    app.run(debug=True, port=5000)