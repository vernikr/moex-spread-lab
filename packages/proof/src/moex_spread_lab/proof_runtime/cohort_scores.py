"""Canonical aggregation of independently evaluated cohort shards."""

from __future__ import annotations

from statistics import fmean


def failure_payload(failure):
    return {
        "utility": None,
        "rank_score": None,
        "reason": f"{failure[1]}: {failure[2]}",
    }


def score_payload(scores):
    scores.sort(key=lambda item: item[0])
    ordered_scores = [item[1] for item in scores]
    exposed = any(item[2] for item in scores)
    score = fmean(ordered_scores)
    return {
        "rank_score": score,
        "by_window_pp": ordered_scores,
        "utility": score if exposed else None,
        **({} if exposed else {"reason": "Открытого спреда не было на обучении."}),
    }


def combine_shards(shard_results, initial_scores=()):
    min_fail = None
    scores = list(initial_scores)
    for sub_fail, sub_scores in shard_results:
        if sub_fail is not None and (min_fail is None or sub_fail[0] < min_fail[0]):
            min_fail = sub_fail
        scores.extend(sub_scores)
    return min_fail, scores


def point_result(cohort_state, canary_box, shard_results, initial_scores=()):
    failure, scores = combine_shards(shard_results, initial_scores)
    if failure is not None:
        canary_box[0] = failure[0]
        cohort_state["canary_active"] = True
        return failure_payload(failure)
    return score_payload(scores)
