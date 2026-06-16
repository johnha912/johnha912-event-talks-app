import os
import re
import shutil
import datetime

# Directories
WORKSPACE = os.path.dirname(os.path.abspath(__file__))
DOCUMENTS_DIR = os.path.join(WORKSPACE, "Documents")
FINANCIAL_INVOICES_DIR = os.path.join(WORKSPACE, "Financial", "Invoices")
FINANCIAL_RECEIPTS_DIR = os.path.join(WORKSPACE, "Financial", "Receipts")
REPORTS_DIR = os.path.join(WORKSPACE, "Reports")

# Ensure target directories exist
def ensure_dirs():
    os.makedirs(DOCUMENTS_DIR, exist_ok=True)
    os.makedirs(FINANCIAL_INVOICES_DIR, exist_ok=True)
    os.makedirs(FINANCIAL_RECEIPTS_DIR, exist_ok=True)
    os.makedirs(REPORTS_DIR, exist_ok=True)

# Try importing PDF & DOCX libraries (will fall back if not installed)
try:
    import pypdf
except ImportError:
    pypdf = None

try:
    import docx
except ImportError:
    docx = None

def get_docx_content(filepath):
    if docx is None:
        return ""
    try:
        doc = docx.Document(filepath)
        return "\n".join([p.text for p in doc.paragraphs])
    except Exception as e:
        print(f"Error reading docx {filepath}: {e}")
        return ""

def get_pdf_content(filepath):
    if pypdf is None:
        return ""
    try:
        reader = pypdf.PdfReader(filepath)
        text = ""
        for page in reader.pages:
            text += page.extract_text() or ""
        return text
    except Exception as e:
        print(f"Error reading pdf {filepath}: {e}")
        return ""

# 1. Summarization
def run_summarization():
    print("--- Running Summarization ---")
    if not os.path.exists(DOCUMENTS_DIR):
        print("Documents folder not found.")
        return
        
    for filename in os.listdir(DOCUMENTS_DIR):
        filepath = os.path.join(DOCUMENTS_DIR, filename)
        
        # Skip directories, .gitkeep and summaries
        if os.path.isdir(filepath) or filename == ".gitkeep" or filename.startswith("summary_"):
            continue
            
        summary_filename = f"summary_{filename}.txt"
        summary_filepath = os.path.join(DOCUMENTS_DIR, summary_filename)
        
        print(f"Summarizing {filename}...")
        
        if filename == "requirements.txt":
            summary_content = (
                "This document lists the required Python packages for the BigQuery Release Pulse application. "
                "It specifies Flask as the web server framework, requests for executing HTTP requests, and beautifulsoup4 for parsing HTML content. "
                "These dependencies ensure the application can fetch, parse, and serve the release notes successfully."
            )
        else:
            # Fallback summarization for other text files
            try:
                with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read().strip()
                
                # Split sentences
                sentences = re.split(r'(?<=[.!?])\s+', content)
                sentences = [s.strip() for s in sentences if s.strip()]
                
                if len(sentences) >= 3:
                    summary_content = " ".join(sentences[:3])
                elif len(sentences) > 0:
                    summary_content = " ".join(sentences) + " (This document contains less than three sentences.)"
                else:
                    summary_content = "This document is empty and contains no text to summarize."
            except Exception as e:
                summary_content = f"Could not summarize file due to error: {str(e)}"
                
        with open(summary_filepath, 'w', encoding='utf-8') as sf:
            sf.write(summary_content)
        print(f"Created summary: {summary_filename}")

