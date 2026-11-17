from healthconsole.core import cards
from healthconsole.core.model import CRITICAL, HEALTHY, INFO, WARNING, red_or_yellow


def test_red_beats_yellow_and_the_rest_fall_back():
    assert red_or_yellow([WARNING, CRITICAL, INFO]) == CRITICAL
    assert red_or_yellow([INFO, WARNING]) == WARNING
    assert red_or_yellow([INFO, HEALTHY]) is None and red_or_yellow([], HEALTHY) == HEALTHY


def test_item_and_card():
    row = {"id": "/subscriptions/S/resourceGroups/rg/providers/Microsoft.Web/sites/shop", "type": "Microsoft.Web/sites",
           "kind": "app", "resourceGroup": "rg", "subscriptionId": "S"}
    item = cards.item(row, WARNING, "slow", fix="scale out")
    assert item["name"] == "shop" and item["display_type"] == "Web App" and item["subscription_id"] == "s"
    assert item["fix"] == "scale out"
    named = cards.item(row, INFO, "x", name="Shop (prod)", display_type="App Service")
    assert (named["name"], named["display_type"]) == ("Shop (prod)", "App Service")
    card = cards.card("k", "Title", "What it checks", [item, cards.item(row, CRITICAL, "down")], error="")
    assert card["status"] == CRITICAL and card["error"] is None  # an empty error is no error
    assert cards.card("k", "T", "D", [])["status"] is None
