from dotenv import load_dotenv
load_dotenv()
from flask import Flask, request, jsonify, Response
import psycopg2
import psycopg2.extras
import hashlib
import os
import json
import requests as http_requests
from datetime import datetime, timedelta
import secrets
import re

app = Flask(__name__)

# ─── CORS — встроенный без flask-cors ───
@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    return response

@app.before_request
def handle_options():
    if request.method == 'OPTIONS':
        return Response('', 204, {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Headers': 'Content-Type, Authorization',
            'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS'
        })

# ─── CONFIG ───
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# PostgreSQL через DATABASE_URL от Railway
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Убираем возможный префикс ${{ если Railway не подставил переменную
if DATABASE_URL.startswith("${{") or not DATABASE_URL:
    # Пробуем альтернативные имена переменных Railway
    DATABASE_URL = (
        os.environ.get("PGURL") or
        os.environ.get("POSTGRES_URL") or
        os.environ.get("PG_URL") or
        ""
    )

# Railway иногда даёт URL с postgres:// — psycopg2 нужен postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# ─── ПРОМОКОДЫ ───
PROMO_CODES = {
    "FITFREE30":  {"type": "days",    "days": 30,    "label": "30 дней бесплатно"},
    "FITFOREVER": {"type": "forever", "days": 36500, "label": "Пожизненный доступ"},
    "FITSTART":   {"type": "days",    "days": 7,     "label": "7 дней бесплатно"},
    "FITPRO":     {"type": "days",    "days": 90,    "label": "90 дней бесплатно"},
}

# ─── DATABASE ───
def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id              SERIAL PRIMARY KEY,
            email           TEXT    UNIQUE NOT NULL,
            name            TEXT    NOT NULL,
            password        TEXT    NOT NULL,
            gender          TEXT    DEFAULT 'female',
            age             INTEGER DEFAULT 25,
            weight          REAL    DEFAULT 70,
            height          REAL    DEFAULT 170,
            place           TEXT    DEFAULT 'home',
            days_per_week   INTEGER DEFAULT 3,
            goal            TEXT    DEFAULT 'Похудеть',
            level           TEXT    DEFAULT 'Новичок',
            promo_code      TEXT    DEFAULT '',
            access_type     TEXT    DEFAULT 'trial',
            access_expires  TEXT    DEFAULT NULL,
            restrictions    TEXT    DEFAULT '',
            registered_at   TEXT    DEFAULT CURRENT_TIMESTAMP,
            last_login      TEXT    DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    # Add restrictions column if it doesnt exist (migration)
    try:
        cur.execute("ALTER TABLE users ADD COLUMN restrictions TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        conn.rollback()  # Must rollback after failed ALTER TABLE in PostgreSQL
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token       TEXT    PRIMARY KEY,
            user_id     INTEGER NOT NULL,
            created_at  TEXT    DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS workout_plans (
            id          SERIAL PRIMARY KEY,
            user_id     INTEGER NOT NULL,
            plan_data   TEXT    NOT NULL,
            created_at  TEXT    DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS exercise_done (
            id              SERIAL PRIMARY KEY,
            user_id         INTEGER NOT NULL,
            exercise_key    TEXT    NOT NULL,
            done_at         TEXT    DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS promo_usage (
            id          SERIAL PRIMARY KEY,
            user_id     INTEGER NOT NULL,
            promo_code  TEXT    NOT NULL,
            used_at     TEXT    DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

# ─── HELPERS ───
def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def get_user_from_token(token):
    if not token:
        return None
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT u.* FROM users u JOIN sessions s ON u.id = s.user_id WHERE s.token = %s",
        (token,)
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return None
    user = dict(row)
    if "days_per_week" not in user or user["days_per_week"] is None:
        user["days_per_week"] = 3
    return user

def user_to_dict(user):
    return {
        "id":             user["id"],
        "email":          user["email"],
        "name":           user["name"],
        "gender":         user["gender"],
        "age":            user["age"],
        "weight":         user["weight"],
        "height":         user["height"],
        "place":          user["place"],
        "goal":           user["goal"],
        "days_per_week":  user["days_per_week"] if "days_per_week" in user else 3,
        "level":          user["level"],
        "promo_code":     user["promo_code"],
        "access_type":    user["access_type"],
        "access_expires": user["access_expires"],
        "registered_at":  user["registered_at"],
    }

def check_access(user):
    access_type = user.get("access_type", "trial")
    access_expires = user.get("access_expires")
    registered_at = user.get("registered_at", "")
    now = datetime.utcnow()

    if access_type == "forever":
        return {"type": "forever", "valid": True, "days_left": -1, "label": "Пожизненный ♾️"}

    if access_type in ("promo", "paid") and access_expires:
        try:
            expires = datetime.fromisoformat(access_expires.replace("Z", "").split(".")[0])
            days_left = (expires - now).days
            if days_left > 0:
                label = "Промокод" if access_type == "promo" else "Premium"
                return {"type": access_type, "valid": True, "days_left": days_left, "label": label}
        except Exception:
            pass

    # Trial 7 дней
    try:
        reg = datetime.fromisoformat(registered_at.replace("Z", "").split(".")[0])
        trial_end = reg + timedelta(days=7)
        days_left = (trial_end - now).days
        if days_left > 0:
            return {"type": "trial", "valid": True, "days_left": days_left, "label": "Пробный период"}
    except Exception:
        pass

    return {"type": "expired", "valid": False, "days_left": 0, "label": "Истёк"}

def require_auth(f):
    def wrapper(*args, **kwargs):
        token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
        user = get_user_from_token(token)
        if not user:
            return jsonify({"error": "Необходима авторизация"}), 401
        return f(user, *args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper

# ════════════════════════════════
# AUTH
# ════════════════════════════════

@app.route("/api/register", methods=["POST"])
def register():
    data       = request.get_json() or {}
    name       = (data.get("name") or "").strip()
    email      = (data.get("email") or "").strip().lower()
    password   = data.get("password") or ""
    gender     = data.get("gender", "female")
    age        = data.get("age", 25)
    weight     = data.get("weight", 70)
    height     = data.get("height", 170)
    place      = data.get("place", "home")
    goal       = data.get("goal", "Похудеть")
    level      = data.get("level", "Новичок")
    promo_code = (data.get("promo_code") or "").strip().upper()

    if not name:
        return jsonify({"error": "Введи имя"}), 400
    if not email or "@" not in email:
        return jsonify({"error": "Некорректный email"}), 400
    if len(password) < 6:
        return jsonify({"error": "Пароль минимум 6 символов"}), 400

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id FROM users WHERE email = %s", (email,))
    existing = cur.fetchone()
    if existing:
        cur.close(); conn.close()
        return jsonify({"error": "Email уже зарегистрирован"}), 409

    # Промокод
    access_type = "trial"
    access_expires = None
    promo_label = None

    if promo_code and promo_code in PROMO_CODES:
        promo = PROMO_CODES[promo_code]
        promo_label = promo["label"]
        if promo["type"] == "forever":
            access_type = "forever"
        else:
            access_type = "promo"
            access_expires = (datetime.utcnow() + timedelta(days=promo["days"])).isoformat()

    days_per_week = int(data.get("days_per_week") or 3)
    days_per_week = max(2, min(6, days_per_week))
    hashed = hash_password(password)
    restrictions = data.get("restrictions", "")
    cur.execute("""
        INSERT INTO users (email, name, password, gender, age, weight, height,
                           place, goal, level, days_per_week, promo_code, access_type, access_expires, restrictions)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
    """, (email, name, hashed, gender, age, weight, height,
          place, goal, level, days_per_week, promo_code, access_type, access_expires, restrictions))
    conn.commit()
    user_id = cur.fetchone()["id"]

    if promo_code and promo_label:
        cur.execute("INSERT INTO promo_usage (user_id, promo_code) VALUES (%s,%s)", (user_id, promo_code))
        conn.commit()

    token = secrets.token_hex(32)
    cur.execute("INSERT INTO sessions (token, user_id) VALUES (%s,%s)", (token, user_id))
    conn.commit()

    cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
    user = dict(cur.fetchone())
    if "days_per_week" not in user or user["days_per_week"] is None:
        user["days_per_week"] = 3
    cur.close(); conn.close()

    return jsonify({
        "token":       token,
        "user":        user_to_dict(user),
        "access":      check_access(user),
        "promo_label": promo_label
    }), 201


@app.route("/api/login", methods=["POST"])
def login():
    data     = request.get_json() or {}
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"error": "Введи email и пароль"}), 400

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM users WHERE email = %s", (email,))
    user = cur.fetchone()

    if not user:
        cur.close(); conn.close()
        return jsonify({"error": "Аккаунт не найден"}), 404

    if user["password"] != hash_password(password):
        cur.close(); conn.close()
        return jsonify({"error": "Неверный пароль"}), 401

    cur.execute("UPDATE users SET last_login = %s WHERE id = %s",
                (datetime.utcnow().isoformat(), user["id"]))
    token = secrets.token_hex(32)
    cur.execute("INSERT INTO sessions (token, user_id) VALUES (%s,%s)", (token, user["id"]))
    conn.commit()

    cur.execute("SELECT * FROM users WHERE id = %s", (user["id"],))
    user = dict(cur.fetchone())
    if "days_per_week" not in user or user["days_per_week"] is None:
        user["days_per_week"] = 3

    cur.execute("SELECT DISTINCT exercise_key FROM exercise_done WHERE user_id = %s", (user["id"],))
    done = {row["exercise_key"]: True for row in cur.fetchall()}

    cur.execute("SELECT plan_data FROM workout_plans WHERE user_id = %s ORDER BY created_at DESC LIMIT 1", (user["id"],))
    plan_row = cur.fetchone()
    plan = json.loads(plan_row["plan_data"]) if plan_row else None
    cur.close(); conn.close()

    return jsonify({
        "token":  token,
        "user":   user_to_dict(user),
        "access": check_access(user),
        "done":   done,
        "plan":   plan
    })


@app.route("/api/logout", methods=["POST"])
@require_auth
def logout(user):
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM sessions WHERE token = %s", (token,))
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"ok": True})


@app.route("/api/me", methods=["GET"])
@require_auth
def me(user):
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT DISTINCT exercise_key FROM exercise_done WHERE user_id = %s", (user["id"],))
    done = {row["exercise_key"]: True for row in cur.fetchall()}
    cur.execute("SELECT plan_data FROM workout_plans WHERE user_id = %s ORDER BY created_at DESC LIMIT 1", (user["id"],))
    plan_row = cur.fetchone()
    plan = json.loads(plan_row["plan_data"]) if plan_row else None
    cur.close(); conn.close()

    return jsonify({
        "user":   user_to_dict(user),
        "access": check_access(user),
        "done":   done,
        "plan":   plan
    })

# ════════════════════════════════
# PROMO
# ════════════════════════════════

@app.route("/api/promo/check", methods=["POST"])
def check_promo():
    code = (request.get_json() or {}).get("code", "").strip().upper()
    if not code:
        return jsonify({"valid": False, "error": "Введи промокод"}), 400
    promo = PROMO_CODES.get(code)
    if promo:
        return jsonify({"valid": True, "label": promo["label"], "code": code})
    return jsonify({"valid": False, "error": "Промокод не найден"}), 404


@app.route("/api/promo/apply", methods=["POST"])
@require_auth
def apply_promo(user):
    code = (request.get_json() or {}).get("code", "").strip().upper()
    if not code:
        return jsonify({"error": "Введи промокод"}), 400

    promo = PROMO_CODES.get(code)
    if not promo:
        return jsonify({"error": "Промокод не найден"}), 404

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id FROM promo_usage WHERE user_id = %s AND promo_code = %s", (user["id"], code))
    already = cur.fetchone()
    if already:
        cur.close(); conn.close()
        return jsonify({"error": "Ты уже использовал этот промокод"}), 409

    if promo["type"] == "forever":
        access_type = "forever"; access_expires = None
    else:
        access_type = "promo"
        access_expires = (datetime.utcnow() + timedelta(days=promo["days"])).isoformat()

    cur.execute("UPDATE users SET access_type = %s, access_expires = %s, promo_code = %s WHERE id = %s",
                (access_type, access_expires, code, user["id"]))
    cur.execute("INSERT INTO promo_usage (user_id, promo_code) VALUES (%s,%s)", (user["id"], code))
    conn.commit()
    cur.execute("SELECT * FROM users WHERE id = %s", (user["id"],))
    updated = dict(cur.fetchone())
    cur.close(); conn.close()

    return jsonify({
        "ok":     True,
        "label":  promo["label"],
        "access": check_access(updated),
        "user":   user_to_dict(updated)
    })

# ════════════════════════════════
# WORKOUT
# ════════════════════════════════

@app.route("/api/workout/generate", methods=["POST"])
@require_auth
def generate_workout(user):
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "API ключ не настроен", "source": "error"}), 500

    data = request.get_json() or {}
    exercise_keys = data.get("exercise_keys", [])
    restrictions  = (data.get("restrictions") or user.get("restrictions") or "").strip()
    days_per_week = int(data.get("days_per_week") or 3)
    # Clamp to valid range
    days_per_week = max(2, min(6, days_per_week))

    # Дни недели под количество тренировок
    all_days = ["День 1","День 2","День 3","День 4","День 5","День 6","Воскресенье"]
    # Распределяем дни равномерно
    day_schedules = {
        2: ["День 1","День 4"],
        3: ["День 1","День 3","День 5"],
        4: ["День 1","День 2","День 4","День 5"],
        5: ["День 1","День 2","День 3","День 4","День 5"],
        6: ["День 1","День 2","День 3","День 4","День 5","День 6"],
    }
    training_days = day_schedules.get(days_per_week, ["День 1","День 3","День 5"])
    days_str = ", ".join(training_days)

    place_label  = "Тренажёрный зал" if user["place"] == "gym" else "Дома без оборудования"
    gender_label = "Женщина" if user["gender"] == "female" else "Мужчина"
    exercises_list = "\n".join(exercise_keys[:50])

    # Блок с ограничениями для промпта
    restrictions_block = ""
    if restrictions:
        restrictions_block = f"""
ВАЖНО — ОГРАНИЧЕНИЯ И ОСОБЕННОСТИ ПОЛЬЗОВАТЕЛЯ:
{restrictions}

Обязательно учти все перечисленные ограничения при подборе упражнений!
Избегай упражнений которые могут навредить с учётом этих ограничений.
Подбирай безопасные альтернативы."""

    variation = data.get('variation', 0)
    # Save restrictions to user profile if provided
    if restrictions:
        try:
            conn2 = get_db()
            cur2 = conn2.cursor()
            cur2.execute("UPDATE users SET restrictions = %s WHERE id = %s", (restrictions, user["id"]))
            conn2.commit()
            cur2.close(); conn2.close()
        except Exception:
            pass
    variation_text = f" [SEED:{variation}. ОБЯЗАТЕЛЬНО используй ДРУГОЙ набор упражнений чем обычно. Запрещено повторять стандартный набор. Выбери случайные упражнения из списка, не самые популярные]" if variation else ""
    prompt = f"""Ты персональный фитнес-тренер.{variation_text} Составь программу тренировок для:
Пол: {gender_label}, Возраст: {user['age']} лет, Вес: {user['weight']} кг, Рост: {user['height']} см
Место: {place_label}, Цель: {user['goal']}, Уровень: {user['level']}
Тренировок в неделю: {days_per_week} ({days_str})
{restrictions_block}
ВАЖНО: Составь программу РОВНО на {days_per_week} дней: {days_str}
Не больше и не меньше — пользователь может заниматься только {days_per_week} раза в неделю.

Используй ТОЛЬКО эти ключи упражнений:
{exercises_list}

Ответь СТРОГО только JSON без лишнего текста:
{{"plan":[{{"day":"День 1","focus":"Ноги","exercises":[{{"key":"squat_home","sets":3,"reps":"15","rest":"60с","tip":"совет"}}]}}]}}

Требования:
- Ровно {days_per_week} дней: {days_str}
- Каждый день 4-6 упражнений
- Грамотно чередуй группы мышц с учётом дней отдыха
- При {days_per_week} днях учитывай восстановление между тренировками
- Учитывай вес и уровень при подборе sets/reps
- Если есть ограничения — давай безопасные советы в поле tip
- ОБЯЗАТЕЛЬНО: для каждого упражнения добавь поле "kcal" — примерный расход калорий (целое число), рассчитай исходя из веса {user['weight']} кг, пола ({gender_label}), числа подходов и повторений
- ОБЯЗАТЕЛЬНО: для каждого дня добавь поле "kcal_total" — суммарный расход за всю тренировку дня (целое число)
Пример формата с ккал: {{"plan":[{{"day":"День 1","focus":"Ноги","kcal_total":280,"exercises":[{{"key":"squat_home","sets":3,"reps":"15","rest":"60с","tip":"совет","kcal":85}}]}}]}}"""

    try:
        response = http_requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 3000,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=30
        )
        result = response.json()
        text = result["content"][0]["text"]

        match = re.search(r'\{[\s\S]*\}', text)
        if not match:
            raise ValueError("JSON не найден")

        plan = json.loads(match.group())
        if not plan.get("plan") or not len(plan["plan"]):
            raise ValueError("Пустой план")

        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO workout_plans (user_id, plan_data) VALUES (%s,%s)",
                    (user["id"], json.dumps(plan, ensure_ascii=False)))
        conn.commit()
        cur.close(); conn.close()

        return jsonify({"plan": plan, "source": "ai"})

    except Exception as e:
        return jsonify({"error": str(e), "source": "error"}), 500


