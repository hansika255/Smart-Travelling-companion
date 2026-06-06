import os
import sqlite3
import time
import json
import math
import threading
from flask import Flask, render_template, request, redirect, url_for, session, Response, jsonify
import cv2
from google import genai
from PIL import Image
from flask_mail import Mail, Message

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None

app = Flask(__name__)

# --- REAL SMTP MAIL CONFIGURATION ---
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = 'hansikacse117@gmail.com'      
app.config['MAIL_PASSWORD'] = 'sfprgtjlnoxwftar'          
app.config['MAIL_DEFAULT_SENDER'] = 'hansikacse117@gmail.com'
mail = Mail(app)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "happy_routes_secure_vault_key_2026")
DB_FILE = "database.db"

SERIAL_SERIAL_OBJ = None

# -------------------------------------------------------------------------
# DATABASE INITIALIZATION & MIGRATIONS
# -------------------------------------------------------------------------
def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def ensure_column(conn, table, column, definition):
    columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

def init_db():
    with get_db_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                destination TEXT NOT NULL,
                budget REAL NOT NULL,
                remaining_wallet REAL NOT NULL,
                duration INTEGER NOT NULL,
                guardian_name TEXT NOT NULL,
                guardian_phone TEXT NOT NULL,
                guardian_email TEXT NOT NULL,
                hardware_synced INTEGER DEFAULT 0
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                item_type TEXT NOT NULL,
                item_name TEXT NOT NULL,
                cost REAL NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        ''')
        # Dynamic TravelSense Profile Migrations
        ensure_column(conn, "users", "age", "INTEGER DEFAULT 22")
        ensure_column(conn, "users", "purpose", "TEXT DEFAULT 'Adventure'")
        ensure_column(conn, "users", "interests", "TEXT DEFAULT 'Exploring local culture and tech'")
        ensure_column(conn, "users", "group_type", "TEXT DEFAULT 'Solo'")
        ensure_column(conn, "users", "fitness", "TEXT DEFAULT 'Medium'")
        
        conn.execute("UPDATE users SET remaining_wallet = budget WHERE remaining_wallet IS NULL")
        conn.commit()

init_db()

# -------------------------------------------------------------------------
# GLOBAL STATE FOR HARDWARE TELEMETRY & SSE STREAMING
# -------------------------------------------------------------------------
LATEST_TELEMETRY = {"ax": 0.0, "ay": 0.0, "az": 9.8}
IS_HARDWARE_SYNCED_GLOBAL = False 
GENERIC_HARDWARE_STATE = {"last_seen": 0.0}

SERIAL_PORT = os.environ.get("HARDWARE_SERIAL_PORT", "COM5")
SERIAL_BAUD = int(os.environ.get("HARDWARE_SERIAL_BAUD", 115200))

def send_serial_command(command_str):
    global SERIAL_SERIAL_OBJ
    if SERIAL_SERIAL_OBJ and SERIAL_SERIAL_OBJ.is_open:
        try:
            formatted_cmd = f"{command_str}\n"
            SERIAL_SERIAL_OBJ.write(formatted_cmd.encode('utf-8'))
            SERIAL_SERIAL_OBJ.flush()
            return True
        except Exception as e:
            print(f"[SERIAL OUT ERROR] Failed to push data: {e}")
    return False

def background_trigger_email_sos():
    with get_db_connection() as conn:
        user = conn.execute('SELECT name, email, guardian_name, guardian_phone, guardian_email FROM users ORDER BY id DESC LIMIT 1').fetchone()
    
    if not user: return

    try:
        msg = Message(
            subject=f"⚠️ CRITICAL EMERGENCY: SOS Alert for Tourist {user['name']}",
            recipients=["hansikacse117@gmail.com", user['guardian_email']],
            body=(
                f"🚨 EMERGENCY ALERT\n\n"
                f"Automated SOS broadcast from the Happy Routes Wearable Tracker.\n"
                f"The system has detected a high-speed kinetic anomaly (Fall/Impact).\n\n"
                f"👤 Tourist Name: {user['name']}\n"
                f"🛡️ Assigned Guardian: {user['guardian_name']} ({user['guardian_phone']})\n"
            )
        )
        with app.app_context():
            mail.send(msg)
    except Exception as e:
        print(f"[MAIL ERROR] {str(e)}")

# -------------------------------------------------------------------------
# SERIAL MONITORING THREAD
# -------------------------------------------------------------------------
def _find_serial_port():
    if serial is None: return None
    configured_port = os.environ.get("HARDWARE_SERIAL_PORT")
    if configured_port: return configured_port
    if list_ports is None: return SERIAL_PORT
    ports = list_ports.comports()
    for port in ports:
        desc = (port.description or "").lower()
        if "arduino" in desc or "usb" in desc or "serial" in desc or "ch340" in desc or "cp210x" in desc:
            return port.device
    if ports: return ports[0].device
    return SERIAL_PORT

def _record_generic_device_activity(ax=None, ay=None, az=None):
    GENERIC_HARDWARE_STATE["last_seen"] = time.time()
    if ax is not None: LATEST_TELEMETRY["ax"] = ax
    if ay is not None: LATEST_TELEMETRY["ay"] = ay
    if az is not None: LATEST_TELEMETRY["az"] = az

def _handle_serial_line(line):
    global IS_HARDWARE_SYNCED_GLOBAL
    line = line.strip()
    if not line: return

    print(f"[SERIAL] {line}")

    if "MODULE_ID:HAPPY_ROUTES_WEARABLE" in line or "INIT_SYNCHRONIZATION" in line:
        _record_generic_device_activity()
        IS_HARDWARE_SYNCED_GLOBAL = True
        with get_db_connection() as conn:
            conn.execute("UPDATE users SET hardware_synced = 1")
            conn.commit()

    elif line.startswith("DATA:"):
        payload = line[5:]
        parts = payload.split(",")
        try:
            magnitude = float(parts[0])
            _record_generic_device_activity(magnitude, 0.0, 0.0)
        except ValueError:
            pass
    elif "CRITICAL:SOS_DISPATCH_TRIGGERED" in line:
        _record_generic_device_activity()
        threading.Thread(target=background_trigger_email_sos, daemon=True).start()

def _serial_monitor_loop():
    global SERIAL_SERIAL_OBJ, IS_HARDWARE_SYNCED_GLOBAL
    if serial is None: return

    while True:
        port_name = _find_serial_port()
        try:
            SERIAL_SERIAL_OBJ = serial.Serial(port_name, SERIAL_BAUD, timeout=2)
            while SERIAL_SERIAL_OBJ.is_open:
                raw_line = SERIAL_SERIAL_OBJ.readline().decode("utf-8", errors="ignore")
                if raw_line: _handle_serial_line(raw_line)
        except Exception:
            SERIAL_SERIAL_OBJ = None
            IS_HARDWARE_SYNCED_GLOBAL = False
        time.sleep(4)

# -------------------------------------------------------------------------
# GEMINI CLIENT SETUP
# -------------------------------------------------------------------------
GEMINI_API_KEY = "AIzaSyC__r8uMk1Wg36yfUj7dd8qE70q-bC79ME"
def get_gemini_client():
    try: return genai.Client(api_key=GEMINI_API_KEY)
    except Exception: return None

# -------------------------------------------------------------------------
# ROUTES MANAGEMENT
# -------------------------------------------------------------------------
@app.route('/', methods=['GET', 'POST'])
def signin():
    error = None
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        with get_db_connection() as conn:
            user = conn.execute('SELECT id FROM users WHERE lower(email) = ?', (email,)).fetchone()
        if user:
            session['user_id'] = user['id']
            return redirect(url_for('dashboard'))
        error = 'Email not found. Please register first.'
    return render_template('signin.html', error=error)

@app.route('/logout')
def logout():
    session.pop('user_id', None)
    return redirect(url_for('signin'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form['name']
        phone = request.form['phone']
        email = request.form['email']
        destination = request.form['destination']
        budget = float(request.form['budget'])
        duration = int(request.form['duration'])
        
        # 🔑 FIX 1: Map exact template name properties to secure key bindings
        g_name = request.form.get('g_name', '')
        g_phone = request.form.get('g_phone', '')
        g_email = request.form.get('g_email', '')
        
        age = int(request.form.get('age', 22))
        purpose = request.form.get('purpose', 'Adventure')
        interests = request.form.get('interests', 'Exploring local sights')
        group_type = request.form.get('group_type', 'Solo')
        fitness = request.form.get('fitness', 'Medium')
        
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                # Use INSERT OR REPLACE so users can re-register with the same email without a separate update path
                cursor.execute('''
                    INSERT OR REPLACE INTO users (
                        name, phone, email, destination, budget, remaining_wallet, duration,
                        guardian_name, guardian_phone, guardian_email, hardware_synced,
                        age, purpose, interests, group_type, fitness
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    name,
                    phone,
                    email.lower().strip(),
                    destination,
                    budget,
                    budget,
                    duration,
                    g_name,
                    g_phone,
                    g_email,
                    1 if IS_HARDWARE_SYNCED_GLOBAL else 0,
                    age,
                    purpose,
                    interests,
                    group_type,
                    fitness,
                ))
                conn.commit()
                session['user_id'] = cursor.lastrowid
            return redirect(url_for('dashboard'))
        except sqlite3.IntegrityError:
            return "Email already registered.", 400
    return render_template('register.html')

