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
from urllib.parse import quote
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")
TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
NEARBY_SEARCH_URL = "https://places.googleapis.com/v1/places:searchNearby"
FIELD_MASK = "places.id,places.displayName,places.rating,places.userRatingCount,places.formattedAddress"

CITY_RADIUS_METERS = 40000.0  # fallback only, used if a place has no viewport info
NEARBY_RADIUS_CAP = 50000.0   # Nearby Search's own max radius

# The 13 sidebar category shortcuts, mapped to Google's official Place types.
# Text Search has no "sort by popularity/reviews" option — only relevance or
# distance — so a broad query like "restaurants" can easily miss a city's
# most-reviewed places entirely. Nearby Search DOES support ranking by
# popularity, but only for exact category types like these (not free text),
# which is why only these known categories get routed through it.
CATEGORY_TYPE_MAP = {
    # The 13 sidebar buttons (plural, as labeled) + their singular forms,
    # since free-text search shouldn't require the exact button wording
    "restaurants": "restaurant",
    "restaurant": "restaurant",
    "hotels": "lodging",
    "hotel": "lodging",
    "car rental": "car_rental",
    "car rentals": "car_rental",
    "gyms": "gym",
    "libraries": "library",
    "library": "library",
    "parks": "park",
    "park": "park",
    "beauty salons": "beauty_salon",
    "beauty salon": "beauty_salon",
    "dry cleaning": "laundry",
    "hospitals": "hospital",
    "hospital": "hospital",
    "schools": "school",
    "school": "school",
    "malls": "shopping_mall",
    "mall": "shopping_mall",
    "mosques": "mosque",
    "mosque": "mosque",
    "grocery stores": "grocery_store",
    "grocery store": "grocery_store",
    # Common free-text searches, mapped to their real Google Places type —
    # covers terms beyond the sidebar so more typed searches get accurate,
    # popularity-ranked results instead of relevance-only Text Search.
    "barber shop": "barber_shop",
    "barbershop": "barber_shop",
    "barber": "barber_shop",
    "hair salon": "hair_salon",
    "hairdresser": "hair_salon",
    "nail salon": "nail_salon",
    "spa": "spa",
    "cafe": "cafe",
    "cafes": "cafe",
    "coffee shop": "cafe",
    "coffee shops": "cafe",
    "bakery": "bakery",
    "bakeries": "bakery",
    "pharmacy": "pharmacy",
    "pharmacies": "pharmacy",
    "dentist": "dentist",
    "dentists": "dentist",
    "doctor": "doctor",
    "clinic": "medical_clinic",
    "clinics": "medical_clinic",
    "bank": "bank",
    "banks": "bank",
    "atm": "atm",
    "gas station": "gas_station",
    "gas stations": "gas_station",
    "petrol station": "gas_station",
    "supermarket": "supermarket",
    "supermarkets": "supermarket",
    "bookstore": "book_store",
    "bookstores": "book_store",
    "pet store": "pet_store",
    "pet stores": "pet_store",
    "cinema": "movie_theater",
    "movie theater": "movie_theater",
    "gym": "gym",
    "fitness center": "fitness_center",
    "veterinary clinic": "veterinary_care",
    "vet": "veterinary_care",
}

# Longest phrases first, so a multi-word match (e.g. "coffee shop") is found
# before a shorter one that might also appear in the same text.
_CATEGORY_PHRASES_BY_LENGTH = sorted(CATEGORY_TYPE_MAP.keys(), key=len, reverse=True)


def extract_category_match(query):
    """Finds a known category phrase ANYWHERE in free text — not just an exact
    match — e.g. 'best barber shop near me' still finds 'barber shop'. Matches
    on whole words/phrases only (so 'spa' doesn't wrongly match inside 'spacious')."""
    q = query.lower()
    for phrase in _CATEGORY_PHRASES_BY_LENGTH:
        if re.search(r"\b" + re.escape(phrase) + r"\b", q):
            return CATEGORY_TYPE_MAP[phrase]
    return None

# Caches the city->coordinates/boundary lookup so searching the same city repeatedly
# (very common — e.g. your own current city) doesn't burn extra Places API quota.
# Resets whenever the app restarts/redeploys; that's fine, it just rebuilds itself.
GEOCODE_CACHE = {}
GEOCODE_CACHE_LOCK = threading.Lock()

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


NOT_A_PLACE_NAME = {"me", "here", "my location", "my area", "my current location"}


