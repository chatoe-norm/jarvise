"""Avoid venue STOP_LOSS_LIMIT HTTP in unit trade tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _mute_protective_stop():
    with patch(
        "jarvise_trade.submit.place_spot_stop_loss_limit",
        return_value={
            "orderId": 1,
            "status": "NEW",
            "executedQty": "0",
            "cummulativeQuoteQty": "0",
            "fills": [],
        },
    ) as mocked:
        yield mocked
