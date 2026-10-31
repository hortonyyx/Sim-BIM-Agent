"""Reusable durable budget projections, isolated from mutable admission attempts."""
from __future__ import annotations

from .budget import RuntimeBudget


def load_budget_projection(store, total_limit, *, task_id=None,
                          near_limit_policy="stop", min_output_tokens=1,
                          pricing=None, cny_pricing=None):
    root = store._root
    revision = root.budget_revision
    if getattr(root, "_budget_projection_revision", None) != revision:
        root._budget_projection_revision = revision
        root._budget_projection_cache = {}
    key = (task_id, total_limit.model_dump_json(), near_limit_policy,
           min_output_tokens, pricing.model_dump_json() if pricing else None,
           cny_pricing)
    cache = root._budget_projection_cache
    if key not in cache:
        cache[key] = RuntimeBudget.from_events(total_limit,
            root.budget_projection_events(task_id=task_id),
            near_limit_policy=near_limit_policy, min_output_tokens=min_output_tokens,
            pricing=pricing, cny_pricing=cny_pricing)
    # reserve/settle mutate their budget instance before journal append. Never
    # expose the canonical cache, including to a second child in the same turn.
    return cache[key].clone()
