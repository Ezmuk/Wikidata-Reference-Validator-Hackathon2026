import unittest
from unittest.mock import patch

import app


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def complete_url_checks(coroutine):
    coroutine.close()
    return [{"url": "https://example.test", "status": "alive"}]


class LanguageSelectionTests(unittest.TestCase):
    def setUp(self):
        app.app.config["TESTING"] = True
        self.client = app.app.test_client()

    def test_homepage_offers_english_default_and_requested_languages(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'<option value="en" selected>English</option>', response.data)
        self.assertIn(b'<option value="fr">French</option>', response.data)
        self.assertIn(b'<option value="nl">Dutch</option>', response.data)

    @patch("app.requests.get")
    def test_search_uses_requested_language(self, get):
        get.return_value = FakeResponse({
            "search": [{"id": "Q42", "label": "Douglas Adams", "description": "English writer"}]
        })

        response = self.client.get("/search?q=Douglas&language=fr")

        self.assertEqual(response.status_code, 200)
        params = get.call_args.kwargs["params"]
        self.assertEqual(params["language"], "fr")
        self.assertEqual(params["uselang"], "fr")
        self.assertEqual(response.get_json()[0]["id"], "Q42")

    @patch("app.requests.get")
    def test_search_defaults_and_normalizes_invalid_language(self, get):
        get.return_value = FakeResponse({"search": []})

        self.client.get("/search?q=Paris")
        self.assertEqual(get.call_args.kwargs["params"]["language"], "en")
        self.client.get("/search?q=Paris&language=xx")
        self.assertEqual(get.call_args.kwargs["params"]["uselang"], "en")

    @patch("app.requests.get")
    def test_labels_prefer_selected_language_then_english_then_id(self, get):
        get.return_value = FakeResponse({
            "entities": {
                "P17": {"labels": {"fr": {"value": "Pays"}, "en": {"value": "Country"}}},
                "P36": {"labels": {"en": {"value": "Capital"}}},
                "P999": {"labels": {}},
            }
        })

        labels = app.fetch_labels({"P17", "P36", "P999"}, "fr")

        self.assertEqual(labels, {"P17": "Pays", "P36": "Capital", "P999": "P999"})
        self.assertEqual(get.call_args.kwargs["params"]["languages"], "fr|en")

    def test_statement_values_keep_literals_and_localize_entity_values(self):
        self.assertEqual(app.format_statement_value("Original text", {}), "Original text")
        self.assertEqual(
            app.format_statement_value({"id": "Q42"}, {"Q42": "Douglas Adams"}),
            "Douglas Adams"
        )
        self.assertEqual(
            app.format_statement_value({"text": "Bonjour", "language": "fr"}, {}),
            "Bonjour"
        )
        self.assertEqual(
            app.format_statement_value({"amount": "+12", "unit": "1"}, {}),
            "+12"
        )

    @patch("app.asyncio.run", side_effect=complete_url_checks)
    @patch("app.requests.get")
    def test_validate_uses_selected_language_for_property_and_item_labels(self, get, _run):
        claims = {
            "P17": [{
                "mainsnak": {"datavalue": {"value": {"id": "Q30"}}},
                "references": [{
                    "snaks": {
                        "P854": [{
                            "datavalue": {"value": {"uri": "https://example.test"}}
                        }]
                    }
                }]
            }]
        }
        get.side_effect = [
            FakeResponse({"entities": {"Q42": {"claims": claims}}}),
            FakeResponse({
                "entities": {
                    "P17": {"labels": {"fr": {"value": "Pays"}}},
                    "Q30": {"labels": {"fr": {"value": "Etats-Unis"}}},
                }
            })
        ]

        response = self.client.post(
            "/validate",
            json={"qid": "Q42", "language": "fr"}
        )

        self.assertEqual(response.status_code, 200)
        result = response.get_json()[0]
        self.assertEqual(result["propertyLabel"], "Pays")
        self.assertEqual(result["statementValue"], "Etats-Unis")
        self.assertEqual(get.call_args_list[1].kwargs["params"]["languages"], "fr|en")


if __name__ == "__main__":
    unittest.main()
