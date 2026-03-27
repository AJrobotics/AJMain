"""
Floor Plan Post-Processing Pipeline for RosMaster.

Converts a raw occupancy grid (600x600, log-odds) into a clean floor plan
with straight wall lines, room segmentation, and SVG export.

Dependencies: numpy, scipy (no OpenCV).

Usage:
    from training.floor_plan_processor import process_grid, FloorPlan

    grid = np.load('map.npz')['grid']  # 600x600 log-odds
    fp = process_grid(grid, cell_size_mm=50)
    fp.to_svg('floor_plan.svg')
    data = fp.to_json()
"""

import math
import json
import numpy as np
from dataclasses import dataclass, field
from typing import List, Tuple, Optional

# scipy imports for morphology and connected components
from scipy.ndimage import binary_erosion, binary_dilation, label as ndlabel
from scipy.ndimage import generate_binary_structure


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

Segment = Tuple[float, float, float, float]  # (x1_mm, y1_mm, x2_mm, y2_mm)


@dataclass
class Room:
    """A room identified by connected-component analysis of free space."""
    id: int
    label: str
    cells: int              # number of free cells in this room
    area_m2: float          # area in square meters
    centroid: Tuple[float, float]  # (x_mm, y_mm) for label placement
    bbox: Tuple[int, int, int, int]  # (min_col, min_row, max_col, max_row) in grid coords
    boundary: List[Tuple[float, float]] = field(default_factory=list)  # simplified polygon (x_mm, y_mm)


