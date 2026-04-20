import os

import requests
from flask import Flask, jsonify, request


app = Flask(__name__)


def json_response(status: int, payload: dict):
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/pot-provider-ping")
@app.get("/")
def handler():
    cron_secret = os.getenv("CRON_SECRET", "").strip()
    auth_header = request.headers.get("authorization", "").strip()

    if cron_secret and auth_header != f"Bearer {cron_secret}":
        return json_response(401, {"ok": False, "error": "Unauthorized"})

    provider_url = os.getenv("YTDL_POT_PROVIDER_URL", "").strip()
    if not provider_url:
        return json_response(500, {"ok": False, "error": "YTDL_POT_PROVIDER_URL missing"})

    ping_url = provider_url.rstrip("/") + "/ping"

    try:
        response = requests.get(ping_url, timeout=90)
        return json_response(
            200 if response.ok else 503,
            {
                "ok": response.ok,
                "providerUrl": provider_url,
                "pingUrl": ping_url,
                "statusCode": response.status_code,
                "body": response.text[:1000],
            },
        )
    except requests.RequestException as error:
        return json_response(
            503,
            {
                "ok": False,
                "providerUrl": provider_url,
                "pingUrl": ping_url,
                "error": str(error),
            },
        )
