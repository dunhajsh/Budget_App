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

    app.register_blueprint(bp)

    @app.route("/")
    def index():
        return render_template("index.html")

    return app
