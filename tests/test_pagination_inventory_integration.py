from __future__ import annotations

from urllib.parse import urlencode

import pytest

from futureeval_bot.api import FeedPaginationIncomplete, Metaculus
from futureeval_bot.inventory import inventory_counts, release_inventory


CHECKED_AT = "2026-11-09T12:00:00+00:00"
BASE_FILTERS = {
    "tournaments": "33121",
    "include_descriptions": "true",
    "with_cp": "false",
    "limit": "100",
    "order_by": "scheduled_close_time",
}


def question_post(question_id: int) -> dict:
    return {
        "id": question_id + 1000,
        "published_at": "2026-11-08T10:00:00Z",
        "question": {
            "id": question_id,
            "type": "binary",
            "open_time": "2026-11-08T10:00:00Z",
            "scheduled_close_time": "2026-11-10T10:00:00Z",
            "actual_close_time": None,
        },
    }


def next_page(offset: int) -> str:
    return Metaculus.BASE + "/posts/?" + urlencode({**BASE_FILTERS, "offset": str(offset)})


def collect(pages: list[dict], *, statuses: list[str] | None = None):
    client = object.__new__(Metaculus)
    calls = []

    def fake_request(method, path, **kwargs):
        assert (method, path) == ("GET", "/posts/")
        params = kwargs["params"]
        calls.append({key: list(value) if isinstance(value, list) else value for key, value in params.items()})
        return pages.pop(0)

    client.request = fake_request
    try:
        posts = Metaculus.posts(client, 33121, statuses=statuses)
    except FeedPaginationIncomplete as error:
        return client, calls, error
    return client, calls, posts


def test_imported_metaculus_and_inventory_integrate_on_contiguous_pages():
    client, calls, posts = collect([
        {"results": [question_post(1)], "next": next_page(1)},
        {"results": [question_post(2)], "next": None},
    ])

    assert [call["offset"] for call in calls] == [0, 1]
    assert all(call["tournaments"] == 33121 for call in calls)
    inventory = release_inventory(posts, client.feed_pages, CHECKED_AT)
    counts = inventory_counts(inventory)
    assert inventory["visible_question_ids"] == [1, 2]
    assert inventory["pagination_complete"] is True
    assert counts["currently_visible_questions"] == 2
    assert counts["pagination_complete"] is True
    assert counts["complete_official_released_inventory"] is False


def test_missing_next_after_accumulated_page_raises_typed_incomplete():
    client, calls, error = collect([
        {"results": [question_post(1)], "next": next_page(1)},
        {"results": [question_post(2)]},
    ])

    assert isinstance(error, FeedPaginationIncomplete)
    assert error.failure_subtype == "continuation_missing"
    assert error.pagination_disposition == "continuation_rejected"
    assert (error.pages_completed, error.items_received, error.last_page_items) == (2, 2, 1)
    assert [call["offset"] for call in calls] == [0, 1]
    assert [page["has_next"] for page in client.feed_pages] == [True, True]


@pytest.mark.parametrize("indicator", [False, "", 0, [], {}], ids=["false", "empty", "zero", "list", "object"])
def test_invalid_non_null_next_after_accumulated_page_raises_typed_incomplete(indicator):
    client, calls, error = collect([
        {"results": [question_post(1)], "next": next_page(1)},
        {"results": [question_post(2)], "next": indicator},
    ])

    assert isinstance(error, FeedPaginationIncomplete)
    assert error.failure_subtype == "continuation_missing"
    assert error.pagination_disposition == "continuation_rejected"
    assert (error.pages_completed, error.items_received, error.last_page_items) == (2, 2, 1)
    assert [call["offset"] for call in calls] == [0, 1]


def test_server_offset_gap_raises_typed_incomplete_before_inventory():
    client, calls, error = collect([
        {"results": [question_post(1)], "next": next_page(50)},
        {"results": [question_post(2)], "next": None},
    ])

    assert isinstance(error, FeedPaginationIncomplete)
    assert error.failure_subtype == "continuation_offset_invalid"
    assert error.pagination_disposition == "continuation_rejected"
    assert (error.pages_completed, error.items_received, error.last_page_items) == (1, 1, 1)
    assert [call["offset"] for call in calls] == [0]
