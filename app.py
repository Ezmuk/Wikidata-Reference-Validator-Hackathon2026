import json
import re

from flask import Flask, request, jsonify, render_template
import requests
import aiohttp
import asyncio

app = Flask(__name__)

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WAYBACK_API = "https://archive.org/wayback/available"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/115.0.0.0 Safari/537.36"
}

SUPPORTED_LANGUAGES = {
    "en", "fr", "es", "de", "it", "pt", "sw", "ar", "zh", "ja", "nl"
}


def normalize_language(language):
    if not isinstance(language, str):
        return "en"

    language = language.strip().lower()
    return language if language in SUPPORTED_LANGUAGES else "en"


def language_fallbacks(language):
    return [language] if language == "en" else [language, "en"]


# ---------------------------------------------------
# ROUTES
# ---------------------------------------------------
@app.route("/")
def home():
    return render_template("index.html")


@app.route("/about")
def about():
    return render_template("about.html")

@app.route("/use")
def use():
    return render_template("use.html")


@app.route("/report")
def report():
    return render_template("report.html")


@app.route("/search")
def search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify([])

    language = normalize_language(request.args.get("language"))
    params = {
        "action": "wbsearchentities",
        "search": query,
        "language": language,
        "uselang": language,
        "format": "json"
    }

    try:
        response = requests.get(WIKIDATA_API, params=params, headers=HEADERS, timeout=10)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("search"), list):
            raise ValueError("Invalid search response")
    except (requests.RequestException, ValueError):
        app.logger.exception("Wikidata search request failed")
        return jsonify({"error": "Wikidata search is temporarily unavailable."}), 502

    search_items = data["search"]
    missing_labels = {
        item["id"] for item in search_items
        if isinstance(item, dict) and item.get("id") and not item.get("label")
    }
    fallback_labels = fetch_labels(missing_labels, language)

    results = []
    for item in search_items:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        results.append({
            "id": item_id,
            "label": item.get("label") or fallback_labels.get(item_id) or item_id,
            "description": item.get("description", ""),
        })

    return jsonify(results)


# ---------------------------------------------------
# URL STATUS CHECK
# ---------------------------------------------------

async def check_url(session, url):
    try:
        async with session.head(url, allow_redirects=True, timeout=5) as resp:
            if resp.status < 400 or resp.status in (401, 403):
                return {"url": url, "status": "alive"}
    except Exception:
        pass

    try:
        async with session.get(url, allow_redirects=True, timeout=10) as resp:
            if resp.status < 400 or resp.status in (401, 403):
                return {"url": url, "status": "alive"}
            return {"url": url, "status": "dead"}
    except Exception:
        return {"url": url, "status": "dead"}


# ---------------------------------------------------
# WAYBACK MACHINE HELPER  ✅ FIXED
# ---------------------------------------------------

def fetch_wayback_snapshot(url):
    """
    Fetch latest available Wayback Machine snapshot for a URL.
    Returns None if no archive exists.
    """
    try:
        params = {"url": url}
        r = requests.get(WAYBACK_API, params=params, timeout=10)
        r.raise_for_status()

        data = r.json()
        snapshot = data.get("archived_snapshots", {}).get("closest")

        if snapshot and snapshot.get("available"):
            return {
                "archiveUrl": snapshot.get("url"),
                "timestamp": snapshot.get("timestamp"),
                "status": snapshot.get("status")
            }

    except Exception as e:
        print(f"[Wayback] Failed for {url}: {e}")

    return None


# ---------------------------------------------------
# LABEL FETCHING
# ---------------------------------------------------

def fetch_labels(ids, language="en"):
    labels = {}
    if not ids:
        return labels

    language = normalize_language(language)
    languages = "|".join(language_fallbacks(language))
    id_list = sorted(set(ids))
    chunks = [id_list[i:i + 50] for i in range(0, len(id_list), 50)]

    for chunk in chunks:
        params = {
            "action": "wbgetentities",
            "ids": "|".join(chunk),
            "format": "json",
            "props": "labels",
            "languages": languages
        }

        try:
            r = requests.get(WIKIDATA_API, params=params, headers=HEADERS, timeout=10)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, dict) or not isinstance(data.get("entities"), dict):
                raise ValueError("Invalid label response")
            entities = data["entities"]

            for eid, entity_data in entities.items():
                entity_labels = (
                    entity_data.get("labels", {})
                    if isinstance(entity_data, dict) else {}
                )
                label = next(
                    (
                        entity_labels.get(lang, {}).get("value")
                        for lang in language_fallbacks(language)
                        if entity_labels.get(lang, {}).get("value")
                    ),
                    eid
                )
                labels[eid] = label

        except (requests.RequestException, ValueError) as error:
            app.logger.warning("Wikidata label lookup failed for %s: %s", chunk, error)
            labels.update({entity_id: entity_id for entity_id in chunk})

    return labels


