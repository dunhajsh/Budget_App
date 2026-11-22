import os

from flask import Flask, render_template

from .db import close_db, init_db

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def create_app(data_dir=None, testing=False):
    data_dir = data_dir or os.environ.get("BUDGET_DATA_DIR") or os.path.join(APP_DIR, "data")
    app = Flask(__name__)
    app.config.update(
        DATA_DIR=data_dir,
        DATABASE=os.path.join(data_dir, "budget.db"),
        RECEIPT_DIR=os.path.join(data_dir, "receipts"),
        MAX_CONTENT_LENGTH=60 * 1024 * 1024,
        TESTING=testing,
        JSON_SORT_KEYS=False,
    )
    os.makedirs(app.config["RECEIPT_DIR"], exist_ok=True)
    init_db(app.config["DATABASE"])
    app.teardown_appcontext(close_db)

    from .api import bp
    from .receipts import WORKER

    app.register_blueprint(bp)
    if not testing:  # finish reading any receipts left half-done (e.g. the app was closed mid-upload)
        WORKER.kick(app.config["DATABASE"], app.config["RECEIPT_DIR"])

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.errorhandler(404)
    def not_found(e):
        # An /api address the server doesn't know usually means the page is newer than the
        # running server (code was updated but the app wasn't restarted).
        from flask import jsonify, request
        if request.path.startswith("/api/"):
            return jsonify(error="This feature isn't available in the running app. Restart it: close the Budget "
                                 "window and run Start Budget.bat again."), 404
        return e

    return app
