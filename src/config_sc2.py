import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Server Config
API_KEY = os.getenv("API_KEY")
HOST_IP = os.getenv("HOST_IP", "0.0.0.0")
PORT = int(os.getenv("PORT", 8443))

# SSL Config
SSL_KEY = os.getenv("SSL_KEY_PATH", "key.pem")
SSL_CERT = os.getenv("SSL_CERT_PATH", "cert.pem")

# Database Config
DB_DSN = os.getenv("SC2_DATABASE_URL")

# Model Config
YOLO_MODEL = os.getenv("YOLO_MODEL_PATH", "webest.pt")
CONF_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", 0.35))