@dataclass
class FloorPlan:
    """Result of the floor plan processing pipeline."""
    walls: List[Segment]
    rooms: List[Room]
    grid_size: int
    cell_size_mm: float
    wall_image: Optional[np.ndarray] = None  # binary image used for line detection

    def to_svg(self, filename: str, margin_mm: float = 500):
        """Export floor plan as an SVG file.

        Args:
            filename: output SVG path
            margin_mm: margin around the content in mm
        """
        if not self.walls and not self.rooms:
            # Nothing to draw
            with open(filename, 'w') as f:
                f.write('<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
                        '<text x="10" y="50" font-size="12">No data</text></svg>')
            return

        # Determine bounding box from walls and room centroids
        all_x, all_y = [], []
        for x1, y1, x2, y2 in self.walls:
            all_x.extend([x1, x2])
            all_y.extend([y1, y2])
        for room in self.rooms:
            for px, py in room.boundary:
                all_x.append(px)
                all_y.append(py)

        if not all_x:
            all_x = [0, self.grid_size * self.cell_size_mm]
            all_y = [0, self.grid_size * self.cell_size_mm]

        min_x = min(all_x) - margin_mm
        min_y = min(all_y) - margin_mm
        max_x = max(all_x) + margin_mm
        max_y = max(all_y) + margin_mm
        width = max_x - min_x
        height = max_y - min_y

        # Scale: 1 pixel = 10mm (reasonable for floor plans)
        scale = 0.1
        svg_w = width * scale
        svg_h = height * scale

        lines = []
        lines.append(f'<svg xmlns="http://www.w3.org/2000/svg" '
                      f'width="{svg_w:.0f}" height="{svg_h:.0f}" '
                      f'viewBox="{min_x} {min_y} {width} {height}">')
        lines.append('<defs>')
        lines.append('  <style>')
        lines.append('    .wall { stroke: #000; stroke-width: 60; stroke-linecap: round; fill: none; }')
        lines.append('    .room-fill { fill: #e8e8e8; stroke: #ccc; stroke-width: 20; }')
        lines.append('    .room-label { font-family: Arial, sans-serif; font-size: 400; '
                      'fill: #555; text-anchor: middle; dominant-baseline: central; }')
        lines.append('    .scale-line { stroke: #333; stroke-width: 20; }')
        lines.append('    .scale-text { font-family: Arial, sans-serif; font-size: 300; '
                      'fill: #333; text-anchor: middle; }')
        lines.append('    .grid-line { stroke: #eee; stroke-width: 5; }')
        lines.append('  </style>')
        lines.append('</defs>')

        # Background
        lines.append(f'<rect x="{min_x}" y="{min_y}" width="{width}" height="{height}" fill="#fff"/>')

        # Grid lines every 1m (1000mm)
        grid_step = 1000
        gx_start = int(math.ceil(min_x / grid_step)) * grid_step
        gy_start = int(math.ceil(min_y / grid_step)) * grid_step
        for gx in range(gx_start, int(max_x), grid_step):
            lines.append(f'<line x1="{gx}" y1="{min_y}" x2="{gx}" y2="{max_y}" class="grid-line"/>')
        for gy in range(gy_start, int(max_y), grid_step):
            lines.append(f'<line x1="{min_x}" y1="{gy}" x2="{max_x}" y2="{gy}" class="grid-line"/>')

        # Room fills (polygons)
        for room in self.rooms:
            if len(room.boundary) >= 3:
                pts = ' '.join(f'{x:.0f},{y:.0f}' for x, y in room.boundary)
                lines.append(f'<polygon points="{pts}" class="room-fill"/>')

        # Wall lines
        for x1, y1, x2, y2 in self.walls:
            lines.append(f'<line x1="{x1:.0f}" y1="{y1:.0f}" '
                          f'x2="{x2:.0f}" y2="{y2:.0f}" class="wall"/>')

        # Room labels
        for room in self.rooms:
            cx, cy = room.centroid
            lines.append(f'<text x="{cx:.0f}" y="{cy:.0f}" class="room-label">{room.label}</text>')
            lines.append(f'<text x="{cx:.0f}" y="{cy + 500:.0f}" class="scale-text">'
                          f'{room.area_m2:.1f} m\u00b2</text>')

        # Scale bar (bottom-right, 1 meter)
        sb_x = max_x - margin_mm - 1000
        sb_y = max_y - margin_mm / 2
        lines.append(f'<line x1="{sb_x}" y1="{sb_y}" x2="{sb_x + 1000}" y2="{sb_y}" class="scale-line"/>')
        lines.append(f'<line x1="{sb_x}" y1="{sb_y - 100}" x2="{sb_x}" y2="{sb_y + 100}" class="scale-line"/>')
        lines.append(f'<line x1="{sb_x + 1000}" y1="{sb_y - 100}" x2="{sb_x + 1000}" y2="{sb_y + 100}" class="scale-line"/>')
        lines.append(f'<text x="{sb_x + 500}" y="{sb_y + 400}" class="scale-text">1 m</text>')

        lines.append('</svg>')

        with open(filename, 'w') as f:
            f.write('\n'.join(lines))

    def to_json(self) -> dict:
        """Export wall lines + rooms as JSON for the web UI.

        Returns dict with:
            walls: [[x1, y1, x2, y2], ...]  (mm coordinates)
            rooms: [{id, label, centroid: [x,y], area_m2, bbox: [c0,r0,c1,r1], boundary: [[x,y],...]}]
            grid_size: int
            cell_size_mm: float
        """
        return {
            'walls': [list(w) for w in self.walls],
            'rooms': [
                {
                    'id': r.id,
                    'label': r.label,
                    'centroid': list(r.centroid),
                    'area_m2': round(r.area_m2, 2),
                    'cells': r.cells,
                    'bbox': list(r.bbox),
                    'boundary': [list(pt) for pt in r.boundary],
                }
                for r in self.rooms
            ],
            'grid_size': self.grid_size,
            'cell_size_mm': self.cell_size_mm,
        }


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------

def _threshold_grid(grid: np.ndarray, wall_thresh: float = 0.8,
                    free_thresh: float = -0.5) -> Tuple[np.ndarray, np.ndarray]:
    """Binary threshold the log-odds grid into wall and free masks.

    Returns:
        wall_mask: bool array, True where grid > wall_thresh
        free_mask: bool array, True where grid < free_thresh
    """
    wall_mask = grid > wall_thresh
    free_mask = grid < free_thresh
    return wall_mask, free_mask