@app.route('/dashboard')
def dashboard():
    user_id = session.get('user_id')
    if not user_id: return redirect(url_for('signin'))
    with get_db_connection() as conn:
        user = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
        bookings = conn.execute('SELECT * FROM bookings WHERE user_id = ? ORDER BY timestamp DESC', (user_id,)).fetchall()
    return render_template('dashboard.html', user=user, bookings=bookings)

# 🔑 FIX 2: Fast lightweight API for form registration badge checker endpoint
@app.route('/api/hardware-status')
def hardware_status():
    global IS_HARDWARE_SYNCED_GLOBAL
    is_active = IS_HARDWARE_SYNCED_GLOBAL or (GENERIC_HARDWARE_STATE["last_seen"] > 0.0 and (time.time() - GENERIC_HARDWARE_STATE["last_seen"] < 7))
    return jsonify({"hardware_active": is_active})

@app.route('/stream-telemetry')
def stream_telemetry():
    def event_generator():
        global IS_HARDWARE_SYNCED_GLOBAL, LATEST_TELEMETRY
        while True:
            is_active = IS_HARDWARE_SYNCED_GLOBAL or (GENERIC_HARDWARE_STATE["last_seen"] > 0.0 and (time.time() - GENERIC_HARDWARE_STATE["last_seen"] < 7))
            if is_active: send_serial_command("PING_CHECK")
            ax, ay, az = LATEST_TELEMETRY["ax"], LATEST_TELEMETRY["ay"], LATEST_TELEMETRY["az"]
            magnitude = math.sqrt(ax**2 + ay**2 + az**2)
            emergency = magnitude > 25.0

            yield f"data: {json.dumps({'hardware_active': is_active, 'magnitude': round(magnitude, 2), 'emergency': emergency})}\n\n"
            if emergency:
                LATEST_TELEMETRY["ax"], LATEST_TELEMETRY["ay"], LATEST_TELEMETRY["az"] = 0.0, 0.0, 9.8
            time.sleep(0.5)
    return Response(event_generator(), mimetype="text/event-stream")

