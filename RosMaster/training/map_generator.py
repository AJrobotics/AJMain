"""
Procedural map generator for RL training.

Generates random room layouts for domain randomization:
- Simple rectangles
- L-shaped rooms
- Rooms with hallways
- Multi-room layouts
- Random obstacles

All maps use the same 600x600 grid at 50mm/cell as the SLAM engine.
"""

import numpy as np
import math
import os

GRID_SIZE = 600
CELL_SIZE_MM = 50
WALL_VAL = 3.0       # log-odds for confirmed wall
FREE_VAL = -2.0      # log-odds for free space
WALL_THICKNESS = 2   # cells


def _draw_rect_walls(grid, x1, y1, x2, y2, val=WALL_VAL, thickness=WALL_THICKNESS):
    """Draw rectangular walls on grid."""
    x1 = max(0, min(x1, GRID_SIZE - 1))
    x2 = max(0, min(x2, GRID_SIZE - 1))
    y1 = max(0, min(y1, GRID_SIZE - 1))
    y2 = max(0, min(y2, GRID_SIZE - 1))

    for t in range(thickness):
        grid[y1 + t, x1:x2 + 1] = val   # top
        grid[y2 - t, x1:x2 + 1] = val   # bottom
        grid[y1:y2 + 1, x1 + t] = val   # left
        grid[y1:y2 + 1, x2 - t] = val   # right


def _fill_free(grid, x1, y1, x2, y2, val=FREE_VAL):
    """Fill interior of rectangle as free space."""
    t = WALL_THICKNESS
    grid[y1 + t:y2 - t + 1, x1 + t:x2 - t + 1] = val


def _add_doorway(grid, x1, y1, x2, y2, wall, position, width=20):
    """Cut a doorway in a wall. wall: 'top', 'bottom', 'left', 'right'."""
    half_w = width // 2

    if wall == 'top':
        cx = x1 + int((x2 - x1) * position)
        grid[y1:y1 + WALL_THICKNESS, max(x1, cx - half_w):min(x2, cx + half_w)] = FREE_VAL
    elif wall == 'bottom':
        cx = x1 + int((x2 - x1) * position)
        grid[y2 - WALL_THICKNESS + 1:y2 + 1, max(x1, cx - half_w):min(x2, cx + half_w)] = FREE_VAL
    elif wall == 'left':
        cy = y1 + int((y2 - y1) * position)
        grid[max(y1, cy - half_w):min(y2, cy + half_w), x1:x1 + WALL_THICKNESS] = FREE_VAL
    elif wall == 'right':
        cy = y1 + int((y2 - y1) * position)
        grid[max(y1, cy - half_w):min(y2, cy + half_w), x2 - WALL_THICKNESS + 1:x2 + 1] = FREE_VAL


def generate_simple_room(rng=None):
    """Generate a single rectangular room with random size."""
    if rng is None:
        rng = np.random.default_rng()

    grid = np.zeros((GRID_SIZE, GRID_SIZE), dtype=np.float32)

    # Random room size: 3m-10m per side
    w = rng.integers(60, 200)   # cells
    h = rng.integers(60, 200)

    # Center on grid
    cx, cy = GRID_SIZE // 2, GRID_SIZE // 2
    x1 = cx - w // 2
    x2 = cx + w // 2
    y1 = cy - h // 2
    y2 = cy + h // 2

    _draw_rect_walls(grid, x1, y1, x2, y2)
    _fill_free(grid, x1, y1, x2, y2)

    # Random obstacles (small boxes inside)
    n_obstacles = rng.integers(0, 5)
    for _ in range(n_obstacles):
        ow = rng.integers(4, 15)
        oh = rng.integers(4, 15)
        ox = rng.integers(x1 + 10, x2 - 10 - ow)
        oy = rng.integers(y1 + 10, y2 - 10 - oh)
        grid[oy:oy + oh, ox:ox + ow] = WALL_VAL

    return grid


