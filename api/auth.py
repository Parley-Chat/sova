from flask import Blueprint, request, jsonify
from utils import version, dev_mode
from .utils import (
    make_json_error, logged_in, pass_db, validate_request_data, sliding_window_rate_limiter,
    public_key_open, get_challenge, timestamp, challenges, challenges_lock,
    regex_first_group_encrypted, browser_regex, device_regex, rsa_encrypt,
    get_channel_last_message_seq, hash_token
)
from utils import generate
from db import SQLite
import bcrypt
import re
from utils import config, RED, colored_log
from .stream import channel_added, member_join

auth_bp=Blueprint("auth", __name__)

@auth_bp.route("/")
def index(): return {"running": "Parley", "version": version,
  "max_file_size": config["max_file_size"], "messages": config["messages"],
  "disable_channel_creation": config["instance"]["disable_channel_creation"],
  "disable_channel_deletion": config["instance"]["disable_channel_deletion"],
  "max_channels": config["max_members"]["max_channels"],
  **({"dev": True} if dev_mode else {})}, 200

def join_invite(db, id, invite_code):
    channel_data=db.select_data("channels", ["id", "type"], {"invite_code": invite_code})
    if not channel_data: return "Instance invite not found"
    channel_id=channel_data[0]["id"]
    if db.exists("members", {"user_id": id, "channel_id": channel_id}): return "User is already a member of instance invite channel"
    if db.exists("bans", {"user_id": id, "channel_id": channel_id}): return "User is banned from instance invite channel"
    user_channel_count=db.execute_raw_sql("SELECT COUNT(*) as count FROM members WHERE user_id=? AND hidden IS NULL", (id,))[0]["count"]
    if user_channel_count>=config["max_members"]["max_channels"]: return "User has reached the maximum number of channels"
    channel_type=channel_data[0]["type"]
    if channel_type!=3:
        member_count=db.execute_raw_sql("SELECT COUNT(*) as count FROM members WHERE channel_id=?", (channel_id,))[0]["count"]
        if member_count>=config["max_members"]["encrypted_channels"]: return "Instance invite channel has reached maximum member limit"
    db.insert_data("members", {"user_id": id, "channel_id": channel_id, "joined_at": timestamp(), "message_seq": 0 if channel_type==3 else get_channel_last_message_seq(db, channel_id)})

    # Get user data and emit member join event
    user_data=db.select_data("users", ["id", "username", "display_name", "pfp"], {"id": id})[0]
    member_join(channel_id, user_data, db)

    # Get channel data and emit channel_added event
    full_channel_data=db.execute_raw_sql("SELECT c.id, c.name, c.pfp, c.type, c.permissions, COUNT(m.user_id) as member_count FROM channels c LEFT JOIN members m ON c.id=m.channel_id WHERE c.id=? GROUP BY c.id", (channel_id,))[0]
    channel_added(id, full_channel_data, db)

@auth_bp.route("/solve", methods=["POST"])
@sliding_window_rate_limiter(limit=20, window=60, user_limit=10)
@validate_request_data({"id": {"len": 20}, "solve": {"len": 20}})
def solve():
    with challenges_lock:
        if request.form["id"] not in challenges: return make_json_error(400, "Invalid challenge ID")
        if challenges[request.form["id"]]["expire"]<timestamp():
            del challenges[request.form["id"]]
            return make_json_error(400, "Invalid challenge ID")
        hashed_challenge=challenges[request.form["id"]]["hashed"]
        logged_in_at=challenges[request.form["id"]].get("logged_in_at")
        new="new" in challenges[request.form["id"]]
        reset_keys="reset_keys" in challenges[request.form["id"]]
        reset_passkey="reset_passkey" in challenges[request.form["id"]]
        if new:
            public_key_text=challenges[request.form["id"]]["public"]
            username=challenges[request.form["id"]]["username"]
        elif reset_keys:
            user_id=challenges[request.form["id"]]["user_id"]
            public_key_text=challenges[request.form["id"]]["public"]
        elif reset_passkey:
            user_id=challenges[request.form["id"]]["user_id"]
        else: id=challenges[request.form["id"]]["id"]
        del challenges[request.form["id"]]
    if not bcrypt.checkpw(request.form["solve"].encode(), hashed_challenge.encode()): return make_json_error(401, "Challenge failed")
    db=SQLite()
    if new:
        public_key, error_resp=public_key_open(public_key_text)
        if error_resp: return error_resp
        id=generate()
        passkey=generate()
        hashed_passkey=bcrypt.hashpw(passkey.encode(), bcrypt.gensalt()).decode()
        try:
            db.insert_data("users", {"id": id, "username": username, "passkey": hashed_passkey, "public_key": public_key_text, "created_at": timestamp()})
        except Exception as e:
            if "UNIQUE constraint failed" in str(e): return make_json_error(400, "Username is in use")
            raise
        if config["instance"]["invite"]:
            invite_error=join_invite(db, id, config["instance"]["invite"])
            if invite_error: colored_log(RED, "ERROR", invite_error)
    elif reset_keys:
        public_key, error_resp=public_key_open(public_key_text)
        if error_resp: return error_resp
        id=user_id
        db.update_data("users", {"public_key": public_key_text}, {"id": user_id})
        db.delete_data("session", {"user": user_id})
    elif reset_passkey:
        new_passkey=generate()
        hashed_passkey=bcrypt.hashpw(new_passkey.encode(), bcrypt.gensalt()).decode()
        db.update_data("users", {"passkey": hashed_passkey}, {"id": user_id})
        user_public=db.select_data("users", ["public_key"], {"id": user_id})[0]["public_key"]
        public_key, error_resp=public_key_open(user_public)
        if error_resp: return error_resp
        id=user_id
    else:
        public_key=db.select_data("users", ["public_key"], {"id": id})
        if not public_key: return make_json_error(400, "User not found")
        public_key, error_resp=public_key_open(public_key[0]["public_key"])
        if error_resp: return error_resp
    if not reset_passkey:
        if "User-Agent" in request.headers:
            browser=regex_first_group_encrypted(browser_regex.search(request.headers["User-Agent"]), public_key)
            device=regex_first_group_encrypted(device_regex.search(request.headers["User-Agent"]), public_key)
        else: browser=device=None
        session=generate()
        db.insert_data("session", {"user": id, "token_hash": hash_token(session), "id": generate(), "browser": browser, "device": device, "logged_in_at": logged_in_at or timestamp(), "next_challenge": timestamp()+3600})
    db.close()
    if reset_passkey: return jsonify({"passkey": new_passkey, "success": True})
    return jsonify({"session": session, "success": True, **(({"passkey": passkey} if new else {}))})