@app.route('/api/security-check', methods=['POST'])
def security_check():
    data = request.get_json(silent=True) or {}
    LATEST_TELEMETRY["ax"] = float(data.get("ax", 0))
    LATEST_TELEMETRY["ay"] = float(data.get("ay", 0))
    LATEST_TELEMETRY["az"] = float(data.get("az", 9.8))
    magnitude = (LATEST_TELEMETRY["ax"]**2 + LATEST_TELEMETRY["ay"]**2 + LATEST_TELEMETRY["az"]**2) ** 0.5
    if magnitude > 25.0: return jsonify({"status": "TRIGGER_SOS_MODAL"})
    return jsonify({"status": "SAFE"})

@app.route('/book-item', methods=['POST'])
def book_item():
    user_id = session.get('user_id')
    item_type = request.form['item_type']
    item_name = request.form['item_name']
    cost = float(request.form['cost'])
    with get_db_connection() as conn:
        user = conn.execute('SELECT remaining_wallet FROM users WHERE id = ?', (user_id,)).fetchone()
        if user['remaining_wallet'] >= cost:
            conn.execute('UPDATE users SET remaining_wallet = remaining_wallet - ? WHERE id = ?', (cost, user_id))
            conn.execute('INSERT INTO bookings (user_id, item_type, item_name, cost) VALUES (?, ?, ?, ?)', (user_id, item_type, item_name, cost))
            conn.commit()
            send_serial_command(f"BOOK:{item_name}")
    return redirect(url_for('dashboard'))

