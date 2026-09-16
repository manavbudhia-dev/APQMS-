"""Small shared constants used by the server and by static-file serving."""
import os

PORT = 5000
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