def _morphological_cleanup(wall_mask: np.ndarray, erosion_iter: int = 1,
                           dilation_iter: int = 2) -> np.ndarray:
    """Clean up wall mask: erode to remove noise, dilate to fill gaps.

    Uses a 3x3 structuring element (4-connected for erosion, 8-connected for dilation).
    """
    # Erosion with a cross kernel (removes isolated pixels / thin noise)
    struct_cross = generate_binary_structure(2, 1)  # 4-connected
    cleaned = binary_erosion(wall_mask, structure=struct_cross, iterations=erosion_iter)

    # Dilation with a full kernel (fills small gaps between wall segments)
    struct_full = generate_binary_structure(2, 2)  # 8-connected
    cleaned = binary_dilation(cleaned, structure=struct_full, iterations=dilation_iter)

    return cleaned.astype(np.uint8)


def _detect_line_segments(wall_img: np.ndarray, min_line_length: int = 5,
                          max_line_gap: int = 3,
                          scan_diagonals: bool = False) -> List[Segment]:
    """Detect line segments from a binary wall image using run-length scanning.

    Scans along rows (horizontal) and columns (vertical) to find contiguous runs
    of wall pixels, allowing small gaps (max_line_gap). Optionally scans 45/135
    degree diagonals for non-orthogonal floor plans.

    This is more robust and faster than a full Hough transform for floor plans.

    Pure numpy implementation (no OpenCV).

    Args:
        wall_img: binary uint8 image (>0 for wall, 0 for empty)
        min_line_length: minimum line segment length in cells
        max_line_gap: maximum gap allowed within a segment
        scan_diagonals: if True, also scan 45 and 135 degree diagonals

    Returns:
        List of (x1, y1, x2, y2) in grid cell coordinates.
    """
    binary = (wall_img > 0).astype(np.uint8)
    h, w = binary.shape
    segments = []

    def _extract_runs_1d(values, min_len, max_gap):
        """Extract start/end indices of runs from a 1D binary array."""
        runs = []
        in_run = False
        start = 0
        gap = 0

        for i in range(len(values)):
            if values[i]:
                if not in_run:
                    start = i
                    in_run = True
                gap = 0
            else:
                if in_run:
                    gap += 1
                    if gap > max_gap:
                        end = i - gap
                        if end - start >= min_len:
                            runs.append((start, end))
                        in_run = False
                        gap = 0

        if in_run:
            end = len(values) - 1 - gap
            if gap <= max_gap:
                end = len(values) - 1
            if end - start >= min_len:
                runs.append((start, end))

        return runs

    # 1. Horizontal lines: scan each row
    for row in range(h):
        runs = _extract_runs_1d(binary[row, :], min_line_length, max_line_gap)
        for start, end in runs:
            segments.append((float(start), float(row), float(end), float(row)))

    # 2. Vertical lines: scan each column
    for col in range(w):
        runs = _extract_runs_1d(binary[:, col], min_line_length, max_line_gap)
        for start, end in runs:
            segments.append((float(col), float(start), float(col), float(end)))

    # 3. 45-degree diagonals (top-left to bottom-right) — optional
    if scan_diagonals:
        for offset in range(-h + 1, w):
            diag = binary.diagonal(offset)
            runs = _extract_runs_1d(diag, min_line_length, max_line_gap)
            for start, end in runs:
                if offset >= 0:
                    segments.append((float(offset + start), float(start),
                                     float(offset + end), float(end)))
                else:
                    segments.append((float(start), float(-offset + start),
                                     float(end), float(-offset + end)))

        # 4. 135-degree diagonals (top-right to bottom-left)
        flipped = np.fliplr(binary)
        for offset in range(-h + 1, w):
            diag = flipped.diagonal(offset)
            runs = _extract_runs_1d(diag, min_line_length, max_line_gap)
            for start, end in runs:
                if offset >= 0:
                    fx_start = offset + start
                    fx_end = offset + end
                    segments.append((float(w - 1 - fx_start), float(start),
                                     float(w - 1 - fx_end), float(end)))
                else:
                    fx_start = start
                    fx_end = end
                    segments.append((float(w - 1 - fx_start), float(-offset + start),
                                     float(w - 1 - fx_end), float(-offset + end)))

    return segments


