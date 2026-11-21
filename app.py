import hashlib
import json
import os
import re
import sqlite3
import tempfile
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

# --- จัดการฐานข้อมูล SQLite (ผูกข้อมูลกับ owner_username) ---
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

# --- ดาวน์โหลดไฟล์เดี่ยวจากลิงก์ Google Drive (ไม่ต้องใช้ API Key) ---
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

    # กรณีไฟล์ใหญ่ Google Drive จะขึ้นหน้า Confirm
    for key, value in res.cookies.items():
        if key.startswith("download_warning"):
            res = session.get(f"{download_url}&confirm={value}", timeout=60)
            break

    content_type = res.headers.get("Content-Type", "")
    if "text/html" in content_type:
        raise Exception("ไม่สามารถดาวน์โหลดไฟล์ได้ (กรุณาเช็กว่าตั้งค่าแชร์ไฟล์เป็น 'ทุกคนที่มีลิงก์' และไม่ได้ปิดการดาวน์โหลด)")

    # พยายามดึงชื่อไฟล์จาก Header
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

# --- ฟังก์ชันเรียก Gemini AI ---
def analyze_exam_with_gemini(file_bytes: bytes, mime_type: str, file_name: str, gemini_key: str, model_name: str, use_thinking: bool):
    client = genai.Client(api_key=gemini_key)
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
    return json.loads(response.text).get("questions", [])

def summarize_file_with_gemini(uploaded_file, custom_prompt: str, output_format: str, summary_lang: str, gemini_key: str, model_name: str, use_thinking: bool):
    client = genai.Client(api_key=gemini_key)

    with tempfile.NamedTemporaryFile(delete=False, suffix=f"_{uploaded_file.name}") as tmp:
        tmp.write(uploaded_file.getvalue())
        tmp_path = tmp.name

    try:
        gemini_file = client.files.upload(file=tmp_path)
        while gemini_file.state.name == "PROCESSING":
            time.sleep(2)
            gemini_file = client.files.get(name=gemini_file.name)

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

        client.files.delete(name=gemini_file.name)
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
    model_preset = st.selectbox(
        "เลือกรุ่นโมเดล",
        options=[
            "gemini-2.5-pro",
            "gemini-3.1-pro-preview",
            "gemini-3-flash-preview",
            "gemini-2.5-flash",
            "กำหนดชื่อ Model ID เอง..."
        ],
        index=0
    )
    if model_preset == "กำหนดชื่อ Model ID เอง...":
        selected_model = st.text_input("พิมพ์ชื่อ Model ID", value="gemini-3.1-pro-preview")
    else:
        selected_model = model_preset

    use_thinking = st.checkbox("เปิดโหมดคิดวิเคราะห์ลึก (Thinking)", value=True, help="ช่วยให้คำนวณและสรุปเนื้อหาซับซ้อนแม่นยำขึ้น")

# --- พื้นที่หลัก 2 โหมด ---
st.title("🎓 AI Study & Exam Assistant")
tab_exam, tab_summary = st.tabs(["📚 1. เฉลยข้อสอบ & แยกบท", "📝 2. สรุปไฟล์เรียน / เสียง / Textbook เป็น PDF"])

# ==========================================
# TAB 1: ระบบเฉลยข้อสอบ (อัปโหลดไฟล์ หรือ วางลิงก์ไฟล์ Drive)
# ==========================================
with tab_exam:
    input_method = st.radio(
        "เลือกวิธีนำเข้าไฟล์ข้อสอบ",
        options=["📁 อัปโหลดไฟล์ PDF / รูปภาพ (เลือกได้หลายไฟล์พร้อมกัน)", "🔗 วางลิงก์ไฟล์เดี่ยวจาก Google Drive"],
        horizontal=True
    )

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
                progress = st.progress(0)
                for idx, uf in enumerate(uploaded_exams):
                    with st.spinner(f"กำลังใช้ {selected_model} อ่านและเฉลย: {uf.name}..."):
                        try:
                            raw_bytes = uf.getvalue()
                            f_hash = hashlib.md5(raw_bytes).hexdigest()
                            solved = analyze_exam_with_gemini(raw_bytes, uf.type, uf.name, gemini_key, selected_model, use_thinking)
                            save_questions(CURRENT_USER, f_hash, uf.name, solved)
                        except Exception as e:
                            st.error(f"ผิดพลาดที่ไฟล์ {uf.name}: {e}")
                    progress.progress((idx + 1) / len(uploaded_exams))
                    if idx < len(uploaded_exams) - 1:
                        time.sleep(4)
                st.success("ประมวลผลเสร็จสิ้น!")
                st.rerun()

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
                with st.spinner(f"กำลังดาวน์โหลดและใช้ {selected_model} เฉลยข้อสอบ..."):
                    try:
                        raw_bytes, mime_type, f_name = download_public_drive_file(drive_file_url)
                        f_hash = hashlib.md5(raw_bytes).hexdigest()
                        solved = analyze_exam_with_gemini(raw_bytes, mime_type, f_name, gemini_key, selected_model, use_thinking)
                        save_questions(CURRENT_USER, f_hash, f_name, solved)
                        st.success(f"เฉลยไฟล์ {f_name} เสร็จสิ้น!")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

    st.divider()

    # โหลดข้อมูลตามสิทธิ์
    questions = load_questions_for_user(CURRENT_USER, IS_ADMIN)

    # จัดการสำรอง/ลบข้อมูล
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

    # แสดงผลคลังข้อสอบ
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
# TAB 2: ระบบสรุปไฟล์เรียน / ไฟล์เสียง / Textbook เป็น PDF
# ==========================================
with tab_summary:
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
            with st.spinner(f"กำลังใช้ {selected_model} วิเคราะห์ไฟล์ '{uploaded_study_file.name}' และสร้าง PDF ({summary_lang})..."):
                try:
                    summary_data = summarize_file_with_gemini(
                        uploaded_study_file, custom_prompt, output_format, summary_lang,
                        gemini_key, selected_model, use_thinking
                    )
                    is_slide = (output_format == "สไลด์ PDF (แนวนอน 16:9)")
                    pdf_bytes = build_pdf_bytes(summary_data, is_slide, summary_lang)

                    lang_tag = "EN" if summary_lang == "ภาษาอังกฤษ (English)" else "TH"
                    st.session_state["generated_pdf"] = pdf_bytes
                    st.session_state["generated_summary"] = summary_data
                    st.session_state["generated_pdf_name"] = f"Summary_{lang_tag}_{os.path.splitext(uploaded_study_file.name)[0]}.pdf"
                    st.success("สร้างไฟล์สรุป PDF เรียบร้อยแล้ว!")
                except Exception as e:
                    st.error(f"เกิดข้อผิดพลาดในการสรุปไฟล์: {e}")

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