def generate_l_shaped_room(rng=None):
    """Generate an L-shaped room (two rectangles joined)."""
    if rng is None:
        rng = np.random.default_rng()

    grid = np.zeros((GRID_SIZE, GRID_SIZE), dtype=np.float32)

    cx, cy = GRID_SIZE // 2, GRID_SIZE // 2

    # Main room
    w1 = rng.integers(80, 160)
    h1 = rng.integers(80, 160)
    x1 = cx - w1 // 2
    y1 = cy - h1 // 2

    _draw_rect_walls(grid, x1, y1, x1 + w1, y1 + h1)
    _fill_free(grid, x1, y1, x1 + w1, y1 + h1)

    # Extension (attached to one side)
    side = rng.choice(['right', 'bottom'])
    w2 = rng.integers(40, 100)
    h2 = rng.integers(40, 100)

    if side == 'right':
        x2 = x1 + w1
        y2 = y1 + rng.integers(0, max(1, h1 - h2))
        _draw_rect_walls(grid, x2, y2, x2 + w2, y2 + h2)
        _fill_free(grid, x2, y2, x2 + w2, y2 + h2)
        # Open connecting wall
        oy = max(y1, y2) + WALL_THICKNESS
        oh = min(y1 + h1, y2 + h2) - WALL_THICKNESS - oy
        if oh > 4:
            grid[oy:oy + oh, x2:x2 + WALL_THICKNESS] = FREE_VAL
    else:
        x2 = x1 + rng.integers(0, max(1, w1 - w2))
        y2 = y1 + h1
        _draw_rect_walls(grid, x2, y2, x2 + w2, y2 + h2)
        _fill_free(grid, x2, y2, x2 + w2, y2 + h2)
        # Open connecting wall
        ox = max(x1, x2) + WALL_THICKNESS
        ow = min(x1 + w1, x2 + w2) - WALL_THICKNESS - ox
        if ow > 4:
            grid[y2:y2 + WALL_THICKNESS, ox:ox + ow] = FREE_VAL

    return grid


def generate_room_with_hallway(rng=None):
    """Generate a room with a hallway extending from one wall."""
    if rng is None:
        rng = np.random.default_rng()

    grid = np.zeros((GRID_SIZE, GRID_SIZE), dtype=np.float32)

    cx, cy = GRID_SIZE // 2, GRID_SIZE // 2

    # Main room
    rw = rng.integers(80, 140)
    rh = rng.integers(80, 140)
    rx1 = cx - rw // 2
    ry1 = cy - rh // 2 - 30  # shift up to make room for hallway

    _draw_rect_walls(grid, rx1, ry1, rx1 + rw, ry1 + rh)
    _fill_free(grid, rx1, ry1, rx1 + rw, ry1 + rh)

    # Hallway from bottom wall
    hw = rng.integers(20, 40)     # hallway width (1-2m)
    hl = rng.integers(60, 120)    # hallway length (3-6m)
    hx = cx - hw // 2
    hy = ry1 + rh

    # Draw hallway walls
    for t in range(WALL_THICKNESS):
        grid[hy:hy + hl, hx + t] = WALL_VAL              # left wall
        grid[hy:hy + hl, hx + hw - 1 - t] = WALL_VAL     # right wall
        grid[hy + hl - 1 - t, hx:hx + hw] = WALL_VAL     # end wall

    # Fill hallway interior
    grid[hy:hy + hl - WALL_THICKNESS, hx + WALL_THICKNESS:hx + hw - WALL_THICKNESS] = FREE_VAL

    # Open doorway between room and hallway
    grid[hy - WALL_THICKNESS:hy + WALL_THICKNESS, hx + WALL_THICKNESS:hx + hw - WALL_THICKNESS] = FREE_VAL

    return grid