def extract_location_phrase(query):
    """Pulls out the location part of a query like 'malls in Cairo' -> 'Cairo'.
    Treats 'near me'/'near here' etc. as NOT an explicit location — those mean
    the user's current location, which is handled by the city/country fallback,
    not a literal place called 'me' to geocode."""
    match = re.search(r"\b(?:in|near|at)\s+(.+)$", query, re.IGNORECASE)
    if not match:
        return None
    phrase = match.group(1).strip()
    if phrase.lower() in NOT_A_PLACE_NAME:
        return None
    return phrase


def resolve_location_phrase(query, city, country):
    """An explicit 'in X' typed by the user always wins; otherwise falls back to
    the city/country the client sent (auto-detected, or chosen in Custom Filters).
    Returns (phrase, already_in_query_text) — the second value tells the caller
    whether the location is already embedded in the user's own text (True) or
    needs to be added to the search text itself (False)."""
    explicit = extract_location_phrase(query)
    if explicit:
        return explicit, True
    if city:
        phrase = f"{city}, {country}" if country and country.lower() not in city.lower() else city
        return phrase, False
    return None, False


def geocode_location(location_text):
    """Looks up a place's coordinates AND real boundary (viewport) using the same Places API.
    Cached by location text so the same city isn't re-resolved on every search."""
    cache_key = location_text.strip().lower()

    with GEOCODE_CACHE_LOCK:
        if cache_key in GEOCODE_CACHE:
            return GEOCODE_CACHE[cache_key]

    body = {"textQuery": location_text, "pageSize": 1}
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": "places.location,places.viewport",
    }
    resp = requests.post(TEXT_SEARCH_URL, json=body, headers=headers).json()
    places = resp.get("places", [])

    result = None
    if places:
        place = places[0]
        loc = place.get("location")
        if loc:
            result = {
                "lat": loc.get("latitude"),
                "lng": loc.get("longitude"),
                "viewport": place.get("viewport"),  # has 'low' and 'high' lat/lng bounds, or None
            }

    with GEOCODE_CACHE_LOCK:
        GEOCODE_CACHE[cache_key] = result

    return result


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


def build_nearby_circle(geo):
    """A circle (required shape for Nearby Search) sized to the city, capped at 50km."""
    viewport = geo.get("viewport")
    radius = CITY_RADIUS_METERS
    if viewport and viewport.get("low") and viewport.get("high"):
        lat_span = abs(viewport["high"]["latitude"] - viewport["low"]["latitude"])
        lng_span = abs(viewport["high"]["longitude"] - viewport["low"]["longitude"])
        # Rough degrees->meters conversion, just to size the circle reasonably.
        radius = max(lat_span, lng_span) * 111_000 / 2
    radius = min(max(radius, 1000.0), NEARBY_RADIUS_CAP)
    return {"latitude": geo["lat"], "longitude": geo["lng"]}, radius


def discover_primary_type(query, geo, headers):
    """When a query isn't one of our known phrases, ask Google itself what type
    of place it's most likely about — a single cheap lookup (1 result, one
    field) — so we can still route through the accurate, popularity-ranked
    tool for queries we never explicitly catalogued. This is the real fix for
    relevance-ranked results being wrong: instead of only trusting a fixed list
    of phrases, we let Google's own classification of a real matching place
    tell us the category, every time, for any query."""
    body = {"textQuery": query, "pageSize": 1}
    if geo:
        restriction = build_location_restriction(geo)
        if restriction:
            body["locationRestriction"] = restriction

    discover_headers = dict(headers, **{"X-Goog-FieldMask": "places.primaryType"})
    try:
        resp = requests.post(TEXT_SEARCH_URL, json=body, headers=discover_headers).json()
    except requests.RequestException:
        return None

    places = resp.get("places", [])
    if not places:
        return None
    return places[0].get("primaryType")


def _parse_places(raw_places):
    return [
        {
            "name": p.get("displayName", {}).get("text", "N/A"),
            "rating": p.get("rating"),
            "user_ratings_total": p.get("userRatingCount", 0),
            "formatted_address": p.get("formattedAddress"),
            "place_id": p.get("id"),
        }
        for p in raw_places
    ]


