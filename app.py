"""
Best Restaurants Finder - Web App
-----------------------------------
A small Flask app with a mobile-friendly search page. It calls the Google
Places API on the SERVER side (so your API key is never exposed to visitors)
and returns the top-rated restaurants for any city or area you type in.
"""

import os
import time
import requests
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")
TEXT_SEARCH_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"


def search_places(query, max_pages=3):
    results = []
    params = {"query": query, "key": API_KEY}

    for _ in range(max_pages):
        resp = requests.get(TEXT_SEARCH_URL, params=params).json()
        status = resp.get("status")

        if status not in ("OK", "ZERO_RESULTS"):
            raise RuntimeError(f"{status}: {resp.get('error_message', '')}")

        results.extend(resp.get("results", []))

        next_token = resp.get("next_page_token")
        if not next_token:
            break

        time.sleep(2)  # Google requires a short delay before a page token is valid
        params = {"pagetoken": next_token, "key": API_KEY}

    return results


def rank_restaurants(places, min_reviews=20, top_n=10):
    filtered = [
        p for p in places
        if p.get("rating") is not None
        and p.get("user_ratings_total", 0) >= min_reviews
    ]
    filtered.sort(key=lambda p: (p["rating"], p["user_ratings_total"]), reverse=True)
    return filtered[:top_n]


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/search")
def api_search():
    if not API_KEY:
        return jsonify({"error": "Server is missing GOOGLE_MAPS_API_KEY."}), 500

    query = request.args.get("query", "").strip()
    min_reviews = int(request.args.get("min_reviews", 20))
    top_n = int(request.args.get("top", 10))

    if not query:
        return jsonify({"error": "Please provide a search query."}), 400

    try:
        places = search_places(query)
        top = rank_restaurants(places, min_reviews, top_n)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502

    cleaned = [
        {
            "name": p.get("name"),
            "rating": p.get("rating"),
            "reviews": p.get("user_ratings_total", 0),
            "address": p.get("formatted_address"),
            "maps_url": f"https://www.google.com/maps/place/?q=place_id:{p.get('place_id')}",
        }
        for p in top
    ]
    return jsonify({"results": cleaned})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
