
import os
import random
import string

import chess
from flask import Flask, render_template
from flask_socketio import SocketIO, join_room
from flask_socketio import emit
from flask import request

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "satranc-secret")

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading"
)

rooms = {}
sid_to_game = {}


def new_code():
    while True:
        code = "".join(
            random.choices(string.ascii_uppercase + string.digits, k=6)
        )
        if code not in rooms:
            return code


def game_status(board):
    if board.is_checkmate():
        return {
            "finished": True,
            "text": "ŞAH MAT! " + (
                "Siyah kazandı." if board.turn else "Beyaz kazandı."
            )
        }

    if board.is_stalemate():
        return {"finished": True, "text": "PAT! Berabere."}

    if board.is_insufficient_material():
        return {"finished": True, "text": "Yetersiz taş. Berabere."}

    if board.can_claim_draw():
        return {
            "finished": False,
            "text": "Beraberlik talep edilebilir."
        }

    if board.is_check():
        return {
            "finished": False,
            "text": "ŞAH! " + ("Beyaz" if board.turn else "Siyah") + " oynuyor."
        }

    return {
        "finished": False,
        "text": ("Beyaz" if board.turn else "Siyah") + " oynuyor."
    }


def send_state(code, last_move=None):
    game = rooms[code]
    board = game["board"]

    socketio.emit("game_state", {
        "fen": board.fen(),
        "turn": "white" if board.turn else "black",
        "moves": game["moves"],
        "status": game_status(board),
        "players": {
            "white": bool(game["players"]["white"]),
            "black": bool(game["players"]["black"])
        },
        "last_move": last_move
    }, to=code)


@app.route("/")
def index():
    return render_template("index.html")


@socketio.on("create_room")
def create_room(data):
    player_id = str(data.get("player_id", ""))

    if not player_id or len(player_id) > 100:
        emit("error_message", "Oyuncu kimliği geçersiz.")
        return

    code = new_code()

    rooms[code] = {
        "board": chess.Board(),
        "players": {
            "white": player_id,
            "black": None
        },
        "sids": {
            "white": request.sid,
            "black": None
        },
        "moves": []
    }

    sid_to_game[request.sid] = code
    join_room(code)

    emit("room_created", {
        "code": code,
        "color": "white"
    })

    send_state(code)


@socketio.on("join_game")
def join_game(data):
    code = str(data.get("code", "")).upper().strip()
    player_id = str(data.get("player_id", ""))

    if not player_id or len(player_id) > 100:
        emit("error_message", "Oyuncu kimliği geçersiz.")
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

    emit("room_joined", {
        "code": code,
        "color": color
    })

    send_state(code)


@socketio.on("make_move")
def make_move(data):
    code = str(data.get("code", "")).upper()
    player_id = str(data.get("player_id", ""))
    uci = str(data.get("uci", ""))

    if code not in rooms:
        emit("error_message", "Oda bulunamadı.")
        return

    game = rooms[code]
    board = game["board"]

    if game["sids"].get("white") != request.sid and game["sids"].get("black") != request.sid:
        emit("error_message", "Bu oyuna bağlı değilsin.")
        return

    if player_id not in game["players"].values():
        emit("error_message", "Oyuncu doğrulanamadı.")
        return

    color = (
        "white" if player_id == game["players"]["white"]
        else "black"
    )

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

    socketio.emit(
        "player_disconnected",
        {"message": "Bir oyuncunun bağlantısı kesildi."},
        to=code
    )


@socketio.on("rejoin_game")
def rejoin_game(data):
    code = str(data.get("code", "")).upper()
    player_id = str(data.get("player_id", ""))

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

    emit("room_joined", {
        "code": code,
        "color": color
    })

    send_state(code)


if __name__ == "__main__":
    socketio.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=False
    )
