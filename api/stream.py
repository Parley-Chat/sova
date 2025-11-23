from flask import Blueprint, Response, stream_with_context
from .utils import (
    logged_in, sliding_window_rate_limiter, timestamp, perm, has_permission
)
from utils import generate
import time
import json
from threading import Lock
from db import SQLite

stream_bp=Blueprint("stream", __name__)

streams={}
streams_lock=Lock()

def emit(event_type, data, conditions=None):
    """Emit event to all matching streams with thread safety"""
    with streams_lock:
        streams_to_remove=[]
        for i, stream_data in streams.items():
            try:
                should_send=True
                if conditions:
                    if "channel_ids" in conditions:
                        required_channels=conditions["channel_ids"]
                        if not any(ch in stream_data["channel_ids"] for ch in required_channels):
                            should_send=False
                    if "user_id" in conditions and stream_data["user_id"] not in conditions["user_id"]:
                        should_send=False
                    if "exclude_user" in conditions and stream_data["user_id"]==conditions["exclude_user"]:
                        should_send=False
                if should_send:
                    with stream_data["lock"]:
                        event_data={
                            "event": event_type,
                            "data": data,
                            "timestamp": timestamp(True)
                        }
                        stream_data["pending"].append(event_data)
            except:
                streams_to_remove.append(i)
        for i in streams_to_remove:
            del streams[i]

def message_sent(channel_id, message_data, user_id, db):
    """Emit message sent event"""
    channel_data=db.select_data("channels", ["type", "permissions"], {"id": channel_id})
    if not channel_data:
        return

    channel_type=channel_data[0]["type"]
    channel_permissions=channel_data[0]["permissions"]

    if channel_type==3:
        member_rows=db.execute_raw_sql("SELECT user_id, permissions FROM members WHERE channel_id=?", (channel_id,))

        manage_users=[]
        regular_users=[]

        for row in member_rows:
            member_user_id=row["user_id"]
            member_permissions=row["permissions"]

            if (has_permission(member_permissions, perm.send_messages, channel_permissions) or
                has_permission(member_permissions, perm.manage_members, channel_permissions) or
                has_permission(member_permissions, perm.manage_permissions, channel_permissions)):
                manage_users.append(member_user_id)
            else:
                regular_users.append(member_user_id)

        if manage_users:
            emit("message_sent", {
                "channel_id": channel_id,
                "message": message_data
            }, {
                "user_id": manage_users
            })

        if regular_users:
            message_data_no_author=dict(message_data)
            message_data_no_author["user"]=None
            message_data_no_author["signature"]=None
            message_data_no_author["signed_timestamp"]=None
            emit("message_sent", {
                "channel_id": channel_id,
                "message": message_data_no_author
            }, {
                "user_id": regular_users
            })
    else:
        emit("message_sent", {
            "channel_id": channel_id,
            "message": message_data
        }, {
            "channel_ids": [channel_id],
        })

def message_edited(channel_id, message_data, user_id, db):
    """Emit message edited event"""
    channel_data=db.select_data("channels", ["type", "permissions"], {"id": channel_id})
    if not channel_data:
        return

    channel_type=channel_data[0]["type"]
    channel_permissions=channel_data[0]["permissions"]

    if channel_type==3:
        member_rows=db.execute_raw_sql("SELECT user_id, permissions FROM members WHERE channel_id=?", (channel_id,))

        manage_users=[]
        regular_users=[]

        for row in member_rows:
            member_user_id=row["user_id"]
            member_permissions=row["permissions"]

            if (has_permission(member_permissions, perm.send_messages, channel_permissions) or
                has_permission(member_permissions, perm.manage_members, channel_permissions) or
                has_permission(member_permissions, perm.manage_permissions, channel_permissions)):
                manage_users.append(member_user_id)
            else:
                regular_users.append(member_user_id)

        if manage_users:
            emit("message_edited", {
                "channel_id": channel_id,
                "message": message_data
            }, {
                "user_id": manage_users
            })

        if regular_users:
            message_data_no_author=dict(message_data)
            message_data_no_author["user"]=None
            message_data_no_author["signature"]=None
            message_data_no_author["signed_timestamp"]=None
            emit("message_edited", {
                "channel_id": channel_id,
                "message": message_data_no_author
            }, {
                "user_id": regular_users
            })
    else:
        emit("message_edited", {
            "channel_id": channel_id,
            "message": message_data
        }, {
            "channel_ids": [channel_id]
        })