def _deduplicate_segments(segments: List[Segment], dist_thresh: float = 3.0
                          ) -> List[Segment]:
    """Remove near-duplicate segments, keeping the longest.

    Two segments are duplicates if they have similar angle, close midpoints,
    and overlapping extent.
    """
    if len(segments) < 2:
        return segments

    # Sort by length descending so we keep longer segments
    segs_with_len = [(math.hypot(s[2]-s[0], s[3]-s[1]), s) for s in segments]
    segs_with_len.sort(reverse=True)

    keep = []
    for length, seg in segs_with_len:
        x1, y1, x2, y2 = seg
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        angle = math.atan2(y2 - y1, x2 - x1) % math.pi

        is_dup = False
        for kx1, ky1, kx2, ky2 in keep:
            ka = math.atan2(ky2 - ky1, kx2 - kx1) % math.pi
            da = abs(angle - ka)
            if da > math.pi / 2:
                da = math.pi - da
            if da > math.radians(15):
                continue

            kmx, kmy = (kx1 + kx2) / 2, (ky1 + ky2) / 2
            mid_dist = math.hypot(mx - kmx, my - kmy)

            # Perpendicular distance from seg midpoint to kept line
            kcos = math.cos(ka)
            ksin = math.sin(ka)
            perp = abs(-ksin * (mx - kmx) + kcos * (my - kmy))

            if perp < dist_thresh and mid_dist < max(length, math.hypot(kx2-kx1, ky2-ky1)) * 0.6:
                is_dup = True
                break

        if not is_dup:
            keep.append(seg)

    return keep


def _snap_angles(segments: List[Segment], snap_angles_deg: List[float] = None,
                 snap_thresh_deg: float = 8.0, cell_size_mm: float = 50.0
                 ) -> List[Segment]:
    """Snap line segments to nearest canonical angle.

    Args:
        segments: line segments in grid cell coordinates
        snap_angles_deg: angles to snap to (default: 0, 45, 90, 135)
        snap_thresh_deg: maximum deviation to snap
        cell_size_mm: for converting to mm coordinates

    Returns:
        Segments converted to mm coordinates, with angles snapped.
    """
    if snap_angles_deg is None:
        snap_angles_deg = [0, 45, 90, 135]

    snap_angles_rad = [math.radians(a) for a in snap_angles_deg]
    snap_thresh_rad = math.radians(snap_thresh_deg)

    result = []
    for x1, y1, x2, y2 in segments:
        # Convert to mm
        x1_mm = x1 * cell_size_mm
        y1_mm = y1 * cell_size_mm
        x2_mm = x2 * cell_size_mm
        y2_mm = y2 * cell_size_mm

        angle = math.atan2(y2_mm - y1_mm, x2_mm - x1_mm)
        # Normalize to [0, pi)
        angle_norm = angle % math.pi

        # Find closest snap angle
        best_snap = None
        best_diff = snap_thresh_rad
        for sa in snap_angles_rad:
            diff = abs(angle_norm - sa)
            if diff > math.pi / 2:
                diff = math.pi - diff
            if diff < best_diff:
                best_diff = diff
                best_snap = sa

        if best_snap is not None:
            # Snap: rotate the segment to the snap angle
            mid_x = (x1_mm + x2_mm) / 2
            mid_y = (y1_mm + y2_mm) / 2
            half_len = math.hypot(x2_mm - x1_mm, y2_mm - y1_mm) / 2

            # Determine which direction along snap angle
            # Use the sign of the original projection
            cos_s = math.cos(best_snap)
            sin_s = math.sin(best_snap)

            # Special handling for orthogonal snaps
            if best_snap < math.radians(1) or abs(best_snap - math.pi) < math.radians(1):
                # Horizontal: snap y coordinates
                y1_mm = y2_mm = mid_y
                # Keep x coordinates
            elif abs(best_snap - math.pi / 2) < math.radians(1):
                # Vertical: snap x coordinates
                x1_mm = x2_mm = mid_x
                # Keep y coordinates
            else:
                # Diagonal: recompute endpoints from midpoint along snap direction
                x1_mm = mid_x - cos_s * half_len
                y1_mm = mid_y - sin_s * half_len
                x2_mm = mid_x + cos_s * half_len
                y2_mm = mid_y + sin_s * half_len

        result.append((round(x1_mm), round(y1_mm), round(x2_mm), round(y2_mm)))

    return result


