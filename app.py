import datetime
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import requests
import streamlit as st
from fpdf import FPDF
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

# --- ตั้งค่าหน้าเว็บ ---
st.set_page_config(page_title="AI Study & Exam Assistant", page_icon="🎓", layout="wide")

# --- ดึงค่า Secrets และรายชื่อผู้ใช้ ---
def get_config():
    gemini_key = st.secrets.get("GEMINI_API_KEY", os.environ.get("GEMINI_API_KEY", ""))
    if "users" in st.secrets:
        users_dict = dict(st.secrets["users"])
    else:
        users_dict = {
            "admin": "admin1234",
            "user1": "user1111",
            "user2": "user2222"
        }
    return gemini_key, users_dict

DEFAULT_GEMINI_KEY, USERS_DB = get_config()

# --- ระบบ Login แยกบัญชีผู้ใช้ ---
if "username" not in st.session_state:
    st.session_state["username"] = None
if "role" not in st.session_state:
    st.session_state["role"] = None

def login_screen():
    col1, col2, col3 = st.columns([1, 1.5, 1])
    with col2:
        st.markdown("<br><br>", unsafe_allow_html=True)
        with st.container(border=True):
            st.title("🔐 เข้าสู่ระบบ")
            st.caption("AI Study & Exam Assistant — กรุณากรอกชื่อผู้ใช้และรหัสผ่าน")
            uname = st.text_input("ชื่อผู้ใช้ (Username)", placeholder="เช่น admin, user1, user2").strip()
            pwd = st.text_input("รหัสผ่าน (Password)", type="password")
            if st.button("เข้าสู่ระบบ", type="primary", use_container_width=True):
                if uname in USERS_DB and str(USERS_DB[uname]) == pwd:
                    st.session_state["username"] = uname
                    st.session_state["role"] = "admin" if uname.lower() == "admin" else "user"
                    st.rerun()
                else:
                    st.error("❌ ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง")

if st.session_state["username"] is None:
    login_screen()
    st.stop()

CURRENT_USER = st.session_state["username"]
IS_ADMIN = (st.session_state["role"] == "admin")

# --- จัดการฐานข้อมูล SQLite (ข้อสอบ + บันทึกการใช้ Token) ---
DB_FILE = "exam_solutions_v3.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_username TEXT,
            file_hash TEXT,
            file_name TEXT,
            chapter TEXT,
            question_number TEXT,
            question_text TEXT,
            solution_steps TEXT,
            final_answer TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS token_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            log_date TEXT,
            username TEXT,
            model_name TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            total_tokens INTEGER
        )
    """)
    conn.commit()
    conn.close()

def log_token_usage(username: str, model_name: str, usage_meta):
    if not usage_meta:
        return
    in_tok = getattr(usage_meta, "prompt_token_count", 0) or 0
    out_tok = (getattr(usage_meta, "candidates_token_count", 0) or 0) + (getattr(usage_meta, "thoughts_token_count", 0) or 0)
    tot_tok = getattr(usage_meta, "total_token_count", 0) or (in_tok + out_tok)
    today_str = datetime.date.today().isoformat()

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        INSERT INTO token_logs (log_date, username, model_name, input_tokens, output_tokens, total_tokens)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (today_str, username, model_name, in_tok, out_tok, tot_tok))
    conn.commit()
    conn.close()

def get_today_token_stats(username: str, is_admin: bool):
    today_str = datetime.date.today().isoformat()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # ยอดรวมทั้งระบบวันนี้ (ใช้เช็กโควตา API Key กลาง)
    c.execute("SELECT COALESCE(SUM(total_tokens), 0), COUNT(*) FROM token_logs WHERE log_date = ?", (today_str,))
    global_tokens, global_reqs = c.fetchone()

    # ยอดเฉพาะของ user คนนี้วันนี้
    c.execute("SELECT COALESCE(SUM(total_tokens), 0), COUNT(*) FROM token_logs WHERE log_date = ? AND username = ?", (today_str, username))
    user_tokens, user_reqs = c.fetchone()
    conn.close()
    return global_tokens, global_reqs, user_tokens, user_reqs

def reset_today_tokens():
    today_str = datetime.date.today().isoformat()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM token_logs WHERE log_date = ?", (today_str,))
    conn.commit()
    conn.close()

def save_questions(owner_username, file_hash, file_name, items):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM questions WHERE owner_username = ? AND file_hash = ?", (owner_username, file_hash))
    for item in items:
        c.execute("""
            INSERT INTO questions (owner_username, file_hash, file_name, chapter, question_number, question_text, solution_steps, final_answer)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            owner_username, file_hash, file_name,
            item["chapter"], item["question_number"],
            item["question_text"], item["solution_steps"], item["final_answer"]
        ))
    conn.commit()
    conn.close()

def delete_file_questions(owner_username, file_name):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM questions WHERE owner_username = ? AND file_name = ?", (owner_username, file_name))
    conn.commit()
    conn.close()