def message_deleted(channel_id, message_id, user_id):
    """Emit message deleted event"""
    emit("message_deleted", {
        "channel_id": channel_id,
        "message_id": message_id
    }, {
        "channel_ids": [channel_id]
    })

def channel_added(user_id, channel_data, db=None):
    """Emit channel added event and update user's channel_ids"""
    # Update the user's channel_ids in their active streams
    with streams_lock:
        for i, stream_data in streams.items():
            if stream_data["user_id"]==user_id:
                with stream_data["lock"]:
                    if channel_data["id"] not in stream_data["channel_ids"]:
                        stream_data["channel_ids"].append(channel_data["id"])

    # Check if user has manage_permissions to include channel_permissions
    if db:
        member_data=db.select_data("members", ["permissions"], {"channel_id": channel_data["id"], "user_id": user_id})
        member_permissions=member_data[0]["permissions"] if member_data else None
        effective_permissions=member_permissions if member_permissions is not None else channel_data.get("permissions", 0)

        if has_permission(member_permissions, perm.manage_permissions, channel_data.get("permissions", 0)):
            db_channel_data=db.select_data("channels", ["permissions"], {"id": channel_data["id"]})
            actual_channel_permissions=db_channel_data[0]["permissions"] if db_channel_data else 0
            channel_with_perms=dict(channel_data)
            channel_with_perms["channel_permissions"]=actual_channel_permissions
            emit("channel_added", {
                "channel": channel_with_perms
            }, {
                "user_id": [user_id]
            })
            return

    emit("channel_added", {
        "channel": channel_data
    }, {
        "user_id": [user_id]
    })

def channel_edited(channel_id, channel_data, db):
    """Emit channel edited event with effective permissions per user"""
    member_rows=db.execute_raw_sql("SELECT user_id, permissions FROM members WHERE channel_id=?", (channel_id,))
    for row in member_rows:
        user_id=row["user_id"]
        effective_permissions=row["permissions"] if row["permissions"] is not None else channel_data["permissions"]
        user_channel=dict(channel_data)
        user_channel["permissions"]=effective_permissions

        # Include channel_permissions if user has manage_permissions
        if has_permission(row["permissions"], perm.manage_permissions, channel_data["permissions"]):
            user_channel["channel_permissions"]=channel_data["permissions"]

        emit("channel_edited", {
            "channel_id": channel_id,
            "channel": user_channel
        }, {
            "user_id": [user_id]
        })

def channel_deleted(channel_id, db):
    """Emit channel deleted event and update users' channel_ids"""
    member_data=db.execute_raw_sql("SELECT user_id FROM members WHERE channel_id=?", (channel_id,))
    user_ids=[row["user_id"] for row in member_data]

    if user_ids:
        emit("channel_deleted", {
            "channel_id": channel_id
        }, {
            "user_id": user_ids
        })

    # Update channel_ids for all affected users' streams
    with streams_lock:
        for i, stream_data in streams.items():
            if stream_data["user_id"] in user_ids:
                with stream_data["lock"]:
                    if channel_id in stream_data["channel_ids"]:
                        stream_data["channel_ids"].remove(channel_id)

