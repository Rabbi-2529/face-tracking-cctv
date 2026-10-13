"""
Floor-plan geometry shared by the live board, the movement map and the
outline editor.

Zone outlines are stored as ``Zone.plan_polygon``: ``[[x, y], ...]`` in the
pixel coordinates of the floor's plan image. Everything here works in those
coordinates, and the browser scales the whole drawing with an SVG viewBox, so
nothing needs recomputing when the page is resized.
"""

from __future__ import annotations

from django.urls import reverse


def valid_polygon(points) -> list[list[float]] | None:
    """A clean ``[[x, y], ...]`` with at least three points, or None."""
    if not isinstance(points, list) or len(points) < 3:
        return None
    cleaned: list[list[float]] = []
    for point in points:
        if not isinstance(point, list | tuple) or len(point) != 2:
            return None
        try:
            x, y = float(point[0]), float(point[1])
        except (TypeError, ValueError):
            return None
        if x != x or y != y:  # NaN
            return None
        cleaned.append([round(x, 1), round(y, 1)])
    return cleaned


def centroid(points: list[list[float]]) -> tuple[float, float]:
    """Area centroid of a simple polygon, falling back to the vertex mean.

    The area centroid sits where a person would read "the middle of the
    room" even for an L-shaped outline, which a plain vertex average does not.
    """
    area = 0.0
    cx = cy = 0.0
    count = len(points)
    for index in range(count):
        x0, y0 = points[index]
        x1, y1 = points[(index + 1) % count]
        cross = x0 * y1 - x1 * y0
        area += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(area) < 1e-9:
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return round(sum(xs) / count, 1), round(sum(ys) / count, 1)
    area *= 0.5
    return round(cx / (6 * area), 1), round(cy / (6 * area), 1)


def svg_points(points: list[list[float]]) -> str:
    return " ".join(f"{x},{y}" for x, y in points)


def has_plan(floor) -> bool:
    return bool(floor.plan_image and floor.plan_width_px and floor.plan_height_px)


def floor_drawing(floor, zones) -> dict:
    """Everything a template needs to draw one floor with its zone outlines.

    ``zones`` must already be filtered to what the viewer may see. Zones
    without a usable outline are left off the drawing, not guessed at.
    """
    shapes = []
    for zone in zones:
        polygon = valid_polygon(zone.plan_polygon)
        if polygon is None:
            continue
        cx, cy = centroid(polygon)
        shapes.append(
            {
                "zone": zone,
                "id": zone.pk,
                "code": zone.code,
                "name": zone.name,
                "points": svg_points(polygon),
                "cx": cx,
                "cy": cy,
            }
        )
    return {
        "floor": floor,
        "image_url": reverse("org:floor_plan_image", args=[floor.pk]),
        "width": floor.plan_width_px,
        "height": floor.plan_height_px,
        "shapes": shapes,
        # Label size in plan pixels, so text stays legible on large plans.
        "font_size": max(12, round(min(floor.plan_width_px, floor.plan_height_px) / 40)),
    }
