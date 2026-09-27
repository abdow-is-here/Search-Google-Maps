"""
Best Restaurants Finder - Web App
-----------------------------------
A small Flask app with a mobile-friendly search page. It calls the Google
Places API on the SERVER side (so your API key is never exposed to visitors)
and returns the top-rated restaurants for any city or area you type in.
"""

import os
import re
import time
import requests
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")
TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = "places.id,places.displayName,places.rating,places.userRatingCount,places.formattedAddress"

CITY_RADIUS_METERS = 40000.0  # fallback only, used if a place has no viewport info


def extract_location_phrase(query):
    """Pulls out the location part of a query like 'malls in Cairo' -> 'Cairo'."""
    match = re.search(r"\b(?:in|near|at)\s+(.+)$", query, re.IGNORECASE)
    return match.group(1).strip() if match else None


def geocode_location(location_text):
    """Looks up a place's coordinates AND real boundary (viewport) using the same Places API."""
    body = {"textQuery": location_text, "pageSize": 1}
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": "places.location,places.viewport",
    }
    resp = requests.post(TEXT_SEARCH_URL, json=body, headers=headers).json()
    places = resp.get("places", [])
    if not places:
        return None

    place = places[0]
    loc = place.get("location")
    viewport = place.get("viewport")
    if not loc:
        return None

    return {
        "lat": loc.get("latitude"),
        "lng": loc.get("longitude"),
        "viewport": viewport,  # has 'low' and 'high' lat/lng bounds, or None
    }


def build_location_restriction(geo):
    """Prefers the city's real boundary (viewport); falls back to a fixed-radius circle."""
    viewport = geo.get("viewport")
    if viewport and viewport.get("low") and viewport.get("high"):
        return {
            "rectangle": {
                "low": {
                    "latitude": viewport["low"]["latitude"],
                    "longitude": viewport["low"]["longitude"],
                },
                "high": {
                    "latitude": viewport["high"]["latitude"],
                    "longitude": viewport["high"]["longitude"],
                },
            }
        }
    return {
        "circle": {
            "center": {"latitude": geo["lat"], "longitude": geo["lng"]},
            "radius": CITY_RADIUS_METERS,
        }
    }


def search_places(query, max_pages=3):
    """Uses the New Places API (Text Search), hard-restricted to the city in the query."""
    results = []

    location_restriction = None
    location_phrase = extract_location_phrase(query)
    if location_phrase:
        geo = geocode_location(location_phrase)
        if geo:
            location_restriction = build_location_restriction(geo)

    body = {"textQuery": query, "pageSize": 20}
    if location_restriction:
        body["locationRestriction"] = location_restriction

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": FIELD_MASK + ",nextPageToken",
    }

    for _ in range(max_pages):
        resp = requests.post(TEXT_SEARCH_URL, json=body, headers=headers).json()

        if "error" in resp:
            raise RuntimeError(resp["error"].get("message", "Unknown error"))

        for p in resp.get("places", []):
            results.append({
                "name": p.get("displayName", {}).get("text", "N/A"),
                "rating": p.get("rating"),
                "user_ratings_total": p.get("userRatingCount", 0),
                "formatted_address": p.get("formattedAddress"),
                "place_id": p.get("id"),
            })

        next_token = resp.get("nextPageToken")
        if not next_token:
            break

        time.sleep(2)  # short delay before a page token becomes valid
        body = {"textQuery": query, "pageSize": 20, "pageToken": next_token}

    return results


def rank_restaurants(places, min_reviews=20, top_n=10):
    filtered = [
        p for p in places
        if p.get("rating") is not None
        and p.get("user_ratings_total", 0) >= min_reviews
    ]
    filtered.sort(key=lambda p: (p["user_ratings_total"], p["rating"]), reverse=True)
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
