"""An immutable dual-clock binding shared by one owned service session."""
from __future__ import annotations

from .decision_lineage import canonical_sha256
from .news_review_clock_policy_review import BoundWindow, DualDeadlineReference, MAX_STEP_MS
from .news_review_contracts import activation_reading, require
from .news_review_windows_clock import sample_windows_clock

EXPIRY_REASONS = frozenset({"absolute_deadline_reached", "elapsed_deadline_reached"})


def clock_faults(verdict):
    """Expiry closes authority, but cannot hide a simultaneous observation fault."""
    metrics = verdict.get('metrics', {})
    faults = [key for key in ('clock_step_exceeded', 'clock_step_limit_unconfirmed',
        'observation_gap_exceeded', 'observation_gap_limit_unconfirmed',
        'wall_rollback_limit_unconfirmed') if metrics.get(key)]
    if metrics.get('wall_rollback_from_high_water_ms', 0)-metrics.get('wall_quantization_uncertainty_ms', 0) > MAX_STEP_MS:
        faults.append('wall_clock_rollback')
    return faults


class BoundRuntimeClock:
    def __init__(self, policy, *, sample=None):
        self.policy_sha256 = canonical_sha256(policy)
        self.activation = activation_reading(policy["clock_activation"])
        self.reference = DualDeadlineReference(BoundWindow(self.policy_sha256,
            policy["not_before_ms"], policy["expires_at_ms"], self.activation), sample or sample_windows_clock)
        self.installed = False
        self.owner = None

    def check(self, *, allow_expiry=False):
        verdict = self.reference.check()
        if not verdict["within_proposed_time_bounds"]:
            require(not clock_faults(verdict), "clock_changed")
            expired = verdict["stop_reason"] in EXPIRY_REASONS
            if not (expired and allow_expiry):
                require(False, "policy_expired" if expired else "clock_changed")
        return verdict

    def expired(self):
        first = self.reference.snapshot()["first_stop"]
        return bool(first and first["stop_reason"] in EXPIRY_REASONS)

    def deadline(self):
        self.check()
        return self.reference.snapshot()["effective_elapsed_deadline_ms"]

    def snapshot(self):
        state = self.reference.snapshot()
        return {"version": "news_review_runtime_clock_v1", "clock_policy": "dual_deadline_windows_v1",
                "policy_sha256": self.policy_sha256, "activation_reading": dict(vars(self.activation)),
                "service_binding_installed": self.installed, "reference": state,
                "expected_window_end_observed": self.expired(), "request_authorized": False}


def require_bound_clock(store, policy):
    binding = getattr(store, "_news_review_clock_bindings", {}).get(policy["policy_id"])
    require(type(binding) is BoundRuntimeClock and binding.installed
            and binding.policy_sha256 == canonical_sha256(policy), "clock_activation_required")
    require(binding.owner is not None, "clock_activation_required")
    binding.owner.assert_held_for(store.path)
    return binding