def _collapse_parallel_lines(lines: List[Segment], angle_thresh_deg: float = 10,
                             perp_dist_thresh: float = 150) -> List[Segment]:
    """Merge parallel lines within perp_dist_thresh into single walls.

    Groups by angle, then clusters by perpendicular distance. Each cluster
    becomes one line at the average perpendicular position spanning the full extent.

    Args:
        lines: wall segments in mm coordinates
        angle_thresh_deg: max angle difference to consider parallel
        perp_dist_thresh: max perpendicular distance in mm (200mm = 4 cells at 50mm/cell)

    Returns:
        Collapsed line segments.
    """
    if len(lines) < 2:
        return lines

    angle_thresh_rad = math.radians(angle_thresh_deg)

    # Compute line info: angle, perpendicular distance, length
    line_info = []
    for x1, y1, x2, y2 in lines:
        angle = math.atan2(y2 - y1, x2 - x1) % math.pi
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        nx, ny = -math.sin(angle), math.cos(angle)
        perp = mx * nx + my * ny
        length = math.hypot(x2 - x1, y2 - y1)
        line_info.append((angle, perp, x1, y1, x2, y2, length))

    used = [False] * len(line_info)
    result = []

    for i in range(len(line_info)):
        if used[i]:
            continue

        # Find all lines with similar angle
        group = [i]
        used[i] = True
        ai = line_info[i][0]

        for j in range(i + 1, len(line_info)):
            if used[j]:
                continue
            aj = line_info[j][0]
            da = abs(ai - aj)
            if da > math.pi / 2:
                da = math.pi - da
            if da < angle_thresh_rad:
                group.append(j)
                used[j] = True

        if len(group) == 1:
            idx = group[0]
            result.append((line_info[idx][2], line_info[idx][3],
                           line_info[idx][4], line_info[idx][5]))
            continue

        # Cluster by perpendicular distance within angle group
        group.sort(key=lambda idx: line_info[idx][1])
        clusters = []
        current_cluster = [group[0]]

        for k in range(1, len(group)):
            if abs(line_info[group[k]][1] - line_info[current_cluster[-1]][1]) < perp_dist_thresh:
                current_cluster.append(group[k])
            else:
                clusters.append(current_cluster)
                current_cluster = [group[k]]
        clusters.append(current_cluster)

        # Merge each cluster: collapse parallel lines while preserving gaps
        for cluster in clusters:
            if len(cluster) == 1:
                idx = cluster[0]
                result.append((line_info[idx][2], line_info[idx][3],
                               line_info[idx][4], line_info[idx][5]))
                continue

            # Weighted average angle and perpendicular distance
            total_len = sum(line_info[idx][6] for idx in cluster)
            if total_len < 1:
                continue
            avg_angle = sum(line_info[idx][0] * line_info[idx][6] for idx in cluster) / total_len
            avg_perp = sum(line_info[idx][1] * line_info[idx][6] for idx in cluster) / total_len

            cos_a = math.cos(avg_angle)
            sin_a = math.sin(avg_angle)

            # Project each segment as (proj_min, proj_max) interval
            intervals = []
            for idx in cluster:
                p1 = line_info[idx][2] * cos_a + line_info[idx][3] * sin_a
                p2 = line_info[idx][4] * cos_a + line_info[idx][5] * sin_a
                intervals.append((min(p1, p2), max(p1, p2)))

            # Merge overlapping intervals (preserves gaps like doorways)
            intervals.sort()
            merged_intervals = [intervals[0]]
            gap_thresh = perp_dist_thresh * 2  # gap threshold along line direction
            for lo, hi in intervals[1:]:
                prev_lo, prev_hi = merged_intervals[-1]
                if lo <= prev_hi + gap_thresh:
                    # Overlapping or very close — extend
                    merged_intervals[-1] = (prev_lo, max(prev_hi, hi))
                else:
                    # Gap — keep separate
                    merged_intervals.append((lo, hi))

            # Reconstruct lines from merged intervals
            nx, ny = -sin_a, cos_a
            base_x = avg_perp * nx
            base_y = avg_perp * ny
            for proj_min, proj_max in merged_intervals:
                x1 = round(base_x + proj_min * cos_a)
                y1 = round(base_y + proj_min * sin_a)
                x2 = round(base_x + proj_max * cos_a)
                y2 = round(base_y + proj_max * sin_a)
                result.append((x1, y1, x2, y2))

    return result