def load_questions_for_user(username: str, is_admin: bool):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if is_admin:
        c.execute("SELECT * FROM questions ORDER BY owner_username, chapter, file_name, id")
    else:
        c.execute("SELECT * FROM questions WHERE owner_username = ? ORDER BY chapter, file_name, id", (username,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

init_db()

# --- ดาวน์โหลดไฟล์เดี่ยวจากลิงก์ Google Drive ---
def download_public_drive_file(drive_url: str):
    match = re.search(r"/d/([a-zA-Z0-9_-]+)", drive_url)
    if not match:
        match = re.search(r"id=([a-zA-Z0-9_-]+)", drive_url)
    if not match:
        raise Exception("รูปแบบลิงก์ไม่ถูกต้อง กรุณาวางลิงก์ไฟล์ Google Drive (เช่น https://drive.google.com/file/d/xxxx/view)")

    file_id = match.group(1)
    download_url = f"https://drive.google.com/uc?export=download&id={file_id}"
    session = requests.Session()
    res = session.get(download_url, timeout=60)

    for key, value in res.cookies.items():
        if key.startswith("download_warning"):
            res = session.get(f"{download_url}&confirm={value}", timeout=60)
            break

    content_type = res.headers.get("Content-Type", "")
    if "text/html" in content_type:
        raise Exception("ไม่สามารถดาวน์โหลดไฟล์ได้ (กรุณาเช็กว่าตั้งค่าแชร์ไฟล์เป็น 'ทุกคนที่มีลิงก์' และไม่ได้ปิดการดาวน์โหลด)")

    cd = res.headers.get("Content-Disposition", "")
    fname_match = re.search(r'filename="?([^";]+)"?', cd)
    file_name = fname_match.group(1) if fname_match else f"Drive_Exam_{file_id[:6]}.pdf"

    mime_type = "application/pdf"
    if "image/png" in content_type or file_name.lower().endswith(".png"):
        mime_type = "image/png"
    elif "image/jpeg" in content_type or file_name.lower().endswith((".jpg", ".jpeg")):
        mime_type = "image/jpeg"

    return res.content, mime_type, file_name

# --- โครงสร้างข้อมูล JSON สำหรับ Gemini ---
class SolvedQuestion(BaseModel):
    chapter: str = Field(description="ชื่อบทเรียนหรือหัวข้อหลักของข้อสอบข้อนี้ เช่น บทที่ 1: Circuit Analysis")
    question_number: str = Field(description="เลขข้อ เช่น ข้อ 1 หรือ 1.2")
    question_text: str = Field(description="สรุปโจทย์หรือข้อความโจทย์ทั้งหมด (ใช้ LaTeX $...$ สำหรับสมการ)")
    solution_steps: str = Field(description="วิธีทำและอธิบายแนวคิดทีละขั้นตอนอย่างละเอียด (ใช้ LaTeX $...$ หรือ $$...$$)")
    final_answer: str = Field(description="คำตอบสุดท้ายที่ชัดเจน")

class ExamExtraction(BaseModel):
    questions: list[SolvedQuestion]

class SlideOrSection(BaseModel):
    title: str = Field(description="หัวข้อของสไลด์หรือหัวข้อหลักของเนื้อหา")
    bullets: list[str] = Field(description="ประเด็นสำคัญ เนื้อหาสรุป สูตร หรือคำอธิบายเป็นข้อๆ")
    highlight_note: str = Field(description="ข้อควรจำ สูตรสำคัญ หรือจุดที่มักออกสอบ (ถ้าไม่มีให้เว้นว่าง)")

class StudySummary(BaseModel):
    doc_title: str = Field(description="ชื่อเรื่องหลักของเอกสารสรุป")
    subtitle: str = Field(description="คำอธิบายสั้นๆ หรือรายวิชา")
    sections: list[SlideOrSection]

# --- ฟังก์ชันเชื่อมต่อ Gemini (v1beta) ---
def get_gemini_client(api_key: str):
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(api_version="v1beta")
    )

@st.cache_data(ttl=3600)
def fetch_available_models(api_key: str):
    fallback = ["gemini-2.5-pro", "gemini-2.5-flash"]
    if not api_key:
        return fallback
    try:
        client = get_gemini_client(api_key)
        models = []
        excluded_keywords = ("embedding", "imagen", "veo", "tts", "audio", "robotics", "computer-use")
        for m in client.models.list():
            actions = getattr(m, "supported_actions", None) or getattr(m, "supported_generation_methods", None) or []
            if "generateContent" in actions:
                name = m.name.replace("models/", "")
                if "gemini" in name and not any(k in name for k in excluded_keywords):
                    models.append(name)
        if not models:
            return fallback
        models.sort(reverse=True)
        return models
    except Exception:
        return fallback

def get_model_daily_limits(model_name: str):
    """ประมาณการลิมิต Free Tier ต่อวัน (Tokens/วัน และ Requests/วัน) ตามรุ่นโมเดล"""
    m = model_name.lower()
    if "pro" in m:
        return 250_000, 50      # รุ่น Pro โควตาต่อวันน้อยกว่า (~250k tokens / 50 reqs)
    elif "lite" in m:
        return 1_500_000, 1000  # รุ่น Flash-Lite โควตาเยอะสุด
    else:
        return 1_000_000, 250   # รุ่น Flash มาตรฐาน (~1M tokens / 250 reqs)

def analyze_exam_with_gemini(file_bytes: bytes, mime_type: str, file_name: str, gemini_key: str, model_name: str, use_thinking: bool, username: str):
    client = get_gemini_client(gemini_key)
    prompt = f"""
    คุณคืออาจารย์และผู้เชี่ยวชาญด้านการเฉลยข้อสอบ 
    จงอ่านข้อสอบทั้งหมดในไฟล์ '{file_name}' นี้ แล้วดำเนินการดังนี้:
    1. แยกข้อสอบออกเป็นรายข้อให้ครบทุกข้อ
    2. วิเคราะห์เนื้อหาของแต่ละข้อ แล้วจัดหมวดหมู่ว่าอยู่ 'บทที่เท่าไหร่ / เรื่องอะไร' (ตั้งชื่อบทให้เป็นมาตรฐานเดียวกันหากเป็นเรื่องเดียวกัน)
    3. แสดงโจทย์ วิธีทำอย่างละเอียดทีละขั้นตอน ตรวจสอบความถูกต้องของการคำนวณทุกบรรทัด และสรุปคำตอบสุดท้ายเป็นภาษาไทย
    4. ใช้รูปแบบ LaTeX ($...$ สำหรับในบรรทัด และ $$...$$ สำหรับสมการแยกบรรทัด) ให้ถูกต้องชัดเจน
    """
    config_args = {
        "response_mime_type": "application/json",
        "response_schema": ExamExtraction,
        "temperature": 0.1
    }
    if use_thinking:
        config_args["thinking_config"] = types.ThinkingConfig(thinking_budget=4096)

    response = client.models.generate_content(
        model=model_name,
        contents=[types.Part.from_bytes(data=file_bytes, mime_type=mime_type), prompt],
        config=types.GenerateContentConfig(**config_args)
    )
    log_token_usage(username, model_name, getattr(response, "usage_metadata", None))
    return json.loads(response.text).get("questions", [])

def summarize_file_with_gemini(file_bytes: bytes, file_name: str, custom_prompt: str, output_format: str, summary_lang: str, gemini_key: str, model_name: str, use_thinking: bool, username: str, cancel_event: threading.Event):
    client = get_gemini_client(gemini_key)

    with tempfile.NamedTemporaryFile(delete=False, suffix=f"_{file_name}") as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    try:
        if cancel_event.is_set():
            return None

        gemini_file = client.files.upload(file=tmp_path)
        while gemini_file.state.name == "PROCESSING":
            if cancel_event.is_set():
                client.files.delete(name=gemini_file.name)
                return None
            time.sleep(2)
            gemini_file = client.files.get(name=gemini_file.name)

        if cancel_event.is_set():
            client.files.delete(name=gemini_file.name)
            return None

        style_instruction = (
            "จัดโครงสร้างเป็นสไลด์นำเสนอ (Presentation Slides) แบ่งเป็นสไลด์ละ 1 หัวข้อย่อย เนื้อหากระชับ อ่านง่าย เป็น Bullet points ชัดเจน ไม่ยาวล้นหน้า"
            if output_format == "สไลด์ PDF (แนวนอน 16:9)"
            else "จัดโครงสร้างเป็นเอกสารสรุปเนื้อหา (Study Note / Handout) อธิบายละเอียด เป็นลำดับขั้นตอน มีตัวอย่างและสูตรครบถ้วน"
        )

        if summary_lang == "ภาษาไทย (Thai)":
            lang_instruction = "เขียนสรุปทั้งหมดเป็นภาษาไทยที่สละสลวย เข้าใจง่าย"
        elif summary_lang == "ภาษาอังกฤษ (English)":
            lang_instruction = "Write the entire summary strictly in clear, academic English."
        else:
            lang_instruction = "เขียนคำอธิบายหลักเป็นภาษาไทย โดยคงคำศัพท์เฉพาะทางเทคนิค/วิศวกรรม (Technical Terms) เป็นภาษาอังกฤษกำกับไว้ในวงเล็บ"

        prompt = f"""
        คุณคือผู้เชี่ยวชาญด้านการสรุปเนื้อหาวิชาการและจัดทำสื่อการเรียนการสอน
        จงวิเคราะห์ไฟล์ที่แนบมานี้ แล้วสรุปเนื้อหาออกมาตามคำสั่งดังนี้:
        - ภาษาที่ใช้สรุป: {lang_instruction}
        - รูปแบบเอกสาร: {style_instruction}
        - ความต้องการเฉพาะของผู้ใช้: {custom_prompt if custom_prompt.strip() else "สรุปเนื้อหาสำคัญทั้งหมดให้ครบถ้วน เข้าใจง่าย เหมาะสำหรับอ่านทบทวนก่อนสอบ"}
        - หมายเหตุ: สำหรับสมการหรือสัญลักษณ์ทางคณิตศาสตร์/วิศวกรรม ให้เขียนในรูปแบบข้อความธรรมดาที่อ่านง่าย (เช่น V = I * R, x^2, sqrt(x)) เพื่อให้แสดงผลในไฟล์ PDF ได้สวยงามโดยไม่ติดโค้ด LaTeX ดิบ
        """

        config_args = {
            "response_mime_type": "application/json",
            "response_schema": StudySummary,
            "temperature": 0.2
        }
        if use_thinking:
            config_args["thinking_config"] = types.ThinkingConfig(thinking_budget=4096)

        response = client.models.generate_content(
            model=model_name,
            contents=[gemini_file, prompt],
            config=types.GenerateContentConfig(**config_args)
        )
        log_token_usage(username, model_name, getattr(response, "usage_metadata", None))
        client.files.delete(name=gemini_file.name)

        if cancel_event.is_set():
            return None
        return json.loads(response.text)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

# --- ฟังก์ชันสร้างไฟล์ PDF ---
def find_thai_font():
    candidates = [
        "/usr/share/fonts/truetype/tlwg/Garuda.ttf",
        "/usr/share/fonts/truetype/tlwg/Waree.ttf",
        "/usr/share/fonts/truetype/tlwg/Loma.ttf",
        "C:/Windows/Fonts/tahoma.ttf",
        "/System/Library/Fonts/Supplemental/Thonburi.ttc"
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None

def build_pdf_bytes(summary_data: dict, is_slide: bool, summary_lang: str) -> bytes:
    font_path = find_thai_font()
    if is_slide:
        pdf = FPDF(orientation="L", unit="mm", format=(190, 338))
    else:
        pdf = FPDF(orientation="P", unit="mm", format="A4")

    pdf.set_auto_page_break(auto=True, margin=15)

    if font_path:
        pdf.add_font("UniFont", "", font_path)
        pdf.add_font("UniFont", "B", font_path)
        font_family = "UniFont"
    else:
        font_family = "Helvetica"

    note_prefix = "Key Note:" if summary_lang == "ภาษาอังกฤษ (English)" else "ข้อควรจำ / Note:"

    pdf.add_page()
    pdf.set_fill_color(24, 49, 83)
    if is_slide:
        pdf.rect(0, 0, 338, 190, style="F")
        pdf.set_text_color(255, 255, 255)
        pdf.set_y(65)
        pdf.set_font(font_family, "B", 28)
        pdf.multi_cell(0, 14, summary_data.get("doc_title", "Study Summary"), align="C")
        pdf.ln(6)
        pdf.set_font(font_family, "", 16)
        pdf.set_text_color(200, 220, 255)
        pdf.multi_cell(0, 10, summary_data.get("subtitle", ""), align="C")
    else:
        pdf.rect(0, 0, 210, 45, style="F")
        pdf.set_text_color(255, 255, 255)
        pdf.set_y(14)
        pdf.set_font(font_family, "B", 20)
        pdf.multi_cell(0, 10, summary_data.get("doc_title", "Study Summary"), align="L")
        pdf.set_font(font_family, "", 12)
        pdf.multi_cell(0, 7, summary_data.get("subtitle", ""), align="L")
        pdf.set_y(55)

    sections = summary_data.get("sections", [])
    for idx, sec in enumerate(sections, start=1):
        if is_slide:
            pdf.add_page()
            pdf.set_fill_color(24, 49, 83)
            pdf.rect(0, 0, 338, 26, style="F")
            pdf.set_text_color(255, 255, 255)
            pdf.set_xy(15, 6)
            pdf.set_font(font_family, "B", 18)
            pdf.cell(0, 14, f"{idx}. {sec.get('title', '')}")

            pdf.set_xy(18, 34)
            pdf.set_text_color(30, 30, 30)
            pdf.set_font(font_family, "", 15)
            for b in sec.get("bullets", []):
                pdf.set_x(18)
                pdf.multi_cell(302, 9, f"•  {b}")
                pdf.ln(2)

            if sec.get("highlight_note"):
                pdf.ln(3)
                pdf.set_x(18)
                pdf.set_fill_color(255, 248, 220)
                pdf.set_text_color(130, 90, 0)
                pdf.set_font(font_family, "B", 14)
                pdf.multi_cell(302, 9, f"{note_prefix} {sec['highlight_note']}", fill=True)
        else:
            pdf.ln(4)
            pdf.set_text_color(24, 49, 83)
            pdf.set_font(font_family, "B", 15)
            pdf.multi_cell(0, 9, f"{idx}. {sec.get('title', '')}")
            pdf.set_draw_color(200, 210, 225)
            pdf.line(10, pdf.get_y(), 200, pdf.get_y())
            pdf.ln(2)

            pdf.set_text_color(30, 30, 30)
            pdf.set_font(font_family, "", 12)
            for b in sec.get("bullets", []):
                pdf.set_x(14)
                pdf.multi_cell(185, 7, f"•  {b}")
                pdf.ln(1)

            if sec.get("highlight_note"):
                pdf.ln(2)
                pdf.set_x(14)
                pdf.set_fill_color(240, 245, 255)
                pdf.set_text_color(24, 49, 83)
                pdf.set_font(font_family, "B", 11)
                pdf.multi_cell(185, 7, f"{note_prefix} {sec['highlight_note']}", fill=True)
            pdf.ln(3)

    return bytes(pdf.output())

# --- แถบด้านข้าง (Sidebar) ---
with st.sidebar:
    role_label = "👑 Admin (ดูได้ทุกคน)" if IS_ADMIN else f"👤 ผู้ใช้: {CURRENT_USER}"
    st.success(f"เข้าสู่ระบบ: **{role_label}**")
    if st.button("🚪 ออกจากระบบ (Logout)", use_container_width=True):
        st.session_state["username"] = None
        st.session_state["role"] = None
        st.rerun()

    st.divider()

    if IS_ADMIN:
        with st.expander("🔑 Gemini API Key (เฉพาะ Admin)", expanded=not bool(DEFAULT_GEMINI_KEY)):
            gemini_key = st.text_input("Gemini API Key", value=DEFAULT_GEMINI_KEY, type="password")
    else:
        gemini_key = DEFAULT_GEMINI_KEY

    st.subheader("🧠 เลือกโมเดล AI")
    available_models = fetch_available_models(gemini_key)
    selected_model = st.selectbox(
        "เลือกรุ่นโมเดลที่ใช้ได้",
        options=available_models,
        index=0
    )
    if st.button("🔄 รีเฟรชรายชื่อโมเดล", use_container_width=True):
        fetch_available_models.clear()
        st.rerun()

    use_thinking = st.checkbox(
        "เปิดโหมดคิดวิเคราะห์ลึก (Thinking)",
        value=True,
        help="ใช้ได้กับโมเดลตระกูล 2.5 และ 3.x (หากเลือกรุ่นเก่าที่ไม่รองรับ Thinking ให้ติ๊กออก)"
    )

    # --- แถบแสดงลิมิตการใช้ Token ประจำวัน ---
    st.divider()
    st.subheader("📊 โควตา Token วันนี้ (Free Tier)")
    g_tokens, g_reqs, u_tokens, u_reqs = get_today_token_stats(CURRENT_USER, IS_ADMIN)
    max_tokens, max_reqs = get_model_daily_limits(selected_model)

    ratio_tok = min(g_tokens / max_tokens, 1.0)
    pct_tok = (g_tokens / max_tokens) * 100

    st.progress(ratio_tok)
    if pct_tok >= 90:
        st.error(f"🚨 ใกล้เต็มแล้ว! ใช้ไป **{pct_tok:.1f}%** ({g_tokens:,} / {max_tokens:,} tokens)")
    elif pct_tok >= 70:
        st.warning(f"⚠️ ใช้ไป **{pct_tok:.1f}%** ({g_tokens:,} / {max_tokens:,} tokens)")
    else:
        st.caption(f"รวมทั้งระบบ: **{g_tokens:,}** / {max_tokens:,} tokens ({pct_tok:.1f}%)")

    st.caption(f"คำขอวันนี้: **{g_reqs}** / {max_reqs} ครั้ง (ของคุณ: {u_tokens:,} tokens)")

    if IS_ADMIN:
        if st.button("🧹 รีเซ็ตตัวนับ Token วันนี้", use_container_width=True):
            reset_today_tokens()
            st.rerun()

# --- พื้นที่หลัก 2 โหมด ---
st.title("🎓 AI Study & Exam Assistant")
tab_exam, tab_summary = st.tabs(["📚 1. เฉลยข้อสอบ & แยกบท", "📝 2. สรุปไฟล์เรียน / เสียง / Textbook เป็น PDF"])

# ==========================================
# TAB 1: ระบบเฉลยข้อสอบ (รองรับปุ่มยกเลิกกลางคัน)
# ==========================================
with tab_exam:
    if "exam_job" not in st.session_state:
        st.session_state["exam_job"] = None

    exam_job = st.session_state["exam_job"]

    # กรณีกำลังประมวลผลข้อสอบอยู่ใน Background Thread
    if exam_job is not None and exam_job["running"]:
        st.info(f"⏳ กำลังประมวลผลไฟล์ที่ {exam_job['current_idx']}/{exam_job['total']} : **{exam_job['current_file']}** (ด้วย `{exam_job['model']}`)")
        st.progress(exam_job["progress"])

        if st.button("🛑 ยกเลิกการดำเนินการ", type="primary", key="cancel_exam_btn"):
            exam_job["cancel_event"].set()
            st.warning("กำลังสั่งยกเลิก... กรุณารอสักครู่")

        time.sleep(1)
        st.rerun()

    # กรณีงานเพิ่งเสร็จสิ้นหรือถูกยกเลิก ให้แสดงผลแจ้งเตือนแล้วเคลียร์สถานะ
    elif exam_job is not None and not exam_job["running"]:
        if exam_job["cancelled"]:
            st.warning(f"🛑 ยกเลิกการดำเนินการแล้ว (บันทึกไฟล์ที่ทำเสร็จก่อนหน้าไว้ {exam_job['completed_count']} ไฟล์)")
        else:
            st.success(f"✅ ประมวลผลเสร็จสิ้นทั้งหมด {exam_job['completed_count']} ไฟล์!")
        for err in exam_job["errors"]:
            st.error(err)
        st.session_state["exam_job"] = None

    # ฟอร์มนำเข้าไฟล์ข้อสอบ
    input_method = st.radio(
        "เลือกวิธีนำเข้าไฟล์ข้อสอบ",
        options=["📁 อัปโหลดไฟล์ PDF / รูปภาพ (เลือกได้หลายไฟล์พร้อมกัน)", "🔗 วางลิงก์ไฟล์เดี่ยวจาก Google Drive"],
        horizontal=True
    )

    def start_exam_background_job(tasks_list):
        cancel_ev = threading.Event()
        job_state = {
            "running": True,
            "cancelled": False,
            "cancel_event": cancel_ev,
            "current_idx": 1,
            "total": len(tasks_list),
            "current_file": tasks_list[0]["name"],
            "progress": 0.0,
            "completed_count": 0,
            "errors": [],
            "model": selected_model
        }
        st.session_state["exam_job"] = job_state

        def worker():
            for idx, task in enumerate(tasks_list):
                if cancel_ev.is_set():
                    job_state["cancelled"] = True
                    break
                job_state["current_idx"] = idx + 1
                job_state["current_file"] = task["name"]
                try:
                    if task["type"] == "drive_link":
                        raw_bytes, mime_type, f_name = download_public_drive_file(task["url"])
                        job_state["current_file"] = f_name
                    else:
                        raw_bytes, mime_type, f_name = task["bytes"], task["mime"], task["name"]

                    if cancel_ev.is_set():
                        job_state["cancelled"] = True
                        break

                    f_hash = hashlib.md5(raw_bytes).hexdigest()
                    solved = analyze_exam_with_gemini(
                        raw_bytes, mime_type, f_name, gemini_key, selected_model, use_thinking, CURRENT_USER
                    )
                    if cancel_ev.is_set():
                        job_state["cancelled"] = True
                        break

                    save_questions(CURRENT_USER, f_hash, f_name, solved)
                    job_state["completed_count"] += 1
                except Exception as e:
                    job_state["errors"].append(f"ผิดพลาดที่ {job_state['current_file']}: {e}")

                job_state["progress"] = (idx + 1) / len(tasks_list)
                if idx < len(tasks_list) - 1 and not cancel_ev.is_set():
                    for _ in range(4):
                        if cancel_ev.is_set():
                            break
                        time.sleep(1)

            if cancel_ev.is_set():
                job_state["cancelled"] = True
            job_state["running"] = False

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        st.rerun()

    if input_method == "📁 อัปโหลดไฟล์ PDF / รูปภาพ (เลือกได้หลายไฟล์พร้อมกัน)":
        uploaded_exams = st.file_uploader(
            "ลากไฟล์ข้อสอบมาวางที่นี่ (เลือกพร้อมกันหลายไฟล์ได้เลย)",
            type=["pdf", "png", "jpg", "jpeg", "webp"],
            accept_multiple_files=True
        )
        if st.button("🚀 เริ่มแยกบทและเฉลยข้อสอบ", type="primary", key="btn_solve_upload"):
            if not gemini_key:
                st.error("ไม่พบ Gemini API Key กรุณาติดต่อ Admin")
            elif not uploaded_exams:
                st.error("กรุณาเลือกไฟล์ข้อสอบอย่างน้อย 1 ไฟล์ก่อนครับ")
            else:
                tasks = [{"type": "upload", "bytes": uf.getvalue(), "mime": uf.type, "name": uf.name} for uf in uploaded_exams]
                start_exam_background_job(tasks)

    else:
        col_link, col_btn = st.columns([3, 1])
        with col_link:
            drive_file_url = st.text_input("วางลิงก์ไฟล์ Google Drive (เช่น https://drive.google.com/file/d/.../view)")
        with col_btn:
            st.write("")
            st.write("")
            solve_link_btn = st.button("🚀 ดึงไฟล์และเฉลย", type="primary", use_container_width=True)

        if solve_link_btn:
            if not gemini_key:
                st.error("ไม่พบ Gemini API Key กรุณาติดต่อ Admin")
            elif not drive_file_url:
                st.error("กรุณาวางลิงก์ไฟล์ก่อนครับ")
            else:
                tasks = [{"type": "drive_link", "url": drive_file_url, "name": "ไฟล์จาก Google Drive"}]
                start_exam_background_job(tasks)

    st.divider()

    questions = load_questions_for_user(CURRENT_USER, IS_ADMIN)

    with st.expander("💾 สำรอง / กู้คืน / ลบข้อมูลเฉลย"):
        c_dl, c_ul, c_del = st.columns(3)
        with c_dl:
            if questions:
                st.download_button(
                    "⬇️ ดาวน์โหลด Backup (.json)",
                    data=json.dumps(questions, ensure_ascii=False, indent=2),
                    file_name=f"exam_backup_{CURRENT_USER}.json",
                    mime="application/json"
                )
        with c_ul:
            uploaded_backup = st.file_uploader("⬆️ กู้คืนจากไฟล์ .json", type=["json"], key="restore_json")
            if uploaded_backup:
                restored = json.loads(uploaded_backup.read().decode("utf-8"))
                conn = sqlite3.connect(DB_FILE)
                c = conn.cursor()
                for r in restored:
                    target_owner = r.get("owner_username", CURRENT_USER) if IS_ADMIN else CURRENT_USER
                    c.execute("""
                        INSERT INTO questions (owner_username, file_hash, file_name, chapter, question_number, question_text, solution_steps, final_answer)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (target_owner, r.get("file_hash", ""), r["file_name"], r["chapter"], r["question_number"], r["question_text"], r["solution_steps"], r["final_answer"]))
                conn.commit()
                conn.close()
                st.success("กู้คืนสำเร็จ!")
                st.rerun()
        with c_del:
            if questions:
                if IS_ADMIN:
                    del_options = sorted(list(set(f"{q['owner_username']} | {q['file_name']}" for q in questions)))
                    target_del = st.selectbox("เลือกไฟล์ที่ต้องการลบ (User | File)", del_options)
                    if st.button("🗑️ ลบเฉลยที่เลือก"):
                        u_name, f_name = target_del.split(" | ", 1)
                        delete_file_questions(u_name, f_name)
                        st.warning(f"ลบ {f_name} ของ {u_name} แล้ว")
                        st.rerun()
                else:
                    del_options = sorted(list(set(q["file_name"] for q in questions)))
                    f_name = st.selectbox("เลือกไฟล์ของคุณที่ต้องการลบ", del_options)
                    if st.button("🗑️ ลบเฉลยของไฟล์นี้"):
                        delete_file_questions(CURRENT_USER, f_name)
                        st.warning(f"ลบข้อมูลของ {f_name} แล้ว")
                        st.rerun()

    if not questions:
        st.info("ยังไม่มีข้อมูลเฉลยข้อสอบของคุณ อัปโหลดไฟล์ PDF/รูปภาพ หรือวางลิงก์ไฟล์ด้านบนเพื่อเริ่มเฉลยได้เลยครับ")
    else:
        if IS_ADMIN:
            all_owners = sorted(list(set(q["owner_username"] for q in questions)))
            col_u, col1, col2, col3 = st.columns([1.5, 2, 2, 1])
            with col_u:
                selected_owner = st.selectbox("👤 กรองตามผู้ใช้ (Admin)", ["ทั้งหมด"] + all_owners)
        else:
            selected_owner = CURRENT_USER
            col1, col2, col3 = st.columns([2, 2, 1])

        owner_filtered = [q for q in questions if (selected_owner == "ทั้งหมด" or q["owner_username"] == selected_owner)]
        chapters = sorted(list(set(q["chapter"] for q in owner_filtered)))
        file_names = sorted(list(set(q["file_name"] for q in owner_filtered)))

        with col1:
            selected_chapter = st.selectbox("📖 กรองตามบทเรียน", ["ทั้งหมด"] + chapters)
        with col2:
            selected_file = st.selectbox("📄 กรองตามไฟล์ต้นฉบับ", ["ทั้งหมด"] + file_names)
        with col3:
            st.metric("จำนวนข้อ", f"{len(owner_filtered)} ข้อ")

        filtered = [
            q for q in owner_filtered
            if (selected_chapter == "ทั้งหมด" or q["chapter"] == selected_chapter)
            and (selected_file == "ทั้งหมด" or q["file_name"] == selected_file)
        ]

        for q in filtered:
            with st.container(border=True):
                c_left, c_right = st.columns([3, 1.2])
                c_left.markdown(f"### {q['question_number']} — `{q['chapter']}`")
                if IS_ADMIN:
                    c_right.caption(f"👤 เจ้าของ: **{q['owner_username']}**\n📄 ไฟล์: {q['file_name']}")
                else:
                    c_right.caption(f"📄 ไฟล์: {q['file_name']}")
                st.markdown(q["question_text"])

                with st.expander("💡 ดูวิธีทำและคำตอบ"):
                    st.markdown("**วิธีทำ:**")
                    st.markdown(q["solution_steps"])
                    st.success(f"**คำตอบ:** {q['final_answer']}")

# ==========================================
# TAB 2: ระบบสรุปไฟล์เรียนเป็น PDF (รองรับปุ่มยกเลิกกลางคัน)
# ==========================================
with tab_summary:
    if "summary_job" not in st.session_state:
        st.session_state["summary_job"] = None

    sum_job = st.session_state["summary_job"]

    # กรณีกำลังสรุปไฟล์อยู่ใน Background Thread
    if sum_job is not None and sum_job["running"]:
        st.info(f"⏳ กำลังใช้ `{sum_job['model']}` วิเคราะห์ไฟล์ **{sum_job['file_name']}** และจัดหน้า PDF...")
        if st.button("🛑 ยกเลิกการสรุปไฟล์", type="primary", key="cancel_summary_btn"):
            sum_job["cancel_event"].set()
            st.warning("กำลังสั่งยกเลิก... กรุณารอสักครู่")
        time.sleep(1)
        st.rerun()

    elif sum_job is not None and not sum_job["running"]:
        if sum_job["cancelled"]:
            st.warning("🛑 ยกเลิกการสร้างไฟล์สรุปแล้ว")
        elif sum_job["error"]:
            st.error(f"เกิดข้อผิดพลาดในการสรุปไฟล์: {sum_job['error']}")
        else:
            st.session_state["generated_pdf"] = sum_job["pdf_bytes"]
            st.session_state["generated_summary"] = sum_job["summary_data"]
            st.session_state["generated_pdf_name"] = sum_job["pdf_name"]
            st.success("✅ สร้างไฟล์สรุป PDF เรียบร้อยแล้ว!")
        st.session_state["summary_job"] = None

    st.subheader("อัปโหลดสื่อการเรียนเพื่อสร้างไฟล์สรุป PDF")
    st.caption("รองรับไฟล์ PDF, Textbook, สไลด์อาจารย์, ไฟล์เสียงอัดบรรยาย (MP3, WAV, M4A, OGG), รูปภาพ และไฟล์ข้อความ")

    uploaded_study_file = st.file_uploader(
        "📁 เลือกไฟล์ที่ต้องการสรุป",
        type=["pdf", "mp3", "wav", "m4a", "ogg", "aac", "png", "jpg", "jpeg", "txt", "md"]
    )

    col_fmt, col_lang, col_opt = st.columns([1.2, 1.2, 2])
    with col_fmt:
        output_format = st.radio(
            "📄 รูปแบบไฟล์ PDF",
            options=["สไลด์ PDF (แนวนอน 16:9)", "เอกสารสรุป PDF (แนวตั้ง A4)"]
        )
    with col_lang:
        summary_lang = st.radio(
            "🌐 ภาษาที่ต้องการให้สรุป",
            options=[
                "ภาษาไทย (Thai)",
                "ภาษาอังกฤษ (English)",
                "ไทย + ศัพท์เทคนิคอังกฤษ"
            ]
        )
    with col_opt:
        custom_prompt = st.text_area(
            "✍️ ระบุแนวทางการสรุปที่ต้องการ (Prompt)",
            placeholder="ตัวอย่าง:\n- สรุปเน้นเฉพาะสูตรคำนวณ เงื่อนไขการใช้งาน และจุดที่มักออกสอบ\n- Summarize key concepts, equations, and exam tips concisely.\n- ถอดความจากไฟล์เสียงบรรยายนี้ แล้วจัดหมวดหมู่เป็นหัวข้อพร้อมตัวอย่างประกอบ",
            height=130
        )

    if st.button("✨ สร้างไฟล์สรุป PDF", type="primary"):
        if not gemini_key:
            st.error("ไม่พบ Gemini API Key กรุณาติดต่อ Admin หรือตั้งค่าใน Secrets")
        elif not uploaded_study_file:
            st.error("กรุณาอัปโหลดไฟล์ที่ต้องการสรุปก่อนครับ")
        else:
            cancel_ev = threading.Event()
            f_bytes = uploaded_study_file.getvalue()
            f_name = uploaded_study_file.name
            is_slide = (output_format == "สไลด์ PDF (แนวนอน 16:9)")
            lang_tag = "EN" if summary_lang == "ภาษาอังกฤษ (English)" else "TH"
            out_pdf_name = f"Summary_{lang_tag}_{os.path.splitext(f_name)[0]}.pdf"

            job_state = {
                "running": True,
                "cancelled": False,
                "cancel_event": cancel_ev,
                "file_name": f_name,
                "model": selected_model,
                "error": None,
                "pdf_bytes": None,
                "summary_data": None,
                "pdf_name": out_pdf_name
            }
            st.session_state["summary_job"] = job_state

            def summary_worker():
                try:
                    s_data = summarize_file_with_gemini(
                        f_bytes, f_name, custom_prompt, output_format, summary_lang,
                        gemini_key, selected_model, use_thinking, CURRENT_USER, cancel_ev
                    )
                    if cancel_ev.is_set() or s_data is None:
                        job_state["cancelled"] = True
                    else:
                        p_bytes = build_pdf_bytes(s_data, is_slide, summary_lang)
                        job_state["summary_data"] = s_data
                        job_state["pdf_bytes"] = p_bytes
                except Exception as e:
                    job_state["error"] = str(e)
                finally:
                    job_state["running"] = False

            t = threading.Thread(target=summary_worker, daemon=True)
            t.start()
            st.rerun()

    if "generated_pdf" in st.session_state:
        st.divider()
        st.download_button(
            label=f"⬇️ ดาวน์โหลดไฟล์ {st.session_state['generated_pdf_name']}",
            data=st.session_state["generated_pdf"],
            file_name=st.session_state["generated_pdf_name"],
            mime="application/pdf",
            type="primary"
        )

        st.markdown("### 👀 ตัวอย่างเนื้อหาที่สรุปได้")
        s_data = st.session_state["generated_summary"]
        st.markdown(f"#### {s_data.get('doc_title', '')}")
        if s_data.get("subtitle"):
            st.caption(s_data["subtitle"])

        for idx, sec in enumerate(s_data.get("sections", []), start=1):
            with st.container(border=True):
                st.markdown(f"**{idx}. {sec.get('title', '')}**")
                for b in sec.get("bullets", []):
                    st.markdown(f"- {b}")
                if sec.get("highlight_note"):
                    st.info(f"💡 {sec['highlight_note']}")