def statement_entity_ids(datavalue):
    if not isinstance(datavalue, dict):
        return set()

    entity_id = datavalue.get("id")
    ids = {entity_id} if isinstance(entity_id, str) else set()

    quantity = datavalue.get("amount")
    unit = datavalue.get("unit")
    if quantity is not None and isinstance(unit, str):
        match = re.search(r"/entity/(Q\d+)$", unit)
        if match:
            ids.add(match.group(1))

    return ids


def format_statement_value(datavalue, labels):
    if isinstance(datavalue, dict):
        if "id" in datavalue:
            entity_id = datavalue["id"]
            return labels.get(entity_id, entity_id)
        if "text" in datavalue and "language" in datavalue:
            return str(datavalue["text"])
        if "amount" in datavalue:
            amount = str(datavalue["amount"])
            unit = datavalue.get("unit", "1")
            unit_match = re.search(r"/entity/(Q\d+)$", unit) if isinstance(unit, str) else None
            if unit_match:
                return f"{amount} {labels.get(unit_match.group(1), unit_match.group(1))}"
            return amount if unit == "1" else f"{amount} {unit}"
        if "time" in datavalue:
            return str(datavalue["time"]).lstrip("+")
        if "latitude" in datavalue and "longitude" in datavalue:
            return f'{datavalue["latitude"]}, {datavalue["longitude"]}'
        if "uri" in datavalue:
            return str(datavalue["uri"])
        return json.dumps(datavalue, ensure_ascii=False, sort_keys=True)

    if datavalue is None:
        return ""
    if isinstance(datavalue, (str, int, float, bool)):
        return str(datavalue)
    return json.dumps(datavalue, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------
# VALIDATE REFERENCES  ✅ CORRECT ROUTE
# ---------------------------------------------------

@app.route("/validate", methods=["POST"])
def validate_references():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("qid"), str):
        return jsonify({"error": "Missing QID"}), 400

    qid = data["qid"].strip()
    if not qid:
        return jsonify({"error": "Missing QID"}), 400
    language = normalize_language(data.get("language"))

    try:
        params = {
            "action": "wbgetentities",
            "ids": qid,
            "format": "json",
            "props": "claims"
        }

        r = requests.get(WIKIDATA_API, params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()

        entity = r.json().get("entities", {}).get(qid, {})
        claims = entity.get("claims", {})

        refs_data = []
        all_ids_to_label = set()

        for prop, statements in claims.items():
            for stmt in statements:
                mainsnak = stmt.get("mainsnak", {})
                datavalue = mainsnak.get("datavalue", {}).get("value")

                for ref in stmt.get("references", []):
                    for snaks in ref.get("snaks", {}).values():
                        for snak in snaks:
                            dv = snak.get("datavalue", {}).get("value")
                            ref_url = None

                            if isinstance(dv, dict) and "uri" in dv:
                                ref_url = dv["uri"]
                            elif isinstance(dv, str) and dv.startswith("http"):
                                ref_url = dv

                            if ref_url:
                                refs_data.append({
                                    "url": ref_url,
                                    "property": prop,
                                    "datavalue": datavalue
                                })
                                all_ids_to_label.add(prop)
                                all_ids_to_label.update(statement_entity_ids(datavalue))

        if not refs_data:
            return jsonify({"message": "No references found"}), 200

        async def run_checks():
            async with aiohttp.ClientSession(headers=HEADERS) as session:
                tasks = [check_url(session, ref["url"]) for ref in refs_data]
                return await asyncio.gather(*tasks)

        url_statuses = asyncio.run(run_checks())
        labels = fetch_labels(all_ids_to_label, language)

        results = []
        for ref_data, status_data in zip(refs_data, url_statuses):
            result = {
                "url": ref_data["url"],
                "status": status_data["status"],
                "propertyLabel": labels.get(ref_data["property"], ref_data["property"]),
                "statementValue": format_statement_value(ref_data["datavalue"], labels)
            }

            # 🔁 Wayback suggestion ONLY for dead refs
            if status_data["status"] == "dead":
                archive = fetch_wayback_snapshot(ref_data["url"])
                if archive:
                    result["suggestedArchive"] = archive

            results.append(result)

        return jsonify(results)

    except (requests.RequestException, ValueError, KeyError) as error:
        app.logger.exception("Wikidata validation request failed: %s", error)
        return jsonify({"error": "Unable to retrieve Wikidata item data right now."}), 502
    except Exception:
        app.logger.exception("Reference validation failed")
        return jsonify({"error": "Reference validation could not be completed."}), 500


# ---------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True)