@app.route("/api/workout/save", methods=["POST"])
@require_auth
def save_workout(user):
    plan = (request.get_json() or {}).get("plan")
    if not plan:
        return jsonify({"error": "Нет данных"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO workout_plans (user_id, plan_data) VALUES (%s,%s)",
                (user["id"], json.dumps(plan, ensure_ascii=False)))
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"ok": True})

# ════════════════════════════════
# PROGRESS
# ════════════════════════════════

@app.route("/api/progress/done", methods=["POST"])
@require_auth
def mark_done(user):
    key = (request.get_json() or {}).get("key")
    if not key:
        return jsonify({"error": "Нет ключа"}), 400

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("INSERT INTO exercise_done (user_id, exercise_key) VALUES (%s,%s)", (user["id"], key))
    conn.commit()
    cur.execute("SELECT COUNT(*) as cnt FROM exercise_done WHERE user_id = %s", (user["id"],))
    total = cur.fetchone()["cnt"]
    cur.close(); conn.close()
    return jsonify({"ok": True, "total_done": total})


@app.route("/api/progress/stats", methods=["GET"])
@require_auth
def get_stats(user):
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT COUNT(*) as cnt FROM exercise_done WHERE user_id = %s", (user["id"],))
    total = cur.fetchone()["cnt"]
    cur.execute("SELECT COUNT(*) as cnt FROM exercise_done WHERE user_id = %s AND done_at >= NOW() - INTERVAL '7 days'", (user["id"],))
    week = cur.fetchone()["cnt"]
    cur.execute("SELECT DISTINCT exercise_key FROM exercise_done WHERE user_id = %s", (user["id"],))
    done_keys = cur.fetchall()
    cur.close(); conn.close()

    return jsonify({
        "total":     total,
        "week":      week,
        "done_keys": [r["exercise_key"] for r in done_keys]
    })

