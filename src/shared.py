import collections

# Global in-memory stores
camera_states = {}       # Stores CameraMemory instances
frame_buffer = {}        # Stores the latest processed frame for streaming
active_cameras_info = {} # Stores metadata (IP, status) for connected cameras

class CameraMemory:
    """
    Manages history for a specific camera to allow "best shot" selection.
    """
    def __init__(self):
        # Cache last 60 frames to find the best angle of a violation
        self.frame_cache = collections.deque(maxlen=60)
        # Keep track of people we've already alerted on to avoid spamming DB
        self.saved_person_ids = set()