# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from srgutil.interfaces import IMozLogging
import itertools
from .base_recommender import AbstractRecommender

from taar.utils import hasher
from taar.recommenders.redis_cache import TAARCache
import time

import markus

metrics = markus.get_metrics("taar")


def is_test_client(client_id):
    """ any client_id where the GUID is composed of a single digit
    (repeating) is a test id """
    return len(set(client_id.replace("-", ""))) == 1


class EnsembleRecommender(AbstractRecommender):
    """
    The EnsembleRecommender is a collection of recommenders where the
    results from each recommendation is amplified or dampened by a
    factor.  The aggregate results are combines and used to recommend
    addons for users.
    """

    def __init__(self, ctx):
        self.RECOMMENDER_KEYS = ["collaborative", "similarity", "locale"]
        self._ctx = ctx

        self._redis_cache = TAARCache.get_instance(self._ctx)
        self.logger = self._ctx[IMozLogging].get_logger("taar.ensemble")

        assert "recommender_factory" in self._ctx

        self._init_from_ctx()

    def _get_cache(self, extra_data):
        tmp = extra_data.get("cache", None)
        if tmp is None:
            tmp = self._redis_cache.cache_context()
        return tmp

    def getWeights(self):
        return self._redis_cache.ensemble_weights()

    def _init_from_ctx(self):
        # Copy the map of the recommenders
        self._recommender_map = {}

        recommender_factory = self._ctx["recommender_factory"]
        for rkey in self.RECOMMENDER_KEYS:
            self._recommender_map[rkey] = recommender_factory.create(rkey)

        self.logger.info("EnsembleRecommender initialized")

    def can_recommend(self, client_data, extra_data={}):
        """The ensemble recommender is always going to be
        available if at least one recommender is available"""
        result = sum(
            [
                self._recommender_map[rkey].can_recommend(client_data)
                for rkey in self.RECOMMENDER_KEYS
            ]
        )
        self.logger.info("Ensemble can_recommend: {}".format(result))
        return result

    @metrics.timer_decorator("ensemble_recommend")
    def recommend(self, client_data, limit, extra_data={}):
        cache = self._get_cache(extra_data)
        client_id = client_data.get("client_id", "no-client-id")

        if is_test_client(client_id):
            whitelist = cache["whitelist"]
            samples = whitelist[:limit]
            self.logger.info("Test ID detected [{}]".format(client_id))

            # Compute a stable weight for any whitelisted addon based
            # on the sha256 hash of the GUID
            p = [(int(hasher(s), 16) % 100) / 100.0 for s in samples]
            results = list(zip(samples, p))
        else:
            try:
                metrics.incr("error_ensemble", value=1)
                results = self._recommend(client_data, limit, extra_data)
            except Exception as e:
                results = []
                self.logger.exception(
                    "Ensemble recommender crashed for {}".format(client_id), e
                )
        return results

    def _recommend(self, client_data, limit, extra_data={}):
        """
        Ensemble recommendations are aggregated from individual
        recommenders.  The ensemble recommender applies a weight to
        the recommendation outputs of each recommender to reorder the
        recommendations to be a better fit.

        The intuitive understanding is that the total space of
        recommended addons across all recommenders will include the
        'true' addons that should be recommended better than any
        individual recommender.  The ensemble method simply needs to
        weight each recommender appropriate so that the ordering is
        correct.
        """
        cache = self._get_cache(extra_data)
        self.logger.info("Ensemble recommend invoked")
        preinstalled_addon_ids = client_data.get("installed_addons", [])

        # Compute an extended limit by adding the length of
        # the list of any preinstalled addons.
        extended_limit = limit + len(preinstalled_addon_ids)

        flattened_results = []
        ensemble_weights = cache["ensemble_weights"]

        for rkey in self.RECOMMENDER_KEYS:
            rec_start = time.time()
            self.logger.info(f"{rkey} recommend starting")
            recommender = self._recommender_map[rkey]
            if recommender.can_recommend(client_data, extra_data):
                raw_results = recommender.recommend(
                    client_data, extended_limit, extra_data
                )
                reweighted_results = []
                for guid, weight in raw_results:
                    item = (guid, weight * ensemble_weights[rkey])
                    reweighted_results.append(item)
                flattened_results.extend(reweighted_results)
            self.logger.info(
                f"{rkey} recommend ended in {time.time()-rec_start} seconds"
            )

        # Sort the results by the GUID
        flattened_results.sort(key=lambda item: item[0])

        # group by the guid, sum up the weights for recurring GUID
        # suggestions across all recommenders
        guid_grouper = itertools.groupby(flattened_results, lambda item: item[0])

        ensemble_suggestions = []
        for (guid, guid_group) in guid_grouper:
            weight_sum = sum([v for (g, v) in guid_group])
            item = (guid, weight_sum)
            ensemble_suggestions.append(item)

        # Sort in reverse order (greatest weight to least)
        ensemble_suggestions.sort(key=lambda x: -x[1])

        filtered_ensemble_suggestions = [
            (guid, weight)
            for (guid, weight) in ensemble_suggestions
            if guid not in preinstalled_addon_ids
        ]

        results = filtered_ensemble_suggestions[:limit]

        log_data = (
            client_data["client_id"],
            extra_data.get("guid_randomization", False),
            str(ensemble_weights),
            str([r[0] for r in results]),
        )
        self.logger.info(
            "client_id: [%s], guid_randomization: [%s], ensemble_weight: [%s], guids: [%s]"
            % log_data
        )
        return results