def update_channel_keys_on_member_change(channel_id, db):
    """Update channels_keys_info entries from the last hour to expire immediately when member changes occur"""
    db.execute_raw_sql(
        "UPDATE channels_keys_info SET expires_at=0 WHERE channel_id=? AND expires_at>=?",
        (channel_id, timestamp())
    )

def _emit_member_event_with_channel_perms(event_type, event_data, channel_id, member_user_id, db):
    """Helper function to emit member events with permission filtering for channel type 3"""
    channel_data=db.select_data("channels", ["type", "permissions"], {"id": channel_id})
    if channel_data and channel_data[0]["type"]==3:
        # For channel type 3, only send to users in this channel with manage_channel or manage_permissions
        channel_permissions=channel_data[0]["permissions"]
        all_members=db.execute_raw_sql("SELECT user_id, permissions FROM members WHERE channel_id=?", (channel_id,))
        user_ids=[]
        for member in all_members:
            if has_permission(member["permissions"], perm.manage_permissions, channel_permissions):
                user_ids.append(member["user_id"])

        # Always include the member who is joining/leaving
        if member_user_id not in user_ids:
            user_ids.append(member_user_id)

        emit(event_type, event_data, {"user_id": user_ids})
    else:
        emit(event_type, event_data, {"channel_ids": [channel_id]})

def member_join(channel_id, user_data, db):
    """Emit member join event and update user's channel_ids"""
    user_id=user_data["id"]

    # Update channels_keys_info to expire the latest entry
    update_channel_keys_on_member_change(channel_id, db)

    # Update the user's channel_ids in their active streams
    with streams_lock:
        for i, stream_data in streams.items():
            if stream_data["user_id"]==user_id:
                with stream_data["lock"]:
                    if channel_id not in stream_data["channel_ids"]:
                        stream_data["channel_ids"].append(channel_id)

    # Get member's permissions for the event
    member_data=db.select_data("members", ["permissions"], {"channel_id": channel_id, "user_id": user_id})
    member_permissions=member_data[0]["permissions"] if member_data else None

    # Create user data without id for the event
    user_event_data={k: v for k, v in user_data.items() if k!="id"}

    # Emit to users with manage permissions (include permissions data)
    channel_data=db.select_data("channels", ["type", "permissions"], {"id": channel_id})
    if channel_data:
        channel_permissions=channel_data[0]["permissions"]
        effective_permissions=member_permissions if member_permissions is not None else channel_permissions

        # Get all members and filter with has_permission
        all_members=db.execute_raw_sql("SELECT user_id, permissions FROM members WHERE channel_id=?", (channel_id,))
        manage_user_ids=[]
        non_manage_user_ids=[]
        for member in all_members:
            if has_permission(member["permissions"], perm.manage_permissions, channel_permissions):
                manage_user_ids.append(member["user_id"])
            else:
                non_manage_user_ids.append(member["user_id"])

        # Send event with permissions to manage users
        if manage_user_ids:
            emit("member_join", {
                "channel_id": channel_id,
                "user": user_event_data,
                "permissions": effective_permissions
            }, {"user_id": manage_user_ids})

        # Send event without permissions to other users
        if non_manage_user_ids:
            emit("member_join", {
                "channel_id": channel_id,
                "user": user_event_data
            }, {"user_id": non_manage_user_ids})
    else:
        # Fallback to original behavior
        _emit_member_event_with_channel_perms("member_join", {
            "channel_id": channel_id,
            "user": user_event_data
        }, channel_id, user_id, db)

def member_leave(channel_id, user_data, db):
    """Emit member leave event and update user's channel_ids"""
    user_id=user_data["id"]

    # Update channels_keys_info to expire the latest entry
    update_channel_keys_on_member_change(channel_id, db)

    # Create user data without id for the event
    user_event_data={k: v for k, v in user_data.items() if k!="id"}

    _emit_member_event_with_channel_perms("member_leave", {
        "channel_id": channel_id,
        "user": user_event_data
    }, channel_id, user_id, db)

    # Update the user's channel_ids in their active streams
    with streams_lock:
        for i, stream_data in streams.items():
            if stream_data["user_id"]==user_id:
                with stream_data["lock"]:
                    if channel_id in stream_data["channel_ids"]:
                        stream_data["channel_ids"].remove(channel_id)

