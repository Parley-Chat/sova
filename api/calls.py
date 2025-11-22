from flask import Blueprint, request, jsonify
from .utils import make_json_error, logged_in, sliding_window_rate_limiter, timestamp, validate_request_data
from .stream import call_start, call_join, call_left, call_signal
from utils import config
from db import SQLite

calls_bp=Blueprint("calls", __name__)

@calls_bp.route("/channel/<string:channel_id>/call", methods=["POST"])
@logged_in()
@sliding_window_rate_limiter(limit=30, window=60, user_limit=15)
def start_or_join_call(db:SQLite, id, channel_id):
    if not config["calls"]["enabled"]: return make_json_error(403, "Calls are disabled")
    member_channel_data=db.execute_raw_sql("""
        SELECT c.type FROM channels c
        JOIN members m ON c.id=m.channel_id
        WHERE m.user_id=? AND m.channel_id=?
    """, (id, channel_id))
    if not member_channel_data: return make_json_error(404, "Channel not found")
    if member_channel_data[0]["type"]!=1: return make_json_error(400, "Calls are only supported in DM channels")
    other_member=db.execute_raw_sql("SELECT user_id FROM members WHERE channel_id=? AND user_id!=?", (channel_id, id))
    if other_member and db.exists("blocks", {"blocker_id": other_member[0]["user_id"], "blocked_id": id}): return make_json_error(403, "You are blocked by this user")
    active_calls=db.execute_raw_sql("""
        SELECT cp.channel_id FROM call_participants cp
        WHERE cp.user_id=? AND cp.left_at IS NULL AND cp.channel_id!=?
    """, (id, channel_id))
    if active_calls:
        for active_call in active_calls:
            db.update_data("call_participants", {"left_at": timestamp(True)}, {"channel_id": active_call["channel_id"], "user_id": id})
            user_data_leave=db.execute_raw_sql("SELECT username, display_name, pfp FROM users WHERE id=?", (id,))[0]
            call_left(active_call["channel_id"], user_data_leave, db)
            remaining_participants=db.execute_raw_sql("SELECT COUNT(*) as count FROM call_participants WHERE channel_id=? AND left_at IS NULL", (active_call["channel_id"],))
            if remaining_participants[0]["count"]==0:
                db.delete_data("calls", {"channel_id": active_call["channel_id"]})
    existing_call=db.select_data("calls", ["started_by", "started_at"], {"channel_id": channel_id})
    if existing_call:
        participant=db.select_data("call_participants", ["left_at"], {"channel_id": channel_id, "user_id": id})
        if participant and participant[0]["left_at"] is None: return make_json_error(400, "You are already in this call")
        if participant:
            db.update_data("call_participants", {"joined_at": timestamp(True), "left_at": None}, {"channel_id": channel_id, "user_id": id})
        else:
            db.insert_data("call_participants", {"channel_id": channel_id, "user_id": id, "joined_at": timestamp(True)})
        user_data=db.execute_raw_sql("SELECT username, display_name, pfp FROM users WHERE id=?", (id,))[0]
        call_join(channel_id, user_data, db)
        return jsonify({"success": True, "joined": True})
    db.insert_data("calls", {"channel_id": channel_id, "started_by": id, "started_at": timestamp(True)})
    db.insert_data("call_participants", {"channel_id": channel_id, "user_id": id, "joined_at": timestamp(True)})
    user_data=db.execute_raw_sql("SELECT username FROM users WHERE id=?", (id,))[0]
    call_start(channel_id, user_data["username"], db)
    return jsonify({"success": True, "started": True}), 201

@calls_bp.route("/channel/<string:channel_id>/call", methods=["DELETE"])
@logged_in()
@sliding_window_rate_limiter(limit=30, window=60, user_limit=15)
def leave_call(db:SQLite, id, channel_id):
    if not db.exists("members", {"user_id": id, "channel_id": channel_id}): return make_json_error(404, "Channel not found")
    participant=db.select_data("call_participants", ["left_at"], {"channel_id": channel_id, "user_id": id})
    if not participant: return make_json_error(404, "You are not in this call")
    if participant[0]["left_at"] is not None: return make_json_error(400, "You already left this call")
    db.update_data("call_participants", {"left_at": timestamp(True)}, {"channel_id": channel_id, "user_id": id})
    user_data=db.execute_raw_sql("SELECT username, display_name, pfp FROM users WHERE id=?", (id,))[0]
    call_left(channel_id, user_data, db)
    active_participants=db.execute_raw_sql("SELECT COUNT(*) as count FROM call_participants WHERE channel_id=? AND left_at IS NULL", (channel_id,))
    if active_participants[0]["count"]==0:
        db.delete_data("calls", {"channel_id": channel_id})
    return jsonify({"success": True})

@calls_bp.route("/channel/<string:channel_id>/call/signal", methods=["POST"])
@logged_in()
@sliding_window_rate_limiter(limit=500, window=60, user_limit=250)
@validate_request_data({"type": {}, "data": {}}, source="json")
def signal_call(db:SQLite, id, channel_id):
    if not db.exists("members", {"user_id": id, "channel_id": channel_id}): return make_json_error(404, "Channel not found")
    participant=db.select_data("call_participants", ["left_at"], {"channel_id": channel_id, "user_id": id})
    if not participant or participant[0]["left_at"] is not None: return make_json_error(403, "You are not in this call")
    signal_type=request.json.get("type")
    signal_data=request.json.get("data")
    if signal_type not in ["offer", "answer", "ice"]: return make_json_error(400, "Invalid signal type")
    call_signal(channel_id, id, signal_type, signal_data, db)
    return jsonify({"success": True})

@calls_bp.route("/channel/<string:channel_id>/call", methods=["GET"])
@logged_in()
@sliding_window_rate_limiter(limit=50, window=60, user_limit=25)
def get_call_status(db:SQLite, id, channel_id):
    if not db.exists("members", {"user_id": id, "channel_id": channel_id}): return make_json_error(404, "Channel not found")
    call_data=db.select_data("calls", ["started_by", "started_at"], {"channel_id": channel_id})
    if not call_data: return jsonify({"active": False})
    starter_data=db.execute_raw_sql("SELECT username FROM users WHERE id=?", (call_data[0]["started_by"],))[0]
    participants=db.execute_raw_sql("""
        SELECT u.username, u.display_name, u.pfp, cp.joined_at
        FROM call_participants cp
        JOIN users u ON cp.user_id=u.id
        WHERE cp.channel_id=? AND cp.left_at IS NULL
    """, (channel_id,))
    answered=len(participants)>=2
    return jsonify({"active": True, "answered": answered, "started_by": starter_data["username"], "started_at": call_data[0]["started_at"], "participants": [{"username": p["username"], "display": p["display_name"], "pfp": p["pfp"], "joined_at": p["joined_at"]} for p in participants]})
