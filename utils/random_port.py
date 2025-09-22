import socket
import random
def get_random_unused_port(start=1024, end=49151, max_attempts=10):
    for _ in range(max_attempts):
        port = random.randint(start, end)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("", port))
                return port
            except OSError:
                continue
    raise RuntimeError("Could not find an unused port after several attempts.")