def _merge_collinear_lines(lines: List[Segment], dist_thresh: float = 200,
                           angle_thresh_deg: float = 10, perp_thresh: float = 100
                           ) -> List[Segment]:
    """Join line segments on the same line that are close together.

    Iteratively merges collinear segments that are within dist_thresh of each other
    (endpoint distance along the line direction).
    """
    if len(lines) < 2:
        return lines

    merged = list(lines)
    angle_thresh_rad = math.radians(angle_thresh_deg)
    changed = True

    while changed:
        changed = False
        new_merged = []
        used = set()

        for i in range(len(merged)):
            if i in used:
                continue
            x1, y1, x2, y2 = merged[i]
            a1 = math.atan2(y2 - y1, x2 - x1)
            cos_a = math.cos(a1)
            sin_a = math.sin(a1)

            for j in range(i + 1, len(merged)):
                if j in used:
                    continue
                x3, y3, x4, y4 = merged[j]
                a2 = math.atan2(y4 - y3, x4 - x3)
                da = abs(a1 - a2)
                if da > math.pi:
                    da = 2 * math.pi - da
                if da > angle_thresh_rad and abs(da - math.pi) > angle_thresh_rad:
                    continue

                # Perpendicular distance between midpoints
                mx = (x3 + x4) / 2 - (x1 + x2) / 2
                my = (y3 + y4) / 2 - (y1 + y2) / 2
                perp = abs(-sin_a * mx + cos_a * my)
                if perp > perp_thresh:
                    continue

                # Minimum endpoint distance
                d_min = min(
                    math.hypot(x1 - x3, y1 - y3), math.hypot(x1 - x4, y1 - y4),
                    math.hypot(x2 - x3, y2 - y3), math.hypot(x2 - x4, y2 - y4),
                )
                if d_min < dist_thresh:
                    # Merge: project all 4 points, take extremes
                    pts = [(x1, y1), (x2, y2), (x3, y3), (x4, y4)]
                    projs = [(p[0] * cos_a + p[1] * sin_a, p) for p in pts]
                    projs.sort()
                    x1, y1 = projs[0][1]
                    x2, y2 = projs[-1][1]
                    merged[i] = (round(x1), round(y1), round(x2), round(y2))
                    used.add(j)
                    changed = True
                    break

            new_merged.append(merged[i])

        # Add remaining unused segments
        for i in range(len(merged)):
            if i not in used and merged[i] not in new_merged:
                new_merged.append(merged[i])

        merged = new_merged

    return merged


def _filter_short_segments(segments: List[Segment], min_length_mm: float = 200
                           ) -> List[Segment]:
    """Remove segments shorter than min_length_mm."""
    return [
        seg for seg in segments
        if math.hypot(seg[2] - seg[0], seg[3] - seg[1]) >= min_length_mm
    ]


def _segment_rooms(free_mask: np.ndarray, cell_size_mm: float,
                   min_room_cells: int = 50) -> List[Room]:
    """Identify rooms using connected components on free space.

    Args:
        free_mask: boolean array, True for free cells
        cell_size_mm: size of each cell in mm
        min_room_cells: minimum cells to be considered a room

    Returns:
        List of Room objects, sorted by area (largest first).
    """
    # Label connected components (8-connected)
    struct = generate_binary_structure(2, 2)
    labeled, n_features = ndlabel(free_mask, structure=struct)

    cell_area_m2 = (cell_size_mm / 1000) ** 2  # area of one cell in m^2
    rooms = []

    for region_id in range(1, n_features + 1):
        region_mask = labeled == region_id
        n_cells = int(region_mask.sum())

        if n_cells < min_room_cells:
            continue

        area_m2 = n_cells * cell_area_m2

        # Centroid in mm
        ys, xs = np.where(region_mask)
        cx_mm = float(xs.mean()) * cell_size_mm
        cy_mm = float(ys.mean()) * cell_size_mm

        # Bounding box (grid coords)
        min_col, max_col = int(xs.min()), int(xs.max())
        min_row, max_row = int(ys.min()), int(ys.max())

        # Simplified boundary: convex hull of the region using a simple approach
        boundary = _extract_boundary(region_mask, cell_size_mm)

        rooms.append(Room(
            id=len(rooms) + 1,
            label=f'Room {len(rooms) + 1}',
            cells=n_cells,
            area_m2=area_m2,
            centroid=(cx_mm, cy_mm),
            bbox=(min_col, min_row, max_col, max_row),
            boundary=boundary,
        ))

    # Sort by area descending
    rooms.sort(key=lambda r: r.area_m2, reverse=True)

    # Re-label after sorting
    for i, room in enumerate(rooms):
        room.id = i + 1
        room.label = f'Room {i + 1}'

    return rooms


