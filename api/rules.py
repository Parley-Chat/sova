from flask import Blueprint, send_file
from .utils import make_json_error

rules_bp=Blueprint("rules", __name__)

@rules_bp.route("/tos.md")
def tos():
    try: return send_file("legal/tos.md")
    except FileNotFoundError: return make_json_error(404, "Terms of Service not found")

@rules_bp.route("/pp.md")
def pp():
    try: return send_file("legal/pp.md")
    except FileNotFoundError: return make_json_error(404, "Privacy Policy not found")

@rules_bp.route("/rules.md")
def rules():
    try: return send_file("legal/rules.md")
    except FileNotFoundError: return make_json_error(404, "Rules not found")
