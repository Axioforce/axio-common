import logging
import threading


# Custom logging filter to include hostname
class HostnameFilter(logging.Filter):
    def __init__(self):
        super().__init__()
        self.local = threading.local()
        self.local.hostname = "hostname_unknown"  # Default hostname
        self.local.ipaddress = "ipaddress_unknown"  # Default ipaddress

    def set_hostname(self, hostname=None):
        self.local.hostname = hostname if hostname else "hostname_unknown"

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

logger = logging.getLogger("job_manager_logger")
hostname_filter = HostnameFilter()
logger.addFilter(hostname_filter)