def generate_multi_room(rng=None):
    """Generate 2-4 rooms connected by doorways."""
    if rng is None:
        rng = np.random.default_rng()

    grid = np.zeros((GRID_SIZE, GRID_SIZE), dtype=np.float32)

    n_rooms = rng.integers(2, 5)
    rooms = []

    # Generate non-overlapping rooms
    for _ in range(n_rooms):
        for attempt in range(50):  # max attempts to place
            w = rng.integers(60, 130)
            h = rng.integers(60, 130)
            x1 = rng.integers(10, GRID_SIZE - w - 10)
            y1 = rng.integers(10, GRID_SIZE - h - 10)
            x2 = x1 + w
            y2 = y1 + h

            # Check overlap with existing rooms (with gap)
            gap = 5
            overlap = False
            for (rx1, ry1, rx2, ry2) in rooms:
                if not (x2 + gap < rx1 or x1 - gap > rx2 or
                        y2 + gap < ry1 or y1 - gap > ry2):
                    overlap = True
                    break

            if not overlap:
                rooms.append((x1, y1, x2, y2))
                _draw_rect_walls(grid, x1, y1, x2, y2)
                _fill_free(grid, x1, y1, x2, y2)
                break

    # Connect adjacent rooms with doorways
    for i in range(len(rooms)):
        for j in range(i + 1, len(rooms)):
            r1 = rooms[i]
            r2 = rooms[j]

            # Check if rooms are adjacent (sharing a wall segment)
            # Right wall of r1 touching left wall of r2
            if abs(r1[2] - r2[0]) < 8:
                overlap_y1 = max(r1[1], r2[1]) + 5
                overlap_y2 = min(r1[3], r2[3]) - 5
                if overlap_y2 - overlap_y1 > 10:
                    mid_y = (overlap_y1 + overlap_y2) // 2
                    door_w = min(15, (overlap_y2 - overlap_y1) // 2)
                    # Cut through both walls
                    x_wall = min(r1[2], r2[0])
                    grid[mid_y - door_w:mid_y + door_w,
                         x_wall - WALL_THICKNESS:x_wall + WALL_THICKNESS + 3] = FREE_VAL

            # Bottom wall of r1 touching top wall of r2
            if abs(r1[3] - r2[1]) < 8:
                overlap_x1 = max(r1[0], r2[0]) + 5
                overlap_x2 = min(r1[2], r2[2]) - 5
                if overlap_x2 - overlap_x1 > 10:
                    mid_x = (overlap_x1 + overlap_x2) // 2
                    door_w = min(15, (overlap_x2 - overlap_x1) // 2)
                    y_wall = min(r1[3], r2[1])
                    grid[y_wall - WALL_THICKNESS:y_wall + WALL_THICKNESS + 3,
                         mid_x - door_w:mid_x + door_w] = FREE_VAL

    return grid


def generate_random_map(seed=None):
    """Generate a random map using one of the layout types."""
    rng = np.random.default_rng(seed)

    generators = [
        generate_simple_room,
        generate_l_shaped_room,
        generate_room_with_hallway,
        generate_multi_room,
    ]

    gen = rng.choice(generators)
    return gen(rng)


def save_map(grid, path):
    """Save map as .npz file (compatible with SLAM engine)."""
    np.savez_compressed(path, grid=grid)


def generate_training_set(output_dir, n_maps=50, seed=42):
    """Generate a set of maps for training."""
    os.makedirs(output_dir, exist_ok=True)
    rng = np.random.default_rng(seed)

    for i in range(n_maps):
        grid = generate_random_map(seed=rng.integers(0, 100000))
        save_map(grid, os.path.join(output_dir, f"map_{i:03d}.npz"))
        print(f"Generated map_{i:03d}.npz")

    print(f"\nGenerated {n_maps} maps in {output_dir}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate training maps")
    parser.add_argument("--output", default="training/maps", help="Output directory")
    parser.add_argument("--count", type=int, default=50, help="Number of maps")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--visualize", action="store_true", help="Show sample maps")
    args = parser.parse_args()

    if args.visualize:
        import matplotlib.pyplot as plt

        fig, axes = plt.subplots(2, 4, figsize=(16, 8))
        rng = np.random.default_rng(args.seed)

        for i, ax in enumerate(axes.flat):
            grid = generate_random_map(seed=rng.integers(0, 100000))

            img = np.zeros((GRID_SIZE, GRID_SIZE, 3), dtype=np.uint8)
            img[grid > 2.0] = [255, 40, 40]       # wall
            img[grid > 0.8] = [255, 140, 40]       # hint
            img[grid < -1.5] = [40, 180, 40]       # free
            img[(grid >= -1.5) & (grid <= 0.8)] = [60, 60, 70]  # unknown

            ax.imshow(img)
            ax.set_title(f"Map {i}")
            ax.axis('off')

        plt.tight_layout()
        plt.savefig("sample_maps.png", dpi=100)
        plt.show()
        print("Saved sample_maps.png")
    else:
        generate_training_set(args.output, args.count, args.seed)
