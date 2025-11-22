from flask import Blueprint
from .auth import auth_bp
from .channels import channels_bp
from .keys import keys_bp
from .members import members_bp
from .bans import bans_bp
from .messages import messages_bp
from .users import users_bp
from .pins import pins_bp
from .stream import stream_bp
from .calls import calls_bp
from .utils import process_cors_headers, cleaner
from threading import Thread

api_bp=Blueprint("API", __name__)

@api_bp.after_request
def add_cors_headers(resp):
    process_cors_headers(resp)
    return resp

api_bp.register_blueprint(auth_bp)
api_bp.register_blueprint(channels_bp)
api_bp.register_blueprint(keys_bp)
api_bp.register_blueprint(members_bp)
api_bp.register_blueprint(bans_bp)
api_bp.register_blueprint(messages_bp)
api_bp.register_blueprint(users_bp)
api_bp.register_blueprint(pins_bp)
api_bp.register_blueprint(stream_bp)
api_bp.register_blueprint(calls_bp)

Thread(target=cleaner, daemon=True).start()