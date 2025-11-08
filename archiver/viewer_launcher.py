# archiver/viewer_launcher.py
# This runs the web viewer in a separate thread

import threading
import logging
import time
from werkzeug.serving import make_server
from .viewer import app

# Suppress Flask's default logging
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

server = None

def run_viewer(host='0.0.0.0', port=8080):
    """Run the Flask viewer app using Werkzeug server"""
    global server
    print(f"[Viewer] Starting web interface on http://{host}:{port}")
    
    try:
        # Create a proper WSGI server
        server = make_server(host, port, app, threaded=True)
        print(f"[Viewer] Web server is listening on {host}:{port}")
        
        # This will block until shutdown
        server.serve_forever()
    except Exception as e:
        print(f"[Viewer] Failed to start server: {e}")
        import traceback
        traceback.print_exc()

def warm_caches():
    """Warm up the viewer caches"""
    try:
        import requests
        base_url = 'http://localhost:8080'
        
        # Check if server is ready
        for i in range(5):
            try:
                response = requests.get(f'{base_url}/health', timeout=2)
                if response.ok:
                    break
            except:
                time.sleep(1)
        else:
            print("[Viewer] Server not ready for cache warming")
            return
            
        # Warm the caches
        response = requests.get(f'{base_url}/api/warm_cache', timeout=10)
        if response.ok:
            print("[Viewer] Cache warming initiated successfully")
        else:
            print(f"[Viewer] Cache warming returned status {response.status_code}")
            
    except Exception as e:
        print(f"[Viewer] Cache warming failed: {e}")

def start_viewer_thread():
    """Start viewer in background thread"""
    viewer_thread = threading.Thread(
        target=run_viewer,
        daemon=True,  # Dies when main program exits
        name="ViewerThread"
    )
    viewer_thread.start()
    
    # Give it a moment to start and verify it's running
    time.sleep(2)
    
    if viewer_thread.is_alive():
        print("[Viewer] Web interface thread is running")
        
        # Warm up the caches in a separate thread to avoid blocking
        warming_thread = threading.Thread(
            target=warm_caches,
            daemon=True,
            name="CacheWarmingThread"
        )
        warming_thread.start()
    else:
        print("[Viewer] ERROR: Web interface thread failed to start")
        
def shutdown_viewer():
    """Shutdown the viewer server gracefully"""
    global server
    if server:
        print("[Viewer] Shutting down web server...")
        server.shutdown()