def _extract_boundary(region_mask: np.ndarray, cell_size_mm: float
                      ) -> List[Tuple[float, float]]:
    """Extract a simplified boundary polygon for a region.

    Uses convex hull approximation with scipy-compatible numpy operations.
    Falls back to bounding box corners if hull computation is not feasible.
    """
    ys, xs = np.where(region_mask)
    if len(xs) < 3:
        return []

    # Use a simple convex hull via gift-wrapping (Jarvis march)
    # Work in grid coordinates, convert to mm at the end
    points = np.column_stack((xs, ys))

    # Subsample for performance if too many points
    if len(points) > 2000:
        indices = np.random.choice(len(points), 2000, replace=False)
        points = points[indices]

    hull_indices = _convex_hull(points)
    if not hull_indices:
        # Fallback: bounding box
        min_x, max_x = xs.min(), xs.max()
        min_y, max_y = ys.min(), ys.max()
        return [
            (float(min_x) * cell_size_mm, float(min_y) * cell_size_mm),
            (float(max_x) * cell_size_mm, float(min_y) * cell_size_mm),
            (float(max_x) * cell_size_mm, float(max_y) * cell_size_mm),
            (float(min_x) * cell_size_mm, float(max_y) * cell_size_mm),
        ]

    # Simplify hull: remove points that are too close together
    hull_pts = points[hull_indices]
    simplified = [hull_pts[0]]
    min_dist = 2.0  # cells
    for pt in hull_pts[1:]:
        if math.hypot(pt[0] - simplified[-1][0], pt[1] - simplified[-1][1]) >= min_dist:
            simplified.append(pt)

    # Convert to mm
    return [(float(p[0]) * cell_size_mm, float(p[1]) * cell_size_mm) for p in simplified]


