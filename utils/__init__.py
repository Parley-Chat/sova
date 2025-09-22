from threading import Event
import tomllib
import os
import sys
import nanoid

def generate(size=20): return nanoid.generate(size=size)

stopping=Event()

with open("version.toml", "rb") as f:
    version_data=tomllib.load(f)
version=version_data["version"]
db_version=version_data["db"]

if not os.path.isfile("config.toml") or os.path.getsize("config.toml")==0:
    from .random_port import get_random_unused_port
    with open("default_config.toml") as fc:
        with open("config.toml", "w") as f:
            f.write("".join(fc.readlines()[2:]).replace("$URI_PREFIX", generate()).replace("$PORT", str(get_random_unused_port())))
    print("Wrote config.toml")
    sys.exit(1)
with open("config.toml", "rb") as f:
    config=tomllib.load(f)

if config["version"]<version_data["config"]:
    print("Your config.toml version doesn't match, please remove it and run the program again to create a new one")
    sys.exit(1)

dev_mode="--dev" in sys.argv or config["server"]["dev"]

BLUE = "\033[34m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"

def colored_log(color, tag, text): print(f"{color}[{tag}]{RESET} {text}")