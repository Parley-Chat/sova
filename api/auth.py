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
  "max_channels": config["max_members"]["max_channels"], "password_protected": bool(config["instance"]["password"]),
  "calls": config["calls"],
  **({"dev": True} if dev_mode else {})}, 200

def join_invite(db, id, invite_code):
    invite_data=db.execute_raw_sql("""
        SELECT c.id, c.type,
               EXISTS(SELECT 1 FROM members WHERE user_id=? AND channel_id=c.id) as is_member,
               EXISTS(SELECT 1 FROM bans WHERE user_id=? AND channel_id=c.id) as is_banned,
               (SELECT COUNT(*) FROM members WHERE user_id=? AND hidden IS NULL) as user_channel_count,
               (SELECT COUNT(*) FROM members WHERE channel_id=c.id) as channel_member_count
        FROM channels c
        WHERE c.invite_code=?
    """, (id, id, id, invite_code))
    if not invite_data: return "Instance invite not found"
    data=invite_data[0]
    channel_id=data["id"]
    if data["is_member"]: return "User is already a member of instance invite channel"
    if data["is_banned"]: return "User is banned from instance invite channel"
    if data["user_channel_count"]>=config["max_members"]["max_channels"]: return "User has reached the maximum number of channels"
    channel_type=data["type"]
    if channel_type!=3:
        if data["channel_member_count"]>=config["max_members"]["encrypted_channels"]: return "Instance invite channel has reached maximum member limit"
    db.insert_data("members", {"user_id": id, "channel_id": channel_id, "joined_at": timestamp(), "message_seq": 0 if channel_type==3 else get_channel_last_message_seq(db, channel_id)})

    # Get user and channel data and emit events
    user_channel_data=db.execute_raw_sql("""
        SELECT u.id, u.username, u.display_name, u.pfp,
               c.name, c.pfp as channel_pfp, c.type, c.permissions,
               COUNT(m.user_id) as member_count
        FROM users u, channels c
        LEFT JOIN members m ON c.id=m.channel_id
        WHERE u.id=? AND c.id=?
        GROUP BY c.id
    """, (id, channel_id))[0]
    user_data={"id": user_channel_data["id"], "username": user_channel_data["username"], "display_name": user_channel_data["display_name"], "pfp": user_channel_data["pfp"]}
    full_channel_data={"id": channel_id, "name": user_channel_data["name"], "pfp": user_channel_data["channel_pfp"], "type": user_channel_data["type"], "permissions": user_channel_data["permissions"], "member_count": user_channel_data["member_count"]}
    member_join(channel_id, user_data, db)
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
        reset_passkey="reset_passkey" in challenges[request.form["id"]]
        if new:
            public_key_text=challenges[request.form["id"]]["public"]
            username=challenges[request.form["id"]]["username"]
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
    elif reset_passkey:
        new_passkey=generate()
        hashed_passkey=bcrypt.hashpw(new_passkey.encode(), bcrypt.gensalt()).decode()
        db.update_data("users", {"passkey": hashed_passkey}, {"id": user_id})
        user_public=db.execute_raw_sql("SELECT public_key FROM users WHERE id=?", (user_id,))[0]["public_key"]
        public_key, error_resp=public_key_open(user_public)
        if error_resp: return error_resp
        id=user_id
    else:
        public_key_data=db.execute_raw_sql("SELECT public_key FROM users WHERE id=?", (id,))
        if not public_key_data: return make_json_error(400, "User not found")
        public_key, error_resp=public_key_open(public_key_data[0]["public_key"])
        if error_resp: return error_resp
    if not reset_passkey:
        if "User-Agent" in request.headers:
            browser=regex_first_group_encrypted(browser_regex.search(request.headers["User-Agent"])[:50], public_key)
            device=regex_first_group_encrypted(device_regex.search(request.headers["User-Agent"])[:50], public_key)
        else: browser=device=None
        session=generate(50)
        db.insert_data("session", {"user": id, "token_hash": hash_token(session), "id": generate(), "browser": browser, "device": device, "logged_in_at": logged_in_at or timestamp(), "next_challenge": timestamp()+3600})
    db.close()
    if reset_passkey: return jsonify({"passkey": new_passkey, "success": True})
    return jsonify({"session": session, "success": True, **(({"passkey": passkey} if new else {}))})

@auth_bp.route("/username_check")
@sliding_window_rate_limiter(limit=50, window=60)
@validate_request_data({"username": {"minlen": 3, "maxlen": 20, "regex": re.compile(r"[a-z0-9_\-]+")}}, source="args")
@pass_db
def username_check(db:SQLite): return make_json_error(400, "Username is in use") if db.exists("users", {"username": request.args["username"]}) else jsonify({"success": True})

@auth_bp.route("/signup", methods=["POST"])
@sliding_window_rate_limiter(limit=10, window=120)
@validate_request_data({"username": {"minlen": 3, "maxlen": 20, "regex": re.compile(r"[a-z0-9_\-]+")}, "public": {"len": 392}}, 401)
@pass_db
def signup(db:SQLite):
    if config["instance"]["password"]:
        if "password" not in request.form: return make_json_error(403, "Password required")
        if request.form["password"]!=config["instance"]["password"]: return make_json_error(403, "Password incorrect")
    if db.exists("users", {"username": request.form["username"]}): return make_json_error(400, "Username is in use")
    db.close()
    public_key, error_resp=public_key_open()
    if error_resp: return error_resp
    id, challenge_hash, challenge_enc=get_challenge(public_key)
    with challenges_lock: challenges[id]={"new": True, "username": request.form["username"], "hashed": challenge_hash, "expire": timestamp()+60, "public": request.form["public"]}
    return jsonify({"id": id, "challenge": challenge_enc, "success": True})

@auth_bp.route("/login", methods=["POST"])
@sliding_window_rate_limiter(limit=20, window=120)
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

@auth_bp.route("/reset-passkey", methods=["POST"])
@sliding_window_rate_limiter(limit=10, window=600, user_limit=5)
@validate_request_data({"public": {"len": 392}}, 401)
@logged_in()
def reset_passkey(db:SQLite, id):
    user_public_data=db.execute_raw_sql("SELECT public_key FROM users WHERE id=?", (id,))
    if not user_public_data: return make_json_error(400, "User not found")
    if request.form["public"]!=user_public_data[0]["public_key"]: return make_json_error(401, "Public key doesn't match")
    public_key, error_resp=public_key_open()
    if error_resp: return error_resp
    id, challenge_hash, challenge_enc=get_challenge(public_key)
    with challenges_lock: challenges[id]={"reset_passkey": True, "user_id": id, "hashed": challenge_hash, "expire": timestamp()+60}
    return jsonify({"id": id, "challenge": challenge_enc, "success": True})