def _convex_hull(points: np.ndarray) -> List[int]:
    """Compute convex hull using Graham scan. Returns indices into points array."""
    n = len(points)
    if n < 3:
        return list(range(n))

    # Find lowest y, then leftmost x
    start = 0
    for i in range(1, n):
        if points[i, 1] < points[start, 1] or \
           (points[i, 1] == points[start, 1] and points[i, 0] < points[start, 0]):
            start = i

    # Sort by polar angle from start
    sx, sy = points[start]

    def polar_key(idx):
        dx = points[idx, 0] - sx
        dy = points[idx, 1] - sy
        return (math.atan2(dy, dx), dx * dx + dy * dy)

    indices = list(range(n))
    indices.remove(start)
    indices.sort(key=polar_key)
    indices = [start] + indices

    # Graham scan
    stack = []
    for idx in indices:
        while len(stack) > 1:
            # Cross product to check turn direction
            o = stack[-2]
            a = stack[-1]
            ox, oy = points[o]
            ax, ay = points[a]
            bx, by = points[idx]
            cross = (ax - ox) * (by - oy) - (ay - oy) * (bx - ox)
            if cross <= 0:
                stack.pop()
            else:
                break
        stack.append(idx)

    return stack


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def process_grid(grid: np.ndarray, cell_size_mm: float = 50,
                 wall_thresh: float = 0.8, free_thresh: float = -0.5,
                 min_line_length_cells: int = 5, min_wall_length_mm: float = 200,
                 snap_angles: List[float] = None, snap_thresh_deg: float = 8.0,
                 parallel_dist_mm: float = 200, collinear_dist_mm: float = 250,
                 min_room_cells: int = 50) -> FloorPlan:
    """Main floor plan processing pipeline.

    Takes a raw occupancy grid (log-odds values) and produces a clean floor plan
    with straight wall lines and identified rooms.

    Args:
        grid: numpy array (NxN), log-odds values. Walls > wall_thresh, free < free_thresh.
        cell_size_mm: size of each grid cell in mm (default 50).
        wall_thresh: log-odds threshold for wall cells.
        free_thresh: log-odds threshold for free cells.
        min_line_length_cells: minimum line segment length for Hough detection (in cells).
        min_wall_length_mm: minimum wall segment length in final output (mm).
        snap_angles: angles to snap to (degrees), default [0, 45, 90, 135].
        snap_thresh_deg: max angle deviation for snapping.
        parallel_dist_mm: max perpendicular distance for collapsing parallel lines.
        collinear_dist_mm: max endpoint gap for merging collinear segments.
        min_room_cells: minimum free cells for a region to be a room.

    Returns:
        FloorPlan object with walls, rooms, and export methods.
    """
    grid_size = grid.shape[0]

    # Step 1: Threshold
    wall_mask, free_mask = _threshold_grid(grid, wall_thresh, free_thresh)

    # Step 2: Morphological cleanup
    wall_img = _morphological_cleanup(wall_mask)
    wall_img_255 = wall_img * 255

    # Step 3: Line segment detection via run-length scanning (in grid cell coordinates)
    raw_segments = _detect_line_segments(
        wall_img_255,
        min_line_length=min_line_length_cells,
        max_line_gap=3,
    )

    # Deduplicate raw segments (many overlapping runs from thick walls)
    raw_segments = _deduplicate_segments(raw_segments, dist_thresh=3.0)

    # Step 4: Angle snapping + convert to mm coordinates
    snapped = _snap_angles(raw_segments, snap_angles, snap_thresh_deg, cell_size_mm)

    # Step 5: Collapse parallel lines
    collapsed = _collapse_parallel_lines(snapped, perp_dist_thresh=parallel_dist_mm)

    # Step 6: Merge collinear segments
    merged = _merge_collinear_lines(collapsed, dist_thresh=collinear_dist_mm)

    # Step 7: Filter short segments
    walls = _filter_short_segments(merged, min_length_mm=min_wall_length_mm)

    # Step 8: Room segmentation
    rooms = _segment_rooms(free_mask, cell_size_mm, min_room_cells)

    return FloorPlan(
        walls=walls,
        rooms=rooms,
        grid_size=grid_size,
        cell_size_mm=cell_size_mm,
        wall_image=wall_img,
    )



# ---------------------------------------------------------------------------
# CLI entry point for testing
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import sys
    import os

    if len(sys.argv) < 2:
        print("Usage: python floor_plan_processor.py <grid.npz> [output.svg]")
        print("  grid.npz must contain a 'grid' key with 600x600 log-odds array")
        sys.exit(1)

    grid_path = sys.argv[1]
    svg_path = sys.argv[2] if len(sys.argv) > 2 else grid_path.replace('.npz', '_floor_plan.svg')

    data = np.load(grid_path)
    grid = data['grid'] if 'grid' in data else data[data.files[0]]

    print(f"Grid shape: {grid.shape}")
    print(f"Value range: [{grid.min():.2f}, {grid.max():.2f}]")
    print(f"Wall cells (>0.8): {(grid > 0.8).sum()}")
    print(f"Free cells (<-0.5): {(grid < -0.5).sum()}")

    fp = process_grid(grid)

    print(f"\nResults:")
    print(f"  Wall segments: {len(fp.walls)}")
    print(f"  Rooms: {len(fp.rooms)}")
    for room in fp.rooms:
        print(f"    {room.label}: {room.area_m2:.1f} m^2, {room.cells} cells")

    fp.to_svg(svg_path)
    print(f"\nSVG saved to: {svg_path}")

    json_path = svg_path.replace('.svg', '.json')
    with open(json_path, 'w') as f:
        json.dump(fp.to_json(), f, indent=2)
    print(f"JSON saved to: {json_path}")
