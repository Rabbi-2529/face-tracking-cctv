"""
Accuracy measurement against a labelled set - the Phase 6 gate.

Pure numpy, no Django and no models, so the arithmetic is testable on its
own. ``scripts/benchmark_accuracy.py`` feeds it real embeddings.

Two views of the same scores:

* **Verification** (genuine vs impostor score distributions): for each
  threshold, how many true matches it accepts (TAR) and how many wrong
  people it accepts (FAR). This is what picks the thresholds.
* **Open-set identification**, which is what the live system actually does:
  each probe is matched against the whole gallery, and the outcome is
  confirmed-correct, confirmed-wrong, probable, or unknown. Confirmed-wrong
  is the number that matters most - a wrong name presented as certain.

The plan's acceptance targets (CCTV_TRACKING_PLAN.md section 14) are checked
here too: at least 95% correct identification of enrolled people, and at most
2% of probes given a confirmed wrong name.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np

TARGET_CORRECT = 0.95
TARGET_FALSE_POSITIVE = 0.02
UNKNOWN_LABEL = "_unknown"


def _unit(vectors) -> np.ndarray:
    array = np.asarray(vectors, dtype=np.float32)
    if array.ndim == 1:
        array = array[None, :]
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return array / norms


@dataclass
class Gallery:
    """Enrolled templates, as the live matcher holds them."""

    labels: list[str]
    matrix: np.ndarray

    @classmethod
    def build(cls, templates: dict[str, list]) -> Gallery:
        labels, rows = [], []
        for name, vectors in templates.items():
            for vector in vectors:
                labels.append(name)
                rows.append(vector)
        matrix = _unit(rows) if rows else np.zeros((0, 512), dtype=np.float32)
        return cls(labels, matrix)

    def best_per_person(self, embedding) -> dict[str, float]:
        """Highest similarity to each enrolled person (their best template)."""
        scores = self.matrix @ _unit(embedding)[0]
        best: dict[str, float] = {}
        for label, score in zip(self.labels, scores, strict=True):
            if score > best.get(label, -1.0):
                best[label] = float(score)
        return best


def score_distributions(gallery: Gallery, probes: list[tuple[str, object]]):
    """Genuine scores (probe vs own templates) and impostor scores (vs everyone else)."""
    genuine, impostor = [], []
    for label, embedding in probes:
        for person, score in gallery.best_per_person(embedding).items():
            (genuine if person == label else impostor).append(score)
    return np.asarray(genuine, dtype=np.float32), np.asarray(impostor, dtype=np.float32)


def sweep(genuine: np.ndarray, impostor: np.ndarray, step: float = 0.01) -> list[dict]:
    """TAR and FAR at each threshold from 0 to 1."""
    rows = []
    for threshold in np.arange(0.0, 1.0 + 1e-9, step):
        rows.append(
            {
                "threshold": round(float(threshold), 2),
                "tar": float((genuine >= threshold).mean()) if genuine.size else 0.0,
                "far": float((impostor >= threshold).mean()) if impostor.size else 0.0,
            }
        )
    return rows


# Never recommend thresholds below these, whatever the data says. A small or
# easy test set can make any threshold look safe; these floors stop a
# benchmark on thin data from talking anyone into confidently naming the
# wrong person. (The Matching page refuses a confirmed threshold under 0.30.)
FLOOR_HIGH = 0.40
FLOOR_LOW = 0.25
# A false-accept rate of 1 in N cannot be measured from fewer than N
# wrong-person comparisons, and a handful of people is not a population.
MIN_IDENTITIES = 10


def recommend(
    genuine: np.ndarray,
    impostor: np.ndarray,
    *,
    target_far: float = 0.001,
    keep_genuine: float = 0.99,
    identities: int | None = None,
) -> dict:
    """Suggested thresholds, and whether the data can support them.

    ``high`` (confirmed): the lowest threshold at which no more than
    ``target_far`` of impostor comparisons pass - so a confirmed match is
    almost never the wrong person - but never below ``FLOOR_HIGH``.
    ``low`` (probable): low enough to keep ``keep_genuine`` of true matches
    above "unknown", always below ``high`` and never below ``FLOOR_LOW``.

    ``sufficient`` is False when there are too few impostor comparisons to
    measure ``target_far``, or too few people; the caller should then keep
    its current thresholds and collect more data.
    """
    rows = sweep(genuine, impostor)
    measured_high = next((r["threshold"] for r in rows if r["far"] <= target_far), 1.0)
    high = round(max(measured_high, FLOOR_HIGH), 2)
    low = float(np.quantile(genuine, 1 - keep_genuine)) if genuine.size else high - 0.15
    low = round(max(min(low, high - 0.05), FLOOR_LOW), 2)
    at_high = next(r for r in rows if abs(r["threshold"] - high) < 1e-9)

    needed = int(np.ceil(1 / target_far)) if target_far > 0 else 0
    reasons = []
    if impostor.size < needed:
        reasons.append(
            f"only {impostor.size} wrong-person comparisons; at least {needed} are "
            f"needed to measure a {target_far:.1%} false-accept rate"
        )
    if identities is not None and identities < MIN_IDENTITIES:
        reasons.append(f"only {identities} people; use at least {MIN_IDENTITIES}")

    return {
        "high": high,
        "low": low,
        "measured_high": measured_high,
        "floored": high > measured_high,
        "tar_at_high": at_high["tar"],
        "far_at_high": at_high["far"],
        "target_far": target_far,
        "sufficient": not reasons,
        "insufficient_reasons": reasons,
    }


@dataclass
class IdentificationReport:
    high: float
    low: float
    enrolled_probes: int = 0
    stranger_probes: int = 0
    confirmed_correct: int = 0
    confirmed_wrong: int = 0
    probable_correct: int = 0
    probable_wrong: int = 0
    missed: int = 0  # enrolled person left "unknown"
    stranger_rejected: int = 0
    stranger_confirmed: int = 0  # a non-enrolled person given a confirmed name
    wrong_examples: list[dict] = field(default_factory=list)

    @property
    def correct_rate(self) -> float:
        return self.confirmed_correct / self.enrolled_probes if self.enrolled_probes else 0.0

    @property
    def false_positive_rate(self) -> float:
        total = self.enrolled_probes + self.stranger_probes
        wrong = self.confirmed_wrong + self.stranger_confirmed
        return wrong / total if total else 0.0

    @property
    def passes(self) -> bool:
        return (
            self.correct_rate >= TARGET_CORRECT
            and self.false_positive_rate <= TARGET_FALSE_POSITIVE
        )

    def as_dict(self) -> dict:
        data = asdict(self)
        data.update(
            correct_rate=round(self.correct_rate, 4),
            false_positive_rate=round(self.false_positive_rate, 4),
            passes=self.passes,
            targets={"correct": TARGET_CORRECT, "false_positive": TARGET_FALSE_POSITIVE},
        )
        return data


def identify(
    gallery: Gallery, probes: list[tuple[str, object]], *, high: float, low: float
) -> IdentificationReport:
    """Open-set identification, decided exactly as the live matcher decides."""
    report = IdentificationReport(high=high, low=low)
    for label, embedding in probes:
        best = gallery.best_per_person(embedding)
        name, score = max(best.items(), key=lambda kv: kv[1]) if best else (None, 0.0)
        stranger = label == UNKNOWN_LABEL or label not in set(gallery.labels)

        if stranger:
            report.stranger_probes += 1
            if score >= high:
                report.stranger_confirmed += 1
                report.wrong_examples.append({"probe": label, "matched": name, "score": round(score, 3)})
            else:
                report.stranger_rejected += 1
            continue

        report.enrolled_probes += 1
        if score >= high:
            if name == label:
                report.confirmed_correct += 1
            else:
                report.confirmed_wrong += 1
                report.wrong_examples.append({"probe": label, "matched": name, "score": round(score, 3)})
        elif score >= low:
            if name == label:
                report.probable_correct += 1
            else:
                report.probable_wrong += 1
        else:
            report.missed += 1
    return report
