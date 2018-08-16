import logging
import os
import sys
from flask import Flask, Blueprint
from flask import request, json
from gevent.queue import Queue
import requests

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

CLIENT_ID = os.environ.get("CLIENT_ID")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET")
TOKEN_HEADER = os.environ.get("TOKEN_HEADER", "X-Wormhole")
TOKEN_VALUE = os.environ.get("TOKEN_VALUE")
VERIFICATION_TOKEN = os.environ.get("VERIFICATION_TOKEN")
MESSAGES = []
OUTBOUND = Queue()

html = Blueprint(r'html', __name__)


@html.route("/wormhole", methods=["GET", "POST"])
def wormhole():
    d = dict(request.form)

    if d.get("token") != [VERIFICATION_TOKEN]:
        logger.warn("Didn't receive the proper verification token (%s)", d)
        return "Nope", 403

    logger.info(json.dumps(d))
    OUTBOUND.put(json.dumps(d))
    return f"Forwarded {d['text'][0]}"


@html.route("/oauth")
def auth():
    if 'code' not in request.args:
        return "missing parameter 'code'", 400

    params = {
        "code": flask.request.args["code"],
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    resp = requests.get("https://slack.com/api/oauth.access", params=params)
    return json.dumps(resp.json())


def check_token(func):

    def _wrap():
        if TOKEN_HEADER not in request.headers:
            return "Not Authorized", 401

        if request.headers.get(TOKEN_HEADER) != TOKEN_VALUE:
            return "Forbidden", 403

        return func()

    return _wrap


@html.route("/irc", methods=["GET", "POST"])
@check_token
def irc():
    if request.method == "POST":
        if request.headers.get("Content-Type") != "application/json":
            return "Invalid Content Type", 415

        MESSAGES.append(request.get_json())
        return json.dumps(request.get_json()), 201

    else:
        try:
            return OUTBOUND.get(timeout=5)

        except:
            return '{}'


app = Flask(__name__)

app.register_blueprint(html, url_prefix=r'')

if __name__ == "__main__":
    if all([CLIENT_ID, CLIENT_SECRET, TOKEN_VALUE, VERIFICATION_TOKEN]):
        from gevent import pywsgi

        server = pywsgi.WSGIServer(('', 5000), app)
        server.serve_forever()
    else:
        logging.error("Don't have enough configuration to start.")
        sys.exit(1)