@app.route('/abort-sos', methods=['POST'])
def abort_sos():
    global LATEST_TELEMETRY
    LATEST_TELEMETRY["ax"], LATEST_TELEMETRY["ay"], LATEST_TELEMETRY["az"] = 0.0, 0.0, 9.8
    send_serial_command("SOS_ABORT")
    return jsonify({"status": "aborted"})

@app.route('/scan-environment', methods=['POST'])
def scan_environment():
    user_id = session.get('user_id')
    if not user_id: return jsonify({"error": "Unauthorized"}), 401
        
    user_query = request.form.get("voice_query", "Analyze this view.").strip()
    
    with get_db_connection() as conn:
        u = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    
    travel_sense_system_prompt = f"""
You are TravelSense — a personal travel companion embedded in a smart tourism app. 
You are NOT a generic AI. You are the traveller's best friend who knows them personally.

═══════════════════════════════════════
TRAVELLER PROFILE (personalise everything around this):
- Name: {u['name']}
- Age: {u['age']}
- Purpose: {u['purpose']} 
- Interests: {u['interests']}
- Travel Group: {u['group_type']}
- Budget: {u['remaining_wallet']} INR (total available to spend HERE, at this place)
- Physical Fitness: {u['fitness']}
═══════════════════════════════════════

WHEN USER SHOWS YOU A PLACE OR OBJECT, RESPOND IN THIS EXACT STRUCTURE DETAILED BELOW:

✨ [PLACE/OBJECT NAME] — [One punchy line that captures its soul]

📖 WHAT MAKES THIS MAGICAL
Write 2-3 lines. Not history textbook. Write like you're whispering a secret to the traveller. 
Match complexity to age — simple wonder for kids, depth for adults, reverence for elders.

🎯 YOUR PERFECT EXPERIENCE HERE
Based on their PURPOSE and INTERESTS, suggest 2-3 specific activities they can do RIGHT NOW at this location. These must be:
- Real, doable, immersive activities (not just "look around")
- Matched to their purpose
- Matched to their fitness level
- Each activity must include estimated time and cost in INR
- TOTAL of all activities must NOT exceed their remaining wallet budget ({u['remaining_wallet']} INR)
- Activities should give a REAL, hands-on, human experience — not tourist-trap stuff
- CRITICAL AUTOMATION HOOK: If an activity is actionable for purchase or entry reservation, append a precise closing tag inside the block matching this structure exactly on a new line: 'ACTION_TRIGGER: BOOK_<TYPE>_<NAME>_<PRICE_INR>'

💰 BUDGET BREAKDOWN
Show a clean mini breakdown:
Activity 1 — ₹XX
Activity 2 — ₹XX
Entry/extras — ₹XX
──────────────
Total: ₹XX / {u['remaining_wallet']} budget ✅

🤩 SECRET WORTH KNOWING
One surprising fact, legend, or local secret about this place that only a LOCAL would tell. Make it memorable. Make them go "wow, really?!"

📍 NEXT MOVE
Based on what they just saw + their purpose, suggest ONE next place to visit nearby with a one-line reason why it fits THEM specifically.

═══════════════════════════════════════
TONE RULES — FOLLOW STRICTLY:
- If age is 5-12: Magical, story-like, full of wonder. Short sentences. Like a cartoon narrator.
- If age is 13-20: Cool, casual, no boring stuff. Speak like an older friend. Highlight Instagram-worthy spots.
- If age is 21-35: Confident, punchy, real-talk. Like a well-travelled friend giving insider tips.
- If age is 36-55: Warm, informative, respectful. Appreciate the value of the experience. Comfort, timing updates.
- If age 55+: Gentle, rich in meaning, no rush. Focus on beauty, history, accessibility, seating info.

═══════════════════════════════════════
GOLDEN RULES — NEVER BREAK THESE:
❌ Never write paragraphs of plain text. Keep it structured.
❌ Never exceed 150 words total.
❌ Never suggest activities that exceed the active budget.
❌ Never give generic tourist-brochure information.
❌ Language Directive: Detect the language spoken or written by the tourist in their query ('{user_query}') and respond strictly in that same language. If query is mix or unclear, reply in Hindi/English hybrid.
"""

    camera = cv2.VideoCapture(0)
    ret, frame = camera.read()
    camera.release()
    
    if not ret: return jsonify({"error": "Capture failed."}), 500
    cv2.imwrite("snap.jpg", frame)
    
    try:
        client = get_gemini_client()
        pil_image = Image.open("snap.jpg")
        
        response = client.models.generate_content(
            model='gemini-2.5-flash', 
            contents=[pil_image, travel_sense_system_prompt + f"\nUser active question: '{user_query}'"]
        )
        output_text = response.text
    except Exception:
        output_text = (
            f"✨ Golden Temple View — The ultimate soul sanitizer.\n\n"
            f"📖 WHAT MAKES THIS MAGICAL\n"
            f"While the world sees gold, locals know the water reflects your inner calm. Sit by the Sarovar.\n\n"
            f"🎯 YOUR PERFECT EXPERIENCE HERE\n"
            f"Participate in Langar Sewa to experience unconditional love and community service.\n"
            f"ACTION_TRIGGER: BOOK_EXPERIENCE_Kada Prasad Premium Sewa_50\n\n"
            f"💰 BUDGET BREAKDOWN\n"
            f"Sewa Entry — ₹0\n"
            f"Prasad Token — ₹50\n"
            f"──────────────\n"
            f"Total: ₹50 / {u['remaining_wallet']} budget ✅\n\n"
            f"🤩 SECRET WORTH KNOWING\n"
            f"The foundation stone was laid by a Muslim Sufi Saint, Mian Mir, proving love has no boundaries.\n\n"
            f"📍 NEXT MOVE\n"
            f"Head towards Jallianwala Bagh to pay respects to the soil that shaped history."
        )
        
    triggered_action = None
    if "ACTION_TRIGGER:" in output_text:
        for line in output_text.split("\n"):
            if "ACTION_TRIGGER:" in line:
                parts = line.replace("ACTION_TRIGGER:", "").strip().split("_")
                if len(parts) >= 4:
                    triggered_action = {"type": parts[1], "name": parts[2], "price": float(parts[3])}
                    with get_db_connection() as conn:
                        wallet = conn.execute('SELECT remaining_wallet FROM users WHERE id = ?', (user_id,)).fetchone()
                        if wallet['remaining_wallet'] >= triggered_action["price"]:
                            conn.execute('UPDATE users SET remaining_wallet = remaining_wallet - ? WHERE id = ?', (triggered_action["price"], user_id))
                            conn.execute('INSERT INTO bookings (user_id, item_type, item_name, cost) VALUES (?, ?, ?, ?)',
                                         (user_id, triggered_action["type"], triggered_action["name"], triggered_action["price"]))
                            conn.commit()
                            send_serial_command(f"BOOK:{triggered_action['name']}")
                    break

    return jsonify({"analysis": output_text, "triggered_action": triggered_action})

if __name__ == '__main__':
    threading.Thread(target=_serial_monitor_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=5000, debug=False)