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
import json
import threading
from datetime import datetime, timezone
import requests
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")
TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = "places.id,places.displayName,places.rating,places.userRatingCount,places.formattedAddress"

CITY_RADIUS_METERS = 40000.0  # fallback only, used if a place has no viewport info

LOG_FILE = os.path.join(os.path.dirname(__file__), "visit_log.jsonl")
LOG_LOCK = threading.Lock()
ADMIN_KEY = os.environ.get("LOGS_ADMIN_KEY")  # set this on Render to view /logs


def get_client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def describe_device(ua):
    ua = ua or ""
    if "iPhone" in ua:
        os_name = "iPhone"
    elif "iPad" in ua:
        os_name = "iPad"
    elif "Android" in ua:
        os_name = "Android"
    elif "Macintosh" in ua or "Mac OS X" in ua:
        os_name = "Mac"
    elif "Windows" in ua:
        os_name = "Windows"
    elif "Linux" in ua:
        os_name = "Linux"
    else:
        os_name = "Unknown OS"

    if "Edg/" in ua:
        browser = "Edge"
    elif "Chrome/" in ua and "Chromium" not in ua:
        browser = "Chrome"
    elif "CriOS" in ua:
        browser = "Chrome (iOS)"
    elif "Firefox/" in ua:
        browser = "Firefox"
    elif "Safari/" in ua and "Chrome" not in ua:
        browser = "Safari"
    else:
        browser = "Unknown browser"

    return f"{os_name} · {browser}"


def log_visit():
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ip": get_client_ip(),
        "device": describe_device(request.headers.get("User-Agent")),
        "user_agent": request.headers.get("User-Agent", ""),
        "path": request.path,
    }
    line = json.dumps(entry)
    with LOG_LOCK:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")


@app.before_request
def _track_visit():
    # Skip static assets, API calls, and the logs page itself to keep the log meaningful
    if request.path.startswith("/static/") or request.path.startswith("/api/") or request.path == "/logs":
        return
    try:
        log_visit()
    except OSError:
        pass  # logging is best-effort; never break the app over a disk issue


def extract_location_phrase(query):
    """Pulls out the location part of a query like 'malls in Cairo' -> 'Cairo'."""
    match = re.search(r"\b(?:in|near|at)\s+(.+)$", query, re.IGNORECASE)
    return match.group(1).strip() if match else None


def resolve_location_phrase(query, city, country):
    """An explicit 'in X' typed by the user always wins; otherwise falls back to
    the city/country the client sent (auto-detected, or chosen in Custom Filters)."""
    explicit = extract_location_phrase(query)
    if explicit:
        return explicit
    if city:
        return f"{city}, {country}" if country else city
    return None


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


def search_places(query, city=None, country=None, max_pages=3):
    """Uses the New Places API (Text Search), hard-restricted to the resolved city."""
    results = []

    location_restriction = None
    location_phrase = resolve_location_phrase(query, city, country)
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
        body["pageToken"] = next_token

    return results


def rank_restaurants(places, min_reviews=0, top_n=10, min_rating=0.0):
    filtered = [
        p for p in places
        if p.get("rating") is not None
        and p["rating"] >= min_rating
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
    mode = request.args.get("mode", "auto")
    city = request.args.get("city", "").strip()
    country = request.args.get("country", "").strip()

    if mode == "auto":
        # Best rankings: no rating/review filters, just the top 10 for the search term
        min_reviews, min_rating, top_n = 0, 0.0, 10
    else:
        try:
            min_reviews = int(request.args.get("min_reviews", 0) or 0)
            min_rating = float(request.args.get("min_rating", 3) or 3)
            top_n = int(request.args.get("top", 10) or 10)
        except ValueError:
            return jsonify({"error": "Invalid filter values."}), 400

    if not query:
        return jsonify({"error": "Please provide a search query."}), 400

    try:
        places = search_places(query, city=city, country=country)
        top = rank_restaurants(places, min_reviews, top_n, min_rating)
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


@app.route("/api/reverse-geocode")
def reverse_geocode():
    """Turns GPS coordinates into a city + country. Uses Places API (New) Nearby
    Search — the same already-enabled, already-billed API as everything else in
    this app — rather than the separate Geocoding API, which has its own billing
    rules in some regions (e.g. requires an authorized reseller in Saudi Arabia)."""
    lat = request.args.get("lat")
    lng = request.args.get("lng")
    if not lat or not lng:
        return jsonify({"error": "Missing lat/lng"}), 400

    body = {
        "maxResultCount": 1,
        "locationRestriction": {
            "circle": {
                "center": {"latitude": float(lat), "longitude": float(lng)},
                "radius": 2000.0,
            }
        },
    }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": "places.addressComponents",
    }
    resp = requests.post(
        "https://places.googleapis.com/v1/places:searchNearby", json=body, headers=headers
    ).json()

    places = resp.get("places", [])
    if not places:
        return jsonify({"error": "No nearby place found"}), 502

    components = places[0].get("addressComponents", [])

    def find(type_name, field="longText"):
        for c in components:
            if type_name in c.get("types", []):
                return c.get(field)
        return None

    city = find("locality") or find("administrative_area_level_2")
    country = find("country")
    country_code = find("country", field="shortText")

    return jsonify({"city": city, "country": country, "country_code": country_code})


@app.route("/api/cities")
def cities_autocomplete():
    """Live city suggestions as the user types, optionally restricted to a country."""
    input_text = request.args.get("input", "").strip()
    country_code = request.args.get("country_code", "").strip()

    if not input_text:
        return jsonify({"cities": []})

    body = {"input": input_text, "includedPrimaryTypes": ["locality"]}
    if country_code:
        body["includedRegionCodes"] = [country_code]

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": "suggestions.placePrediction.text",
    }
    resp = requests.post(
        "https://places.googleapis.com/v1/places:autocomplete", json=body, headers=headers
    ).json()

    cities = [
        s["placePrediction"]["text"]["text"]
        for s in resp.get("suggestions", [])
        if "placePrediction" in s
    ]
    return jsonify({"cities": cities})


@app.route("/logs")
def view_logs():
    provided_key = request.args.get("key", "")
    if not ADMIN_KEY or provided_key != ADMIN_KEY:
        return "Not found", 404

    entries = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    entries.reverse()  # most recent first
    return render_template("logs.html", entries=entries)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