@auth_bp.route("/username_check")
@sliding_window_rate_limiter(limit=50, window=60, user_limit=20)
@validate_request_data({"username": {"minlen": 3, "maxlen": 20, "regex": re.compile(r"[a-z0-9_\-]+")}}, source="args")
@pass_db
def username_check(db:SQLite): return make_json_error(400, "Username is in use") if db.exists("users", {"username": request.args["username"]}) else jsonify({"success": True})

@auth_bp.route("/signup", methods=["POST"])
@sliding_window_rate_limiter(limit=15, window=300, user_limit=5)
@validate_request_data({"username": {"minlen": 3, "maxlen": 20, "regex": re.compile(r"[a-z0-9_\-]+")}, "public": {"len": 392}}, 401)
@pass_db
def signup(db:SQLite):
    if db.exists("users", {"username": request.form["username"]}): return make_json_error(400, "Username is in use")
    db.close()
    public_key, error_resp=public_key_open()
    if error_resp: return error_resp
    id, challenge_hash, challenge_enc=get_challenge(public_key)
    with challenges_lock: challenges[id]={"new": True, "username": request.form["username"], "hashed": challenge_hash, "expire": timestamp()+60, "public": request.form["public"]}
    return jsonify({"id": id, "challenge": challenge_enc, "success": True})

@auth_bp.route("/login", methods=["POST"])
@sliding_window_rate_limiter(limit=30, window=300, user_limit=15)
@validate_request_data({"username": {"minlen": 3, "maxlen": 20}, "passkey": {"len": 20}, "public": {"len": 392}}, 401)
@pass_db
def login(db:SQLite):
    user=db.select_data("users", ["id", "passkey", "public_key"], {"username": request.form["username"]})
    db.close()
    if not user: return make_json_error(401, "Invalid login details")
    if not bcrypt.checkpw(request.form["passkey"].encode(), user[0]["passkey"].encode()): return make_json_error(401, "Invalid login details")
    if request.form["public"]!=user[0]["public_key"]: return make_json_error(401, "Public key doesn't match")
    public_key, error_resp=public_key_open()
    if error_resp: return error_resp
    id, challenge_hash, challenge_enc=get_challenge(public_key)
    id=generate()
    with challenges_lock: challenges[id]={"id": user[0]["id"], "hashed": challenge_hash, "expire": timestamp()+60}
    return jsonify({"id": id, "challenge": challenge_enc, "success": True})

@auth_bp.route("/reset-keys", methods=["POST"])
@sliding_window_rate_limiter(limit=10, window=600, user_limit=5)
@validate_request_data({"public": {"len": 392}}, 401)
@logged_in()
def reset_keys(id):
    public_key, error_resp=public_key_open()
    if error_resp: return error_resp
    id, challenge_hash, challenge_enc=get_challenge(public_key)
    with challenges_lock: challenges[id]={"reset_keys": True, "user_id": id, "hashed": challenge_hash, "expire": timestamp()+60, "public": request.form["public"]}
    return jsonify({"id": id, "challenge": challenge_enc, "success": True})

@auth_bp.route("/reset-passkey", methods=["POST"])
@sliding_window_rate_limiter(limit=10, window=600, user_limit=5)
@validate_request_data({"public": {"len": 392}}, 401)
@logged_in()
def reset_passkey(db:SQLite, id):
    user_public=db.select_data("users", ["public_key"], {"id": id})
    if not user_public: return make_json_error(400, "User not found")
    if request.form["public"]!=user_public[0]["public_key"]: return make_json_error(401, "Public key doesn't match")
    public_key, error_resp=public_key_open()
    if error_resp: return error_resp
    id, challenge_hash, challenge_enc=get_challenge(public_key)
    with challenges_lock: challenges[id]={"reset_passkey": True, "user_id": id, "hashed": challenge_hash, "expire": timestamp()+60}
    return jsonify({"id": id, "challenge": challenge_enc, "success": True})