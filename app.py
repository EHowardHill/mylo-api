# api_app.py
import os
import datetime
from flask import Flask
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv

load_dotenv()

from utils.shared_api import db, UPLOAD_FOLDER, ensure_indexes, limiter, get_or_create_feed_circle

from routes.api import api_bp
from routes.users import users_bp
from routes.circles import circles_bp
from routes.posts import posts_bp
from routes.push import push_bp

class PrefixMiddleware(object):
    def __init__(self, app):
        self.app = app
    def __call__(self, environ, start_response):
        prefix = environ.get("HTTP_X_FORWARDED_PREFIX", "")
        if prefix:
            environ["SCRIPT_NAME"] = prefix
            path_info = environ.get("PATH_INFO", "")
            if path_info.startswith(prefix):
                environ["PATH_INFO"] = path_info[len(prefix) :]
        scheme = environ.get("HTTP_X_FORWARDED_PROTO", "")
        if scheme:
            environ["wsgi.url_scheme"] = scheme
        return self.app(environ, start_response)

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
limiter.init_app(app)

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 70 * 1024 * 1024
app.config["SECRET_KEY"] = os.environ.get("MYLO_SECRET_KEY")
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_PATH"] = "/mylo"
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = datetime.timedelta(days=365)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True

cors_origins = os.environ.get("MYLO_CORS_ORIGINS", "https://cinemint.online").split(",")
CORS(app, origins=cors_origins, supports_credentials=True)

app.wsgi_app = PrefixMiddleware(app.wsgi_app)

# Register only the API blueprints
app.register_blueprint(api_bp, url_prefix="/api")
app.register_blueprint(users_bp, url_prefix="/api/users")
app.register_blueprint(circles_bp, url_prefix="/api/circles")
app.register_blueprint(posts_bp, url_prefix="/api/posts")
app.register_blueprint(push_bp, url_prefix="/api/push")

if __name__ == "__main__":
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    ensure_indexes()
    get_or_create_feed_circle()
    
    # Run API on port 9001
    app.run(debug=True, use_reloader=False, host="0.0.0.0", port=9001)