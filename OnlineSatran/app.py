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


class PlayerStats(db.Model):
    user_id = db.Column(db.String(36), db.ForeignKey("user.id"), primary_key=True)
    rating = db.Column(db.Integer, nullable=False, default=1000)
    wins = db.Column(db.Integer, nullable=False, default=0)
    losses = db.Column(db.Integer, nullable=False, default=0)
    draws = db.Column(db.Integer, nullable=False, default=0)
    games = db.Column(db.Integer, nullable=False, default=0)


class FriendRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.String(36), db.ForeignKey("user.id"), nullable=False, index=True)
    receiver_id = db.Column(db.String(36), db.ForeignKey("user.id"), nullable=False, index=True)
    status = db.Column(db.String(12), nullable=False, default="pending")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    __table_args__ = (db.UniqueConstraint("sender_id", "receiver_id", name="uq_friend_pair"),)


class GameRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    white_id = db.Column(db.String(36), db.ForeignKey("user.id"), nullable=False, index=True)
    black_id = db.Column(db.String(36), db.ForeignKey("user.id"), nullable=False, index=True)
    result = db.Column(db.String(12), nullable=False)
    moves_json = db.Column(db.Text, nullable=False, default="[]")
    white_rating_change = db.Column(db.Integer, nullable=False, default=0)
    black_rating_change = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


def ensure_player_stats(user_id):
    stats = db.session.get(PlayerStats, user_id)
    if not stats:
        stats = PlayerStats(user_id=user_id)
        db.session.add(stats)
        db.session.flush()
    return stats


def finish_record(game, result):
    if game.get("recorded") or not game["players"].get("white") or not game["players"].get("black"):
        return
    white_id, black_id = game["players"]["white"], game["players"]["black"]
    if white_id == black_id:
        return
    white = ensure_player_stats(white_id)
    black = ensure_player_stats(black_id)
    expected_white = 1 / (1 + 10 ** ((black.rating - white.rating) / 400))
    score_white = 1.0 if result == "white" else 0.0 if result == "black" else 0.5
    change_white = round(32 * (score_white - expected_white))
    change_black = -change_white
    white.rating = max(100, white.rating + change_white)
    black.rating = max(100, black.rating + change_black)
    white.games += 1
    black.games += 1
    if result == "draw":
        white.draws += 1
        black.draws += 1
    elif result == "white":
        white.wins += 1
        black.losses += 1
    else:
        black.wins += 1
        white.losses += 1
    db.session.add(GameRecord(
        white_id=white_id, black_id=black_id, result=result,
        moves_json=json.dumps(game["moves"], ensure_ascii=False),
        white_rating_change=change_white, black_rating_change=change_black
    ))
    db.session.commit()
    game["recorded"] = True


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


@app.get("/api/stats")
@login_required_api
def player_stats_api():
    user = current_user()
    stats = ensure_player_stats(user.id)
    db.session.commit()
    return jsonify(ok=True, stats={
        "rating": stats.rating, "wins": stats.wins, "losses": stats.losses,
        "draws": stats.draws, "games": stats.games,
        "win_rate": round((stats.wins / stats.games) * 100, 1) if stats.games else 0
    })


@app.get("/api/leaderboard")
def leaderboard_api():
    rows = db.session.query(PlayerStats, User).join(User, User.id == PlayerStats.user_id).order_by(PlayerStats.rating.desc(), PlayerStats.wins.desc()).limit(50).all()
    return jsonify(ok=True, players=[{
        "username": user.display_name or user.username, "rating": stats.rating,
        "wins": stats.wins, "games": stats.games
    } for stats, user in rows])


