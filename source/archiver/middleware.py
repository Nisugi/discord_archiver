"""
Performance monitoring middleware for Flask viewer
Logs slow requests and errors for debugging
"""
import time
import traceback
from flask import request, g
from functools import wraps

class PerformanceMonitor:
    """Middleware to track request performance"""

    def __init__(self, app=None, slow_threshold=2.0):
        self.slow_threshold = slow_threshold
        if app:
            self.init_app(app)

    def init_app(self, app):
        """Initialize with Flask app"""
        app.before_request(self.before_request)
        app.after_request(self.after_request)
        app.teardown_request(self.teardown_request)

    def before_request(self):
        """Record start time"""
        g.request_start_time = time.time()

    def after_request(self, response):
        """Log request completion"""
        if hasattr(g, 'request_start_time'):
            elapsed = time.time() - g.request_start_time

            # Log all API requests
            if request.path.startswith('/api/'):
                status = response.status_code
                method = request.method

                log_msg = f"[{method}] {request.path} - {status} - {elapsed:.2f}s"

                # Add query params for debugging (truncate if too long)
                if request.query_string:
                    query_str = request.query_string.decode('utf-8')
                    if len(query_str) > 100:
                        query_str = query_str[:100] + "..."
                    log_msg += f" - Params: {query_str}"

                # Mark slow requests
                if elapsed > self.slow_threshold:
                    log_msg = "⚠️  SLOW " + log_msg

                print(f"[Performance] {log_msg}")

        return response

    def teardown_request(self, error=None):
        """Handle errors"""
        if error:
            print(f"[Error] Request error on {request.path}: {error}")
            traceback.print_exc()


def log_errors(f):
    """Decorator to log errors in route handlers"""
    @wraps(f)
    def wrapped(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            print(f"[Error] Exception in {f.__name__}: {e}")
            traceback.print_exc()
            raise
    return wrapped