def search_places(query, city=None, country=None, max_pages=3):
    """Finds places for a query, hard-restricted to the resolved city.

    Known categories go through Nearby Search ranked by POPULARITY, since Text
    Search has no such ranking option and can miss a city's most-reviewed
    places under a broad relevance match (and only ever returns 60 results
    total). For a query that isn't in our known-phrase dictionary, we ask
    Google itself what type of place it's most likely about (one cheap
    lookup) and route through the same accurate path using that discovered
    type. Only if neither of those applies — or the discovered type turns out
    unusable for filtering — does it fall back to plain Text Search.
    """
    geo = None
    location_phrase, location_in_query_text = resolve_location_phrase(query, city, country)
    if location_phrase:
        geo = geocode_location(location_phrase)
        if not geo:
            # A location WAS specified but couldn't be resolved — better to say so
            # clearly than to silently run an unrestricted, potentially global search.
            raise RuntimeError(f"Could not find the location \"{location_phrase}\". Try a different spelling.")

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": FIELD_MASK,
    }

    # 1. An EXACT known phrase (the whole query, not just a word inside it) is
    # trusted outright — fast, no extra lookup, verified type string.
    exact_match_type = CATEGORY_TYPE_MAP.get(query.strip().lower())
    place_type = exact_match_type
    discovered = False

    if not exact_match_type and geo:
        # 2. Not an exact match — the query might still CONTAIN a known phrase
        # (e.g. "moroccan restaurants" contains "restaurants"), but it might
        # also have extra, more specific words ("moroccan") that phrase can't
        # capture. Ask Google what type this query is most likely about —
        # that discovered type, when available, is preferred for being more
        # specific than a generic partial match.
        discovered_type = discover_primary_type(query, geo, headers)
        if discovered_type:
            place_type = discovered_type
            discovered = True
        else:
            # Discovery found nothing — fall back to a partial/fuzzy match
            # (e.g. the generic "restaurant" from "moroccan restaurants")
            # rather than giving up and using the less accurate tool.
            place_type = extract_category_match(query)
            discovered = True  # not individually verified — treat failures as non-fatal

    # Nearby Search (POPULARITY-ranked), when a category is known or discovered.
    # NOTE: even this "accurate" tool has a real limit — "popularity" is still
    # Google's own black-box score, not literally sorted by raw review count,
    # and it caps at 20 results with NO pagination at all. So a genuinely
    # high-review place can still be outside that top-20 slice. That's why we
    # don't stop here — we also run Text Search below and merge both result
    # sets before doing our own definitive sort by review count. A place only
    # needs to be caught by ONE of the two tools to end up ranked correctly.
    nearby_places = []
    if place_type and geo:
        center, radius = build_nearby_circle(geo)
        nearby_body = {
            "includedPrimaryTypes": [place_type],
            "maxResultCount": 20,
            "rankPreference": "POPULARITY",
            "locationRestriction": {"circle": {"center": center, "radius": radius}},
        }
        resp = requests.post(NEARBY_SEARCH_URL, json=nearby_body, headers=headers).json()
        if "error" not in resp:
            nearby_places = _parse_places(resp.get("places", []))
        elif not discovered:
            # A phrase from our own verified dictionary failed — that's a real
            # problem worth surfacing, not silently hiding.
            raise RuntimeError(resp["error"].get("message", "Unknown error"))
        # A DISCOVERED type can occasionally be one Nearby Search can't filter
        # on (e.g. a Table B type) — fine, Text Search below still runs.

    # Text Search (relevance-ranked), with pagination up to max_pages.
    # Google's own Text Search docs recommend embedding the location directly in
    # the text (e.g. "Restaurants in New York") for reliable categorical-query
    # detection — a separate locationRestriction field alone isn't always enough,
    # especially for phrases Google doesn't confidently recognize as a business
    # category (e.g. "Barber shop"). So if the location came from auto-detection
    # or Custom Filters rather than the user's own typed text, add it to the text.
    text_query = query
    if location_phrase and not location_in_query_text:
        text_query = f"{query} in {location_phrase}"

    location_restriction = build_location_restriction(geo) if geo else None
    text_body = {"textQuery": text_query, "pageSize": 20}
    if location_restriction:
        text_body["locationRestriction"] = location_restriction

    text_places = []
    headers["X-Goog-FieldMask"] = FIELD_MASK + ",nextPageToken"
    for _ in range(max_pages):
        resp = requests.post(TEXT_SEARCH_URL, json=text_body, headers=headers).json()

        if "error" in resp:
            if nearby_places:
                break  # we still have the Nearby results; don't fail the whole search
            raise RuntimeError(resp["error"].get("message", "Unknown error"))

        text_places.extend(_parse_places(resp.get("places", [])))

        next_token = resp.get("nextPageToken")
        if not next_token:
            break

        time.sleep(2)  # short delay before a page token becomes valid
        text_body["pageToken"] = next_token

    # Merge both pools, deduplicated by place_id — a place only needs to be
    # found by one of the two tools to be correctly ranked afterward.
    merged = {}
    for p in nearby_places + text_places:
        pid = p.get("place_id")
        if pid and pid not in merged:
            merged[pid] = p
    return list(merged.values())


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
            "maps_url": (
                "https://www.google.com/maps/search/?api=1"
                f"&query={quote(p.get('name', ''))}"
                f"&query_place_id={p.get('place_id', '')}"
            ),
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
