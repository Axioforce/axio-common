import logging
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


print("logger_config.py executed")

# Configure logging
# noinspection SpellCheckingInspection
logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] [%(hostname)s/%(ipaddress)s] [%(module)s:%(funcName)s:%(lineno)d] %(message)s",
    handlers=[
        logging.StreamHandler()
    ]
)

# Create and register filter
hostname_filter = HostnameFilter()

# Apply filter to your custom logger
logger = logging.getLogger("job_manager_logger")
logger.addFilter(hostname_filter)

# Apply filter to root logger to avoid KeyError
logging.getLogger().addFilter(hostname_filter)