@app.get("/api/friends")
@login_required_api
def friends_api():
    user = current_user()
    requests_in = FriendRequest.query.filter_by(receiver_id=user.id, status="pending").all()
    accepted = FriendRequest.query.filter(
        db.or_(
            db.and_(FriendRequest.sender_id == user.id, FriendRequest.status == "accepted"),
            db.and_(FriendRequest.receiver_id == user.id, FriendRequest.status == "accepted")
        )
    ).all()
    def public_friend_request(item, other_id):
        other = db.session.get(User, other_id)
        return {"id": item.id, "user_id": other.id, "username": other.display_name or other.username}
    return jsonify(ok=True,
        incoming=[public_friend_request(x, x.sender_id) for x in requests_in],
        friends=[public_friend_request(x, x.receiver_id if x.sender_id == user.id else x.sender_id) for x in accepted])


@app.post("/api/friends/request")
@login_required_api
def friend_request_api():
    user = current_user()
    name = str((request.get_json(silent=True) or {}).get("username", "")).strip()
    other = User.query.filter(User.username.ilike(name)).first()
    if not other or other.id == user.id:
        return jsonify(ok=False, error="Kullanıcı bulunamadı."), 404
    existing = FriendRequest.query.filter(
        db.or_(
            db.and_(FriendRequest.sender_id == user.id, FriendRequest.receiver_id == other.id),
            db.and_(FriendRequest.sender_id == other.id, FriendRequest.receiver_id == user.id)
        )
    ).first()
    if existing:
        return jsonify(ok=False, error="Bu kullanıcıya zaten istek gönderilmiş veya arkadaşsınız."), 409
    db.session.add(FriendRequest(sender_id=user.id, receiver_id=other.id))
    db.session.commit()
    return jsonify(ok=True)


@app.post("/api/friends/<int:request_id>/accept")
@login_required_api
def accept_friend_api(request_id):
    item = db.session.get(FriendRequest, request_id)
    if not item or item.receiver_id != current_user().id or item.status != "pending":
        return jsonify(ok=False, error="İstek bulunamadı."), 404
    item.status = "accepted"
    db.session.commit()
    return jsonify(ok=True)


@app.get("/api/history")
@login_required_api
def history_api():
    user = current_user()
    rows = GameRecord.query.filter(db.or_(GameRecord.white_id == user.id, GameRecord.black_id == user.id)).order_by(GameRecord.created_at.desc()).limit(50).all()
    output = []
    for row in rows:
        white = db.session.get(User, row.white_id)
        black = db.session.get(User, row.black_id)
        output.append({
            "white": white.display_name or white.username,
            "black": black.display_name or black.username,
            "result": row.result, "moves": json.loads(row.moves_json),
            "date": row.created_at.strftime("%d.%m.%Y %H:%M"),
            "my_color": "white" if row.white_id == user.id else "black"
        })
    return jsonify(ok=True, games=output)


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
    status = game_status(board)
    if status["finished"] and not game.get("recorded"):
        result = "draw" if board.is_stalemate() or board.is_insufficient_material() or board.is_fivefold_repetition() or board.is_seventyfive_moves() else ("black" if board.turn else "white")
        finish_record(game, result)
    socketio.emit("game_state", {
        "fen": board.fen(), "turn": "white" if board.turn else "black",
        "moves": game["moves"], "status": status,
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
        "moves": [], "recorded": False, "chat": []
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


@socketio.on("chat_message")
def chat_message(data):
    data = data or {}
    code = str(data.get("code", "")).upper()
    message = str(data.get("message", "")).strip()
    user = current_user()
    if not user or not user.verified or code not in rooms:
        emit("error_message", "Sohbet için giriş yapmalı ve oyunda olmalısın.")
        return
    game = rooms[code]
    if user.id not in game["players"].values() or game["sids"].get("white") != request.sid and game["sids"].get("black") != request.sid:
        emit("error_message", "Bu odanın sohbetine erişimin yok.")
        return
    if not message or len(message) > 300:
        emit("error_message", "Mesaj 1-300 karakter olmalı.")
        return
    socketio.emit("chat_message", {
        "username": user.display_name or user.username,
        "message": message,
        "time": datetime.utcnow().strftime("%H:%M")
    }, to=code)


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