# ════════════════════════════════
# USER UPDATE
# ════════════════════════════════

@app.route("/api/user/update", methods=["PUT"])
@require_auth
def update_user(user):
    data    = request.get_json() or {}
    allowed = ["name", "gender", "age", "weight", "height", "place", "goal", "level", "days_per_week"]
    updates = {k: v for k, v in data.items() if k in allowed and v is not None}

    if not updates:
        return jsonify({"error": "Нечего обновлять"}), 400

    fields = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [user["id"]]

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"UPDATE users SET {fields} WHERE id = %s", values)
    conn.commit()
    cur.execute("SELECT * FROM users WHERE id = %s", (user["id"],))
    updated = dict(cur.fetchone())
    cur.close(); conn.close()

    return jsonify({"user": user_to_dict(updated), "access": check_access(updated)})

# ════════════════════════════════
# HEALTH CHECK
# ════════════════════════════════

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "app": "FitForm API", "version": "1.0.0"})


@app.route("/api/debug/prompt", methods=["GET"])
@require_auth
def debug_prompt(user):
    """Returns the prompt that would be generated for this user"""
    gender_label = "Женщина" if user["gender"] == "female" else "Мужчина"
    place_label = "Тренажёрный зал" if user["place"] == "gym" else "Дома без оборудования"
    return jsonify({
        "gender": gender_label,
        "weight": user["weight"],
        "place": place_label,
        "goal": user["goal"],
        "level": user["level"],
        "days_per_week": user["days_per_week"],
        "restrictions": user.get("restrictions", ""),
        "prompt_includes_kcal": True,
        "max_tokens": 3000
    })

@app.route("/api/health", methods=["GET"])
def api_health():
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT COUNT(*) as cnt FROM users")
        users_count = cur.fetchone()["cnt"]
        cur.close(); conn.close()
    except Exception:
        users_count = 0
    return jsonify({
        "status":     "ok",
        "users":      users_count,
        "ai_enabled": bool(ANTHROPIC_API_KEY)
    })

# ─── INIT ───
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