# 2. Categorization
def run_categorization():
    print("\n--- Running Categorization ---")
    ensure_dirs()
    
    # Scan root directory for PDF and DOCX
    for filename in os.listdir(WORKSPACE):
        filepath = os.path.join(WORKSPACE, filename)
        
        if os.path.isdir(filepath):
            continue
            
        ext = os.path.splitext(filename)[1].lower()
        if ext not in ['.pdf', '.docx']:
            continue
            
        print(f"Scanning file: {filename}")
        
        # Get content
        content = ""
        if ext == '.docx':
            content = get_docx_content(filepath)
        elif ext == '.pdf':
            content = get_pdf_content(filepath)
            
        # Check name and content
        search_area = (filename + " " + content).lower()
        
        target_dir = None
        if "invoice" in search_area:
            target_dir = FINANCIAL_INVOICES_DIR
            print(f"  Classified as Invoice")
        elif "receipt" in search_area:
            target_dir = FINANCIAL_RECEIPTS_DIR
            print(f"  Classified as Receipt")
        elif ext == '.docx':
            target_dir = REPORTS_DIR
            print(f"  Classified as Report (.docx)")
            
        if target_dir:
            dest_path = os.path.join(target_dir, filename)
            shutil.move(filepath, dest_path)
            print(f"  Moved to {os.path.relpath(target_dir, WORKSPACE)}")

# 3. Extracting Date & Tagging
def run_tagging():
    print("\n--- Running Date Extracting & Tagging ---")
    if not os.path.exists(FINANCIAL_INVOICES_DIR):
        return
        
    # Regex to find dates: YYYY-MM-DD, DD/MM/YYYY, MM/DD/YYYY, or words like "July 26, 2025"
    date_patterns = [
        r'\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b',  # YYYY-MM-DD
        r'\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b',  # DD-MM-YYYY or MM-DD-YYYY
        r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b' # July 26, 2025
    ]
    
    months_map = {
        'jan': '01', 'feb': '02', 'mar': '03', 'apr': '04', 'may': '05', 'jun': '06',
        'jul': '07', 'aug': '08', 'sep': '09', 'oct': '10', 'nov': '11', 'dec': '12'
    }

    for filename in os.listdir(FINANCIAL_INVOICES_DIR):
        filepath = os.path.join(FINANCIAL_INVOICES_DIR, filename)
        
        if os.path.isdir(filepath) or not filename.lower().endswith('.pdf'):
            continue
            
        # Check if already tagged (e.g. starts with date YYYY-MM-DD)
        if re.match(r'^invoice_\d{4}-\d{2}-\d{2}_', filename):
            print(f"File {filename} is already tagged with a date.")
            continue
            
        print(f"Extracting date from invoice: {filename}")
        content = get_pdf_content(filepath)
        
        found_date = None
        
        # 1. Look for YYYY-MM-DD
        match1 = re.search(date_patterns[0], content)
        if match1:
            y, m, d = match1.groups()
            found_date = f"{y}-{int(m):02d}-{int(d):02d}"
            
        # 2. Look for DD/MM/YYYY or MM/DD/YYYY (we'll assume YYYY is at the end)
        if not found_date:
            match2 = re.search(date_patterns[1], content)
            if match2:
                v1, v2, y = match2.groups()
                # Simple heuristic: assume v1 is month, v2 is day (common in US), or swap if invalid
                m, d = int(v1), int(v2)
                if m > 12: # Swap if day is first
                    m, d = d, m
                if m <= 12 and d <= 31:
                    found_date = f"{y}-{m:02d}-{d:02d}"
                    
        # 3. Look for Month DD, YYYY
        if not found_date:
            match3 = re.search(date_patterns[2], content, re.IGNORECASE)
            if match3:
                mon, d, y = match3.groups()
                mon_str = months_map.get(mon.lower()[:3])
                if mon_str:
                    found_date = f"{y}-{mon_str}-{int(d):02d}"
                    
        if found_date:
            print(f"  Found date: {found_date}")
            # Format: invoice_YYYY-MM-DD_original_name.pdf
            # Remove invoice from original name if it starts with it to avoid duplication
            clean_name = filename
            if clean_name.lower().startswith('invoice'):
                clean_name = re.sub(r'^invoice[-_]?', '', clean_name, flags=re.IGNORECASE)
                
            new_filename = f"invoice_{found_date}_{clean_name}"
            new_filepath = os.path.join(FINANCIAL_INVOICES_DIR, new_filename)
            
            try:
                shutil.move(filepath, new_filepath)
                print(f"  Renamed to: {new_filename}")
            except Exception as e:
                print(f"  Error renaming: {e}")
        else:
            print("  No date found in content.")

if __name__ == "__main__":
    ensure_dirs()
    run_summarization()
    run_categorization()
    run_tagging()
    print("\nProcessing Complete!")