def member_info_changed(user_id, user_data, db):
    """Emit member info changed event (only once per member across all channels)"""
    # Get all channels where this user is a member
    user_channels=db.execute_raw_sql("SELECT c.id as channel_id, c.type FROM channels c JOIN members m ON c.id=m.channel_id WHERE m.user_id=?", (user_id,))
    channel_ids=[row["channel_id"] for row in user_channels]

    if channel_ids:
        # Create user data without id for the event
        user_event_data={k: v for k, v in user_data.items() if k!="id"}

        # Check if all channels are type 3
        all_type_3=all(row["type"]==3 for row in user_channels)

        if all_type_3:
            # All mutual channels are type 3, use permission-based filtering
            # Get all users with manage_channel or manage_permissions across all these channels
            permitted_users_set=set()
            for row in user_channels:
                channel_id=row["channel_id"]
                channel_data=db.select_data("channels", ["permissions"], {"id": channel_id})
                if channel_data:
                    channel_permissions=channel_data[0]["permissions"]
                    channel_members=db.execute_raw_sql("SELECT user_id, permissions FROM members WHERE channel_id=?", (channel_id,))
                    for member in channel_members:
                        if has_permission(member["permissions"], perm.manage_permissions, channel_permissions):
                            permitted_users_set.add(member["user_id"])

            if permitted_users_set:
                emit("member_info_changed", {
                    "user": user_event_data,
                    "channels": channel_ids
                }, {
                    "user_id": list(permitted_users_set)
                })
        else:
            # Normal behavior for mixed or non-type-3 channels
            emit("member_info_changed", {
                "user": user_event_data,
                "channels": channel_ids
            }, {
                "channel_ids": channel_ids
            })

def member_perms_changed(channel_id, user_id, username, permissions, db):
    channel_data=db.select_data("channels", ["permissions"], {"id": channel_id})
    channel_permissions=channel_data[0]["permissions"] if channel_data else 0
    effective_permissions=permissions if permissions is not None else channel_permissions

    # Get all members and filter with has_permission
    all_members=db.execute_raw_sql("SELECT user_id, permissions FROM members WHERE channel_id=?", (channel_id,))
    manage_user_ids=[]
    for member in all_members:
        if has_permission(member["permissions"], perm.manage_permissions, channel_permissions):
            manage_user_ids.append(member["user_id"])

    # Always include the target user
    if user_id not in manage_user_ids:
        manage_user_ids.append(user_id)

    emit("member_perms_changed", {
        "username": username,
        "channel_id": channel_id,
        "permissions": effective_permissions
    }, {
        "user_id": manage_user_ids
    })

def dm_unhide(channel_id, user_id, db):
    """Emit channel_added and member_join events when a DM is unhidden, only to the specific user"""
    # Get the other user in the DM
    other_user=db.execute_raw_sql("SELECT user_id FROM members WHERE channel_id=? AND user_id!=?", (channel_id, user_id))
    if not other_user: return

    other_user_id=other_user[0]["user_id"]

    # Get user data
    other_user_data=db.select_data("users", ["username", "display_name", "pfp"], {"id": other_user_id})[0]
    current_user_data=db.select_data("users", ["username", "display_name", "pfp"], {"id": user_id})[0]

    # Emit channel_added event to the user who unhid the channel (showing other user's info)
    channel_data={
        "id": channel_id,
        "name": other_user_data["username"],
        "pfp": other_user_data["pfp"],
        "type": 1,
        "permissions": perm.send_messages,
        "member_count": 2
    }
    channel_added(user_id, channel_data, db)

    # Emit member_join event only to the user who unhid the channel
    user_event_data={k: v for k, v in current_user_data.items() if k!="id"}
    emit("member_join", {
        "channel_id": channel_id,
        "user": user_event_data
    }, {"user_id": [user_id]})

