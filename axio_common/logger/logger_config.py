import logging
import sys
import threading
import socket


# Custom logging filter to include hostname and IP address
class HostnameFilter(logging.Filter):
    def __init__(self):
        super().__init__()
        self.local = threading.local()
        # Default to actual hostname and IP
        self.local.hostname = socket.gethostname()
        try:
            self.local.ipaddress = socket.gethostbyname(socket.gethostname())
        except socket.gaierror:
            self.local.ipaddress = "ipaddress_unknown"

    def set_hostname(self, hostname=None):
        self.local.hostname = hostname if hostname else socket.gethostname()

    def set_ipaddress(self, ipaddress=None):
        self.local.ipaddress = ipaddress if ipaddress else "ipaddress_unknown"

    def filter(self, record):
        record.hostname = getattr(self.local, "hostname", "hostname_unknown")
        record.ipaddress = getattr(self.local, "ipaddress", "ipaddress_unknown")
        return True

class SafeFormatter(logging.Formatter):
    def format(self, record):
        if not hasattr(record, "hostname"):
            record.hostname = "unknown_host"
        if not hasattr(record, "ipaddress"):
            record.ipaddress = "unknown_ip"
        return super().format(record)

def set_log_level(level):
    """
    Set the logging level for the logger.
    """
    level = level.upper()
    if level not in logging._nameToLevel:
        available_levels = "\n\t".join(logging._nameToLevel.keys())
        raise ValueError(f"Invalid log level: {level}\nAvailable levels:\n{available_levels}")
    new_level = logging._nameToLevel[level]
    logging.getLogger().setLevel(new_level)
    logging.getLogger("job_manager_logger").setLevel(new_level)

    return f"Log level set to {level}"

print("logger_config.py executed")

# Configure logging
# noinspection SpellCheckingInspection
# --- Logging setup ---
formatter = SafeFormatter("[%(levelname)s] [%(hostname)s/%(ipaddress)s] [%(module)s:%(funcName)s:%(lineno)d] %(message)s")

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(formatter)

logging.basicConfig(level=logging.INFO, handlers=[handler])

hostname_filter = HostnameFilter()
logging.getLogger().addFilter(hostname_filter)  # Apply filter to base logger
logging.getLogger("job_manager_logger").addFilter(hostname_filter)  # Also the custom logger

# Apply to uvicorn loggers too
logging.getLogger("uvicorn.access").addFilter(hostname_filter)
logging.getLogger("uvicorn.error").addFilter(hostname_filter)
