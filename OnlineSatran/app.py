import os
import random
import string
import secrets
import json
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from functools import wraps

import chess
from flask import Flask, render_template, request, jsonify, session
from flask_socketio import SocketIO, join_room, emit
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", secrets.token_hex(32))
database_url = os.environ.get("DATABASE_URL", "sqlite:///satranc.db")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 3 * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

rooms = {}
sid_to_game = {}


class User(db.Model):
    id = db.Column(db.String(36), primary_key=True)
    username = db.Column(db.String(24), unique=True, nullable=False, index=True)
    email = db.Column(db.String(254), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    verified = db.Column(db.Boolean, default=False, nullable=False)
    verification_code = db.Column(db.String(6))
    verification_expires = db.Column(db.DateTime)
    display_name = db.Column(db.String(32), default="")
    bio = db.Column(db.String(160), default="")
    avatar_color = db.Column(db.String(7), default="#eab308")
    avatar_image = db.Column(db.LargeBinary)
    avatar_mime = db.Column(db.String(40))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


with app.app_context():
    db.create_all()


def send_email(to_address, subject, body):
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    from_address = os.environ.get("RESEND_FROM", "").strip()
    if not api_key or not from_address:
        raise RuntimeError("Resend e-posta ayarları eksik.")

    payload = json.dumps({
        "from": from_address,
        "to": [to_address],
        "subject": subject,
        "text": body
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Resend e-posta API hatası: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("Resend e-posta servisine bağlanılamadı.") from exc


def current_user():
    user_id = session.get("user_id")
    return db.session.get(User, user_id) if user_id else None


def login_required_api(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user or not user.verified:
            return jsonify({"ok": False, "error": "Giriş yapmalısın veya hesabın devre dışı."}), 401
        return fn(*args, **kwargs)
    return wrapper


def user_public(user):
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name or user.username,
        "bio": user.bio or "",
        "avatar_color": user.avatar_color or "#eab308",
        "has_avatar": bool(user.avatar_image),
        "created_at": user.created_at.strftime("%d.%m.%Y"),
        "is_admin": (bool(os.environ.get("ADMIN_USERNAME", "").strip()) and user.username.lower() == os.environ.get("ADMIN_USERNAME", "").strip().lower()) or (bool(os.environ.get("ADMIN_EMAIL", "").strip()) and user.email.lower() == os.environ.get("ADMIN_EMAIL", "").strip().lower())
    }


@app.route("/")
def index():
    user = current_user()
    return render_template("index.html", player_id=user.id if user else None)


@app.post("/api/register")
def register():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    if not 3 <= len(username) <= 24 or not all(c.isalnum() or c == "_" for c in username):
        return jsonify(ok=False, error="Kullanıcı adı 3-24 karakter olmalı; harf, rakam ve _ kullanılabilir."), 400
    if len(password) < 8:
        return jsonify(ok=False, error="Şifre en az 8 karakter olmalı."), 400
    if User.query.filter(User.username.ilike(username)).first():
        return jsonify(ok=False, error="Bu kullanıcı adı zaten kayıtlı."), 409

    # Eski veritabanı şemasındaki zorunlu e-posta sütunuyla uyumluluk için
    # kullanıcıya görünmeyen, teslim edilemeyen dahili bir adres tutulur.
    internal_email = username.lower() + "@no-email.invalid"
    user = User(
        id=secrets.token_hex(16),
        username=username,
        email=internal_email,
        password_hash=generate_password_hash(password),
        verified=True
    )
    db.session.add(user)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify(ok=False, error="Hesap oluşturulamadı. Kullanıcı adını kontrol et."), 409

    session.clear()
    session["user_id"] = user.id
    return jsonify(ok=True, message="Hesabın oluşturuldu.", user=user_public(user))


@app.post("/api/login")
def login():
    data = request.get_json(silent=True) or {}
    identity = str(data.get("identity", "")).strip()
    password = str(data.get("password", ""))
    user = User.query.filter(User.username.ilike(identity)).first()
    if not user or not check_password_hash(user.password_hash, password):
        return jsonify(ok=False, error="Kullanıcı adı veya şifre hatalı."), 401
    if not user.verified:
        return jsonify(ok=False, error="Bu hesap devre dışı bırakılmış."), 403
    session.clear()
    session["user_id"] = user.id
    return jsonify(ok=True, user=user_public(user))


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/me")
@login_required_api
def me():
    return jsonify(ok=True, user=user_public(current_user()))


@app.post("/api/profile")
@login_required_api
def update_profile():
    user = current_user()
    data = request.get_json(silent=True) or {}
    display_name = str(data.get("display_name", user.username)).strip()
    bio = str(data.get("bio", "")).strip()
    color = str(data.get("avatar_color", "#eab308"))
    if not 1 <= len(display_name) <= 32 or len(bio) > 160:
        return jsonify(ok=False, error="Görünen ad 1-32, biyografi en fazla 160 karakter olmalı."), 400
    if len(color) != 7 or not color.startswith("#") or any(c not in "0123456789abcdefABCDEF" for c in color[1:]):
        return jsonify(ok=False, error="Renk kodu geçersiz."), 400
    user.display_name, user.bio, user.avatar_color = display_name, bio, color
    db.session.commit()
    return jsonify(ok=True, user=user_public(user))


@app.post("/api/profile/avatar")
@login_required_api
def upload_avatar():
    user = current_user()
    file = request.files.get("avatar")
    allowed = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
    if not file or file.mimetype not in allowed:
        return jsonify(ok=False, error="PNG, JPG, WEBP veya GIF görseli seç."), 400
    content = file.read(2 * 1024 * 1024 + 1)
    if not content or len(content) > 2 * 1024 * 1024:
        return jsonify(ok=False, error="Görsel en fazla 2 MB olabilir."), 400
    user.avatar_image = content
    user.avatar_mime = file.mimetype
    db.session.commit()
    return jsonify(ok=True)


@app.get("/api/avatar/<user_id>")
def avatar(user_id):
    from flask import Response
    user = db.session.get(User, user_id)
    if not user or not user.avatar_image:
        return "", 404
    return Response(user.avatar_image, mimetype=user.avatar_mime or "image/png", headers={"Cache-Control": "no-store"})


@app.get("/api/admin/users")
@login_required_api
def admin_users():
    user = current_user()
    admin_email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
    if not ((os.environ.get("ADMIN_USERNAME", "").strip() and user.username.lower() == os.environ.get("ADMIN_USERNAME", "").strip().lower()) or (admin_email and user.email.lower() == admin_email)):
        return jsonify(ok=False, error="Bu sayfa yalnızca yöneticiye açık."), 403
    users = User.query.order_by(User.created_at.desc()).all()
    return jsonify(ok=True, users=[user_public(u) for u in users])


@app.post("/api/admin/user/<user_id>/disable")
@login_required_api
def admin_disable_user(user_id):
    admin = current_user()
    admin_email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
    if not ((os.environ.get("ADMIN_USERNAME", "").strip() and admin.username.lower() == os.environ.get("ADMIN_USERNAME", "").strip().lower()) or (admin_email and admin.email.lower() == admin_email)):
        return jsonify(ok=False, error="Yetkin yok."), 403
    target = db.session.get(User, user_id)
    if not target or target.id == admin.id:
        return jsonify(ok=False, error="Hesap bulunamadı veya bu işlem yapılamaz."), 400
    target.verified = False
    db.session.commit()
    return jsonify(ok=True)


def new_code():
    while True:
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        if code not in rooms:
            return code


def game_status(board):
    if board.is_checkmate():
        return {"finished": True, "text": "ŞAH MAT! " + ("Siyah kazandı." if board.turn else "Beyaz kazandı.")}
    if board.is_stalemate():
        return {"finished": True, "text": "PAT! Berabere."}
    if board.is_insufficient_material():
        return {"finished": True, "text": "Yetersiz taş. Berabere."}
    if board.can_claim_draw():
        return {"finished": False, "text": "Beraberlik talep edilebilir."}
    if board.is_check():
        return {"finished": False, "text": "ŞAH! " + ("Beyaz" if board.turn else "Siyah") + " oynuyor."}
    return {"finished": False, "text": ("Beyaz" if board.turn else "Siyah") + " oynuyor."}


def send_state(code, last_move=None):
    game = rooms[code]
    board = game["board"]
    socketio.emit("game_state", {
        "fen": board.fen(), "turn": "white" if board.turn else "black",
        "moves": game["moves"], "status": game_status(board),
        "players": {"white": bool(game["players"]["white"]), "black": bool(game["players"]["black"])},
        "last_move": last_move
    }, to=code)


def socket_player_ok(player_id):
    user = current_user()
    return bool(user and user.verified and str(player_id) == user.id)


@socketio.on("create_room")
def create_room(data):
    player_id = str((data or {}).get("player_id", ""))
    if not socket_player_ok(player_id):
        emit("error_message", "Oynamak için hesabına giriş yapmalısın.")
        return
    code = new_code()
    rooms[code] = {
        "board": chess.Board(),
        "players": {"white": player_id, "black": None},
        "sids": {"white": request.sid, "black": None},
        "moves": []
    }
    sid_to_game[request.sid] = code
    join_room(code)
    emit("room_created", {"code": code, "color": "white"})
    send_state(code)


@socketio.on("join_game")
def join_game(data):
    data = data or {}
    code = str(data.get("code", "")).upper().strip()
    player_id = str(data.get("player_id", ""))
    if not socket_player_ok(player_id):
        emit("error_message", "Oynamak için hesabına giriş yapmalısın.")
        return
    if code not in rooms:
        emit("error_message", "Böyle bir oda bulunamadı.")
        return
    game = rooms[code]
    if player_id == game["players"]["white"]:
        color = "white"
    elif player_id == game["players"]["black"]:
        color = "black"
    elif game["players"]["black"] is None:
        color = "black"
        game["players"]["black"] = player_id
    else:
        emit("error_message", "Bu oda dolu.")
        return
    old_sid = game["sids"][color]
    if old_sid:
        sid_to_game.pop(old_sid, None)
    game["sids"][color] = request.sid
    sid_to_game[request.sid] = code
    join_room(code)
    emit("room_joined", {"code": code, "color": color})
    send_state(code)


@socketio.on("make_move")
def make_move(data):
    data = data or {}
    code = str(data.get("code", "")).upper()
    player_id = str(data.get("player_id", ""))
    uci = str(data.get("uci", ""))
    if code not in rooms:
        emit("error_message", "Oda bulunamadı.")
        return
    game = rooms[code]
    board = game["board"]
    if not socket_player_ok(player_id):
        emit("error_message", "Hesap doğrulanamadı.")
        return
    if game["sids"].get("white") != request.sid and game["sids"].get("black") != request.sid:
        emit("error_message", "Bu oyuna bağlı değilsin.")
        return
    if player_id not in game["players"].values():
        emit("error_message", "Oyuncu doğrulanamadı.")
        return
    color = "white" if player_id == game["players"]["white"] else "black"
    if color != ("white" if board.turn else "black"):
        emit("error_message", "Sıra sende değil.")
        return
    try:
        move = chess.Move.from_uci(uci)
    except ValueError:
        emit("error_message", "Hamle geçersiz.")
        return
    if move not in board.legal_moves:
        emit("error_message", "Geçersiz hamle.")
        return
    san = board.san(move)
    board.push(move)
    game["moves"].append(san)
    send_state(code, san)


@socketio.on("disconnect")
def disconnect():
    code = sid_to_game.pop(request.sid, None)
    if not code or code not in rooms:
        return
    game = rooms[code]
    for color in ("white", "black"):
        if game["sids"][color] == request.sid:
            game["sids"][color] = None
    socketio.emit("player_disconnected", {"message": "Bir oyuncunun bağlantısı kesildi."}, to=code)


@socketio.on("rejoin_game")
def rejoin_game(data):
    data = data or {}
    code = str(data.get("code", "")).upper()
    player_id = str(data.get("player_id", ""))
    if not socket_player_ok(player_id):
        emit("error_message", "Hesabına giriş yapmalısın.")
        return
    if code not in rooms:
        emit("error_message", "Oda artık mevcut değil.")
        return
    game = rooms[code]
    if player_id == game["players"]["white"]:
        color = "white"
    elif player_id == game["players"]["black"]:
        color = "black"
    else:
        emit("error_message", "Bu odaya erişimin yok.")
        return
    old_sid = game["sids"][color]
    if old_sid:
        sid_to_game.pop(old_sid, None)
    game["sids"][color] = request.sid
    sid_to_game[request.sid] = code
    join_room(code)
    emit("room_joined", {"code": code, "color": color})
    send_state(code)


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
