# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import boto3
import json
from moto import mock_s3
from taar.recommenders import RecommendationManager
from taar.recommenders.base_recommender import AbstractRecommender

from taar.recommenders.ensemble_recommender import (
    TAAR_ENSEMBLE_BUCKET,
    TAAR_ENSEMBLE_KEY,
)

from taar.settings import TAAR_WHITELIST_BUCKET, TAAR_WHITELIST_KEY

from .mocks import MockRecommenderFactory

import operator
from functools import reduce

from markus import TIMING
from markus.testing import MetricsMock


def install_mock_curated_data(ctx):
    mock_data = []
    for i in range(20):
        mock_data.append(str(i) * 16)

    ctx = ctx.child()
    conn = boto3.resource("s3", region_name="us-west-2")

    conn.create_bucket(Bucket=TAAR_WHITELIST_BUCKET)
    conn.Object(TAAR_WHITELIST_BUCKET, TAAR_WHITELIST_KEY).put(
        Body=json.dumps(mock_data)
    )

    return ctx


class StubRecommender(AbstractRecommender):
    """ A shared, stub recommender that can be used for testing.
    """

    def __init__(self, can_recommend, stub_recommendations):
        self._can_recommend = can_recommend
        self._recommendations = stub_recommendations

    def can_recommend(self, client_info, extra_data={}):
        return self._can_recommend

    def recommend(self, client_data, limit, extra_data={}):
        return self._recommendations


def install_mocks(ctx, mock_fetcher=None):
    ctx = ctx.child()

    class DefaultMockProfileFetcher:
        def get(self, client_id):
            return {"client_id": client_id}

    if mock_fetcher is None:
        mock_fetcher = DefaultMockProfileFetcher()

    ctx["profile_fetcher"] = mock_fetcher
    ctx["recommender_factory"] = MockRecommenderFactory()

    DATA = {
        "ensemble_weights": {"collaborative": 1000, "similarity": 100, "locale": 10,}
    }

    conn = boto3.resource("s3", region_name="us-west-2")
    conn.create_bucket(Bucket=TAAR_ENSEMBLE_BUCKET)
    conn.Object(TAAR_ENSEMBLE_BUCKET, TAAR_ENSEMBLE_KEY).put(Body=json.dumps(DATA))

    return ctx


@mock_s3
def test_none_profile_returns_empty_list(test_ctx):
    ctx = install_mocks(test_ctx)

    class MockProfileFetcher:
        def get(self, client_id):
            return None

    ctx["profile_fetcher"] = MockProfileFetcher()

    rec_manager = RecommendationManager(ctx)
    assert rec_manager.recommend("random-client-id", 10) == []


@mock_s3
def test_simple_recommendation(test_ctx):
    ctx = install_mocks(test_ctx)

    EXPECTED_RESULTS = [
        ("ghi", 3430.0),
        ("def", 3320.0),
        ("ijk", 3200.0),
        ("hij", 3100.0),
        ("lmn", 420.0),
        ("klm", 409.99999999999994),
        ("jkl", 400.0),
        ("abc", 23.0),
        ("fgh", 22.0),
        ("efg", 21.0),
    ]

    with MetricsMock() as mm:
        manager = RecommendationManager(ctx.child())
        recommendation_list = manager.recommend("some_ignored_id", 10)

        assert isinstance(recommendation_list, list)
        assert recommendation_list == EXPECTED_RESULTS

        assert mm.has_record(TIMING, stat="taar.ensemble")
        assert mm.has_record(TIMING, stat="taar.profile_recommendation")


@mock_s3
def test_fixed_client_id_valid(test_ctx):
    ctx = install_mocks(test_ctx)
    ctx = install_mock_curated_data(ctx)

    manager = RecommendationManager(ctx.child())
    recommendation_list = manager.recommend("111111", 10)

    assert len(recommendation_list) == 10


@mock_s3
def test_fixed_client_id_empty_list(test_ctx):
    class NoClientFetcher:
        def get(self, client_id):
            return None

    ctx = install_mocks(test_ctx, mock_fetcher=NoClientFetcher())

    ctx = install_mock_curated_data(ctx)

    manager = RecommendationManager(ctx.child())
    recommendation_list = manager.recommend("not_a_real_client_id", 10)

    assert len(recommendation_list) == 0


@mock_s3
def test_experimental_randomization(test_ctx):
    ctx = install_mocks(test_ctx)
    ctx = install_mock_curated_data(ctx)

    manager = RecommendationManager(ctx.child())
    raw_list = manager.recommend("111111", 10)

    # Clobber the experiment probability to be 100% to force a
    # reordering.
    ctx["TAAR_EXPERIMENT_PROB"] = 1.0

    manager = RecommendationManager(ctx.child())
    rand_list = manager.recommend("111111", 10)

    """
    The two lists should be :

    * different (guid, weight) lists (possibly just order)
    * same length
    """
    assert (
        reduce(
            operator.and_,
            [
                (t1[0] == t2[0] and t1[1] == t2[1])
                for t1, t2 in zip(rand_list, raw_list)
            ],
        )
        is False
    )

    assert len(rand_list) == len(raw_list)