def call_start(channel_id, started_by_username, db):
    """Emit call start event"""
    emit("call_start", {
        "channel_id": channel_id,
        "started_by": started_by_username,
        "timestamp": timestamp(True)
    }, {
        "channel_ids": [channel_id]
    })

def call_join(channel_id, user_data, db):
    """Emit call join event"""
    emit("call_join", {
        "channel_id": channel_id,
        "user": {
            "username": user_data["username"],
            "display": user_data["display_name"],
            "pfp": user_data["pfp"]
        }
    }, {
        "channel_ids": [channel_id]
    })

def call_left(channel_id, user_data, db):
    """Emit call left event"""
    emit("call_left", {
        "channel_id": channel_id,
        "user": {
            "username": user_data["username"],
            "display": user_data["display_name"],
            "pfp": user_data["pfp"]
        }
    }, {
        "channel_ids": [channel_id]
    })

def call_signal(channel_id, from_user_id, signal_type, signal_data, db):
    """Emit WebRTC signaling data to other participant"""
    emit("call_signal", {
        "channel_id": channel_id,
        "from_user": from_user_id,
        "type": signal_type,
        "data": signal_data
    }, {
        "channel_ids": [channel_id],
        "exclude_user": from_user_id
    })

@stream_bp.route("/stream")
@logged_in(True)
@sliding_window_rate_limiter(limit=10, window=60, user_limit=5)
def stream(db:SQLite, session_id, id):
    channel_ids=db.execute_raw_sql("""
        SELECT c.id FROM channels c
        JOIN members m ON c.id=m.channel_id
        WHERE m.user_id=?
    """, (id,))
    channel_ids=[row["id"] for row in channel_ids]
    active_call_events=[]
    if channel_ids:
        placeholders=", ".join(["?"]*len(channel_ids))
        active_call_rows=db.execute_raw_sql(f"""
            SELECT c.channel_id, c.started_at, u.username FROM calls c
            JOIN users u ON c.started_by=u.id
            WHERE c.channel_id IN ({placeholders})
        """, tuple(channel_ids))
        for row in active_call_rows:
            active_call_events.append({
                "event": "call_start",
                "data": {
                    "channel_id": row["channel_id"],
                    "started_by": row["username"],
                    "timestamp": row["started_at"]
                }
            })

    stream_data={
        "channel_ids": channel_ids,
        "user_id": id,
        "pending": active_call_events,
        "lock": Lock()
    }
    client=generate()
    with streams_lock:
        streams[client]=stream_data
    def generator():
        try:
            yield f": heartbeat\n\n"
            next_heartbeat=timestamp()+10
            session_check_time=timestamp()+60
            while True:
                current_time=timestamp()

                # Check session validity every 60s
                if current_time>=session_check_time:
                    if not db.exists("session", {"id": session_id}):
                        yield f"event: error\ndata: {{\"error\": \"Invalid_session\"}}\n\n"
                        break
                    session_check_time=current_time+60

                # Send heartbeat every 10s
                if current_time>=next_heartbeat:
                    yield f": heartbeat\n\n"
                    next_heartbeat=current_time+10

                # Process pending events
                with stream_data["lock"]:
                    pending_events=stream_data["pending"].copy()
                    stream_data["pending"].clear()

                for event in pending_events:
                    event_str=json.dumps(event["data"])
                    yield f"event: {event['event']}\ndata: {event_str}\n\n"

                time.sleep(0.1)
        except Exception as e:
            yield f"event: error\ndata: {{\"error\": \"connection_error\"}}\n\n"
        finally:
            with streams_lock:
                del streams[client]

    resp=Response(stream_with_context(generator()), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    return resp
