"""
Gymnasium environment for RosMaster X3 robot navigation.

Loads a saved occupancy grid (.npz from SLAM engine) and simulates robot
navigation using ray-cast LiDAR. Designed for training RL navigation policies.

Supports multiple task types:
  - explore:    General exploration (36 LiDAR bins)
  - floor_plan: Complete map coverage (36 LiDAR + frontier angle/dist + coverage)
  - wall_follow: Maintain wall distance (36 LiDAR + side distance + target dist)

Action: 3 continuous floats in [-1, 1] -> mapped to (vx, vy, vz)
"""

import math
import numpy as np
import gymnasium as gym
from gymnasium import spaces

# Grid parameters (must match slam_engine.py)
GRID_SIZE = 600
CELL_SIZE_MM = 50
MAP_SIZE_MM = GRID_SIZE * CELL_SIZE_MM  # 30000mm = 30m

# LiDAR simulation
NUM_RAYS = 360          # 1° resolution
MAX_RANGE_MM = 6000     # max LiDAR range
NUM_BINS = 36           # downsample 360 rays → 36 bins (10° each)

# Collision thresholds (match collision_avoidance.py)
STOP_DIST = 100         # mm
SLOW_DIST = 200
CAUTION_DIST = 300

# Sector definitions (match collision_avoidance.py)
NUM_SECTORS = 8
SECTOR_SIZE = 45.0      # degrees

# Action mapping: [-1, 1] → real velocities (match real robot limits)
VX_MIN, VX_MAX = -0.05, 0.15   # m/s forward/backward
VY_MIN, VY_MAX = -0.12, 0.12   # m/s lateral strafe (mecanum wheels)
VZ_MIN, VZ_MAX = -1.0, 1.0     # rad/s rotation

# Simulation timing
DT = 0.2  # seconds per step (5 Hz, matches explorer)

# Task types and their observation sizes
TASK_TYPES = {
    "explore":    {"obs_size": NUM_BINS,     "desc": "General exploration"},
    "floor_plan": {"obs_size": NUM_BINS + 3, "desc": "Complete map coverage"},  # +frontier_angle, frontier_dist, coverage
    "wall_follow":{"obs_size": NUM_BINS + 3, "desc": "Wall following"},         # +side_dist, target_dist, forward_dist
}

# Episode limits
MAX_STEPS = 1000
COVERAGE_DONE = 0.95        # episode ends at 95% coverage

# Rear ignore zone (matches LiDAR reader)
IGNORE_ANGLE = 140  # degrees behind robot to ignore

# Realism parameters (sim-to-real transfer)
ROBOT_RADIUS_MM = 150       # physical robot radius for wall clearance
LIDAR_NOISE_MM = 30         # ±30mm Gaussian noise on LiDAR readings
LIDAR_DROPOUT = 0.02        # 2% of rays randomly drop out (return max range)
VELOCITY_NOISE = 0.10       # ±10% random error on velocity execution
HEADING_NOISE_RAD = 0.005   # ±0.3° random heading drift per step (IMU noise)


class RobotNavEnv(gym.Env):
    """Gymnasium environment simulating robot navigation on an occupancy grid.

    The robot navigates using simulated LiDAR ray-casting through the grid.
    Collision avoidance applies the same rules as the real robot.
    """

    metadata = {"render_modes": ["human", "rgb_array"], "render_fps": 5}

    def __init__(self, map_path=None, render_mode=None, task="explore",
                 wall_follow_side="left", wall_follow_dist=150):
        super().__init__()

        self.render_mode = render_mode
        self.task = task
        self.wall_follow_side = wall_follow_side  # "left" or "right"
        self.wall_follow_target = wall_follow_dist  # mm

        if task not in TASK_TYPES:
            raise ValueError(f"Unknown task: {task}. Options: {list(TASK_TYPES.keys())}")

        obs_size = TASK_TYPES[task]["obs_size"]

        # Observation space depends on task
        self.observation_space = spaces.Box(
            low=0.0, high=1.0, shape=(obs_size,), dtype=np.float32
        )

        # Action: 3 continuous values in [-1, 1]
        self.action_space = spaces.Box(
            low=-1.0, high=1.0, shape=(3,), dtype=np.float32
        )

        # Load map
        self.map_path = map_path
        self.grid = np.zeros((GRID_SIZE, GRID_SIZE), dtype=np.float32)
        self._load_map(map_path)

        # Determine explorable cells (non-wall cells that have been observed)
        self._compute_explorable_cells()

        # Robot state
        self.pose = np.array([MAP_SIZE_MM / 2, MAP_SIZE_MM / 2, 0.0])
        self.step_count = 0
        self.explored_cells = set()
        self.total_reward = 0.0

        # Pre-compute wall mask for fast ray-casting
        self.wall_mask = self.grid > 0.8  # log-odds > 0.8 = wall (hint or confirmed)

    def _load_map(self, map_path):
        """Load occupancy grid from .npz file (saved by SLAM engine)."""
        if map_path is None:
            # Generate a simple default room
            self._generate_default_room()
            return

        try:
            data = np.load(map_path)
            if 'grid' in data:
                self.grid = data['grid'].astype(np.float32)
            elif 'arr_0' in data:
                self.grid = data['arr_0'].astype(np.float32)
            else:
                keys = list(data.keys())
                if keys:
                    self.grid = data[keys[0]].astype(np.float32)

            # Ensure correct shape
            if self.grid.shape != (GRID_SIZE, GRID_SIZE):
                print(f"Warning: grid shape {self.grid.shape} != expected ({GRID_SIZE},{GRID_SIZE})")
                self.grid = np.zeros((GRID_SIZE, GRID_SIZE), dtype=np.float32)
                self._generate_default_room()
        except Exception as e:
            print(f"Error loading map: {e}")
            self._generate_default_room()

    def _generate_default_room(self):
        """Generate a simple rectangular room for testing."""
        self.grid = np.zeros((GRID_SIZE, GRID_SIZE), dtype=np.float32)

        # Fill everything as unknown (0)
        # Draw walls as high log-odds
        wall_val = 3.0

        # Outer walls of a 6m x 8m room centered on grid
        cx, cy = GRID_SIZE // 2, GRID_SIZE // 2
        room_w = 120  # cells = 6m
        room_h = 160  # cells = 8m

        x1 = cx - room_w // 2
        x2 = cx + room_w // 2
        y1 = cy - room_h // 2
        y2 = cy + room_h // 2

        # Draw walls (thick = 2 cells)
        for t in range(2):
            self.grid[y1 + t, x1:x2] = wall_val   # top wall
            self.grid[y2 - t, x1:x2] = wall_val   # bottom wall
            self.grid[y1:y2, x1 + t] = wall_val   # left wall
            self.grid[y1:y2, x2 - t] = wall_val   # right wall

        # Mark interior as free space
        self.grid[y1 + 2:y2 - 1, x1 + 2:x2 - 1] = -2.0  # free

        # Add a hallway extending from bottom wall
        hw = 30   # hallway width in cells = 1.5m
        hl = 80   # hallway length in cells = 4m
        hx1 = cx - hw // 2
        hx2 = cx + hw // 2

        # Clear the opening in bottom wall
        self.grid[y2 - 1:y2 + 1, hx1:hx2] = -2.0

        # Draw hallway walls
        for t in range(2):
            self.grid[y2:y2 + hl, hx1 + t] = wall_val       # left hallway wall
            self.grid[y2:y2 + hl, hx2 - 1 - t] = wall_val   # right hallway wall
            self.grid[y2 + hl - t, hx1:hx2] = wall_val       # end wall

        # Mark hallway interior as free
        self.grid[y2:y2 + hl - 1, hx1 + 2:hx2 - 1] = -2.0

    def _compute_explorable_cells(self):
        """Count cells that are free or have been observed (not walls)."""
        # Explorable = cells with log-odds < 0.8 (free or unknown but not wall)
        # For reward, we track cells that the robot marks as "seen"
        self.free_cells = set()
        for y in range(GRID_SIZE):
            for x in range(GRID_SIZE):
                if self.grid[y, x] < -0.5:  # free space
                    self.free_cells.add((x, y))
        self.total_free = len(self.free_cells) if self.free_cells else 1

    def _find_spawn_point(self):
        """Find a valid spawn point inside free space."""
        # Try center first
        cx = int(self.pose[0] / CELL_SIZE_MM)
        cy = int(self.pose[1] / CELL_SIZE_MM)
        if 0 <= cx < GRID_SIZE and 0 <= cy < GRID_SIZE and self.grid[cy, cx] < -0.5:
            return self.pose[:2].copy()

        # Search for free cells near center
        center_x, center_y = GRID_SIZE // 2, GRID_SIZE // 2
        best_dist = float('inf')
        best_pos = None

        for y in range(GRID_SIZE):
            for x in range(GRID_SIZE):
                if self.grid[y, x] < -0.5:  # free space
                    d = (x - center_x) ** 2 + (y - center_y) ** 2
                    if d < best_dist:
                        best_dist = d
                        best_pos = (x * CELL_SIZE_MM, y * CELL_SIZE_MM)

        if best_pos:
            return np.array(best_pos, dtype=np.float64)

        # Fallback: center
        return np.array([MAP_SIZE_MM / 2, MAP_SIZE_MM / 2], dtype=np.float64)

    # ================================================================
    # Ray-casting
    # ================================================================

    def _raycast(self):
        """Cast 360 rays through the occupancy grid.

        Returns array of 360 distances in mm (one per degree).
        Uses DDA stepping through grid cells.
        Includes realistic LiDAR noise and dropout.
        """
        ox = self.pose[0] / CELL_SIZE_MM  # robot position in grid coords
        oy = self.pose[1] / CELL_SIZE_MM
        theta = self.pose[2]              # heading in radians

        max_cells = int(MAX_RANGE_MM / CELL_SIZE_MM)  # 120 cells max
        distances = np.full(NUM_RAYS, MAX_RANGE_MM, dtype=np.float32)

        for i in range(NUM_RAYS):
            angle = theta + math.radians(i)  # world angle for this ray
            dx = math.cos(angle)
            dy = math.sin(angle)

            # Step along ray using DDA (Digital Differential Analyzer)
            cx, cy = int(ox), int(oy)

            if dx == 0:
                step_x = 0
                t_max_x = float('inf')
                t_delta_x = float('inf')
            else:
                step_x = 1 if dx > 0 else -1
                t_max_x = ((cx + (1 if dx > 0 else 0)) - ox) / dx
                t_delta_x = abs(1.0 / dx)

            if dy == 0:
                step_y = 0
                t_max_y = float('inf')
                t_delta_y = float('inf')
            else:
                step_y = 1 if dy > 0 else -1
                t_max_y = ((cy + (1 if dy > 0 else 0)) - oy) / dy
                t_delta_y = abs(1.0 / dy)

            for _ in range(max_cells):
                # Check bounds
                if cx < 0 or cx >= GRID_SIZE or cy < 0 or cy >= GRID_SIZE:
                    break

                # Check if wall — account for robot physical radius
                if self.wall_mask[cy, cx]:
                    dist = math.sqrt((cx - ox) ** 2 + (cy - oy) ** 2) * CELL_SIZE_MM
                    # Subtract robot radius (LiDAR measures from center, walls are closer to body)
                    dist = max(dist - ROBOT_RADIUS_MM, 25)
                    distances[i] = dist
                    break

                # Step to next cell
                if t_max_x < t_max_y:
                    t_max_x += t_delta_x
                    cx += step_x
                else:
                    t_max_y += t_delta_y
                    cy += step_y

        # Apply realistic sensor noise
        if LIDAR_NOISE_MM > 0 and self.np_random is not None:
            noise = self.np_random.normal(0, LIDAR_NOISE_MM, size=NUM_RAYS).astype(np.float32)
            distances = np.clip(distances + noise, 25, MAX_RANGE_MM)

        # Apply random dropout (some rays return max range)
        if LIDAR_DROPOUT > 0 and self.np_random is not None:
            dropout_mask = self.np_random.random(NUM_RAYS) < LIDAR_DROPOUT
            distances[dropout_mask] = MAX_RANGE_MM

        return distances

    def _downsample_scan(self, distances_360):
        """Downsample 360 rays to 36 bins (min distance per 10° bin)."""
        bins = distances_360.reshape(NUM_BINS, NUM_RAYS // NUM_BINS).min(axis=1)
        # Normalize to [0, 1]
        return np.clip(bins / MAX_RANGE_MM, 0.0, 1.0).astype(np.float32)

    # ================================================================
    # Collision sectors
    # ================================================================

    def _compute_sectors(self, distances_360):
        """Compute 8 sector minimum distances from 360° scan.

        Matches collision_avoidance.py sector layout:
        Sector 0 = front (337.5° to 22.5°), clockwise.
        Rear sectors (3, 4, 5) set to 9999 (ignore zone).
        """
        sectors = np.full(NUM_SECTORS, 9999.0, dtype=np.float32)

        half_ignore = IGNORE_ANGLE / 2.0  # 70°

        for i in range(NUM_RAYS):
            angle_deg = float(i)
            dist = distances_360[i]

            # Apply rear ignore zone
            angle_from_rear = abs(((angle_deg - 180) + 180) % 360 - 180)
            if angle_from_rear < half_ignore:
                continue

            # Map to sector
            sector = int((angle_deg + SECTOR_SIZE / 2) % 360 / SECTOR_SIZE) % NUM_SECTORS

            if dist < sectors[sector]:
                sectors[sector] = dist

        return sectors

    def _collision_filter(self, vx, vy, vz, sectors):
        """Apply collision avoidance — same logic as collision_avoidance.py."""
        speed = math.sqrt(vx * vx + vy * vy)
        if speed < 0.001:
            return vx, vy, vz

        # Hard safety: any sector < STOP_DIST → block all translation
        global_min = float(np.min(sectors))
        if global_min < STOP_DIST:
            return 0.0, 0.0, vz

        # Directional check: 5-sector window
        move_angle_deg = math.degrees(math.atan2(-vy, vx)) % 360
        target_sector = int((move_angle_deg + SECTOR_SIZE / 2) % 360 / SECTOR_SIZE) % NUM_SECTORS
        neighbors = [
            (target_sector - 2) % NUM_SECTORS,
            (target_sector - 1) % NUM_SECTORS,
            target_sector,
            (target_sector + 1) % NUM_SECTORS,
            (target_sector + 2) % NUM_SECTORS,
        ]
        dir_min = min(float(sectors[s]) for s in neighbors)

        # Scale speed
        check_min = min(global_min, dir_min)
        if check_min < STOP_DIST:
            scale = 0.0
        elif check_min < SLOW_DIST:
            scale = 0.3
        elif check_min < CAUTION_DIST:
            scale = 0.7
        else:
            scale = 1.0

        return vx * scale, vy * scale, vz

    # ================================================================
    # Exploration tracking
    # ================================================================

    def _mark_explored(self):
        """Mark cells visible from current position as explored.

        Uses the ray-cast results to determine which cells the robot can see.
        """
        ox = int(self.pose[0] / CELL_SIZE_MM)
        oy = int(self.pose[1] / CELL_SIZE_MM)

        # Mark cells in a radius around robot as explored
        # (simulating what the LiDAR would reveal)
        vis_radius = int(MAX_RANGE_MM / CELL_SIZE_MM)
        newly_explored = 0

        for i in range(NUM_RAYS):
            angle = self.pose[2] + math.radians(i)
            dx = math.cos(angle)
            dy = math.sin(angle)

            # Step along ray until wall or max range
            for step in range(1, vis_radius):
                cx = ox + int(dx * step)
                cy = oy + int(dy * step)

                if cx < 0 or cx >= GRID_SIZE or cy < 0 or cy >= GRID_SIZE:
                    break
                if self.wall_mask[cy, cx]:
                    break

                cell = (cx, cy)
                if cell in self.free_cells and cell not in self.explored_cells:
                    self.explored_cells.add(cell)
                    newly_explored += 1

        return newly_explored

    # ================================================================
    # Gymnasium interface
    # ================================================================

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)

        # Reset robot to spawn point
        spawn = self._find_spawn_point()
        self.pose = np.array([spawn[0], spawn[1], 0.0])

        # Randomize heading
        if self.np_random is not None:
            self.pose[2] = self.np_random.uniform(-math.pi, math.pi)

        self.step_count = 0
        self.explored_cells = set()
        self.total_reward = 0.0
        self._collision_count = 0
        self._last_milestone = 0

        # Update wall mask
        self.wall_mask = self.grid > 0.8

        # Initial observation
        distances = self._raycast()
        self._last_distances = distances
        obs = self._build_obs(distances)

        # Mark initial visible cells
        self._mark_explored()

        info = {
            "pose": self.pose.tolist(),
            "coverage": len(self.explored_cells) / self.total_free,
            "sectors": self._compute_sectors(distances).tolist(),
        }

        return obs, info

    def step(self, action):
        action = np.clip(action, -1.0, 1.0)

        # Map action [-1, 1] → real velocities
        vx = action[0] * (VX_MAX - VX_MIN) / 2 + (VX_MAX + VX_MIN) / 2
        vy = action[1] * (VY_MAX - VY_MIN) / 2 + (VY_MAX + VY_MIN) / 2
        vz = action[2] * (VZ_MAX - VZ_MIN) / 2 + (VZ_MAX + VZ_MIN) / 2

        # Get current LiDAR and sectors for collision check
        distances = self._raycast()
        sectors = self._compute_sectors(distances)

        # Apply collision avoidance
        safe_vx, safe_vy, safe_vz = self._collision_filter(vx, vy, vz, sectors)

        # Check if collision blocked movement
        was_blocked = (abs(vx) > 0.001 or abs(vy) > 0.001) and abs(safe_vx) < 0.001 and abs(safe_vy) < 0.001

        # Move robot with realistic noise
        # Add velocity noise (mecanum wheel slip: ±10% random error)
        if VELOCITY_NOISE > 0 and self.np_random is not None:
            vx_noise = 1.0 + self.np_random.normal(0, VELOCITY_NOISE)
            vy_noise = 1.0 + self.np_random.normal(0, VELOCITY_NOISE * 1.5)  # lateral slip is worse
            vz_noise = 1.0 + self.np_random.normal(0, VELOCITY_NOISE * 0.5)  # rotation is more stable
            exec_vx = safe_vx * vx_noise
            exec_vy = safe_vy * vy_noise
            exec_vz = safe_vz * vz_noise
        else:
            exec_vx, exec_vy, exec_vz = safe_vx, safe_vy, safe_vz

        # Translation in world frame (rotate by heading)
        cos_t = math.cos(self.pose[2])
        sin_t = math.sin(self.pose[2])
        dx_world = (exec_vx * cos_t - exec_vy * sin_t) * DT * 1000  # m/s -> mm
        dy_world = (exec_vx * sin_t + exec_vy * cos_t) * DT * 1000
        dtheta = exec_vz * DT

        # Add heading drift (IMU noise)
        if HEADING_NOISE_RAD > 0 and self.np_random is not None:
            dtheta += self.np_random.normal(0, HEADING_NOISE_RAD)

        new_x = self.pose[0] + dx_world
        new_y = self.pose[1] + dy_world

        # Check if new position collides with wall (robot has physical radius)
        new_gx = int(new_x / CELL_SIZE_MM)
        new_gy = int(new_y / CELL_SIZE_MM)
        in_wall = False
        radius_cells = int(ROBOT_RADIUS_MM / CELL_SIZE_MM) + 1  # cells to check around robot

        for ox in range(-radius_cells, radius_cells + 1):
            for oy in range(-radius_cells, radius_cells + 1):
                cx, cy = new_gx + ox, new_gy + oy
                if 0 <= cx < GRID_SIZE and 0 <= cy < GRID_SIZE and self.wall_mask[cy, cx]:
                    dist = math.sqrt((new_x - (cx + 0.5) * CELL_SIZE_MM) ** 2 +
                                     (new_y - (cy + 0.5) * CELL_SIZE_MM) ** 2)
                    if dist < ROBOT_RADIUS_MM:
                        in_wall = True
                        break
            if in_wall:
                break

        if not in_wall:
            self.pose[0] = new_x
            self.pose[1] = new_y
        # else: blocked by wall — don't move (counted as collision below)

        self.pose[2] += dtheta
        # Normalize heading
        self.pose[2] = math.atan2(math.sin(self.pose[2]), math.cos(self.pose[2]))

        # Clamp to grid bounds
        self.pose[0] = np.clip(self.pose[0], CELL_SIZE_MM, MAP_SIZE_MM - CELL_SIZE_MM)
        self.pose[1] = np.clip(self.pose[1], CELL_SIZE_MM, MAP_SIZE_MM - CELL_SIZE_MM)

        # Mark newly explored cells
        newly_explored = self._mark_explored()

        # Compute task-specific reward
        collision = in_wall or was_blocked
        reward = self._compute_reward(newly_explored, collision, distances)

        self.step_count += 1
        self.total_reward += reward

        # Check termination
        coverage = len(self.explored_cells) / self.total_free
        terminated = False
        truncated = False

        if not hasattr(self, '_collision_count'):
            self._collision_count = 0
        if collision:
            self._collision_count += 1
        if self._collision_count >= 200:
            terminated = True
        elif coverage >= COVERAGE_DONE and self.task in ("explore", "floor_plan"):
            terminated = True
            reward += 5.0
        elif self.step_count >= MAX_STEPS:
            truncated = True
            if coverage > 0.8 and self.task in ("explore", "floor_plan"):
                reward += 5.0

        # New observation from new position
        distances = self._raycast()
        self._last_distances = distances
        obs = self._build_obs(distances)

        info = {
            "pose": self.pose.tolist(),
            "coverage": coverage,
            "explored": len(self.explored_cells),
            "total_free": self.total_free,
            "sectors": self._compute_sectors(distances).tolist(),
            "step": self.step_count,
            "was_blocked": was_blocked,
            "in_wall": in_wall,
        }

        return obs, reward, terminated, truncated, info

    # ================================================================
    # Task-specific observation and reward
    # ================================================================

    def _build_obs(self, distances):
        """Build observation vector based on task type."""
        lidar_bins = self._downsample_scan(distances)

        if self.task == "explore":
            return lidar_bins

        elif self.task == "floor_plan":
            # Add: frontier_angle (normalized), frontier_dist (normalized), coverage
            frontier_angle, frontier_dist = self._find_nearest_frontier()
            coverage = len(self.explored_cells) / self.total_free
            extra = np.array([
                (frontier_angle / math.pi + 1.0) / 2.0,  # [-pi,pi] -> [0,1]
                min(frontier_dist / MAX_RANGE_MM, 1.0),   # normalize dist
                coverage,                                  # [0,1]
            ], dtype=np.float32)
            return np.concatenate([lidar_bins, extra])

        elif self.task == "wall_follow":
            # Add: side_distance (normalized), target_distance (normalized), front_distance
            sectors = self._compute_sectors(distances)
            if self.wall_follow_side == "left":
                side_dist = sectors[6]  # left sector (247.5-292.5)
            else:
                side_dist = sectors[2]  # right sector (67.5-112.5)
            front_dist = sectors[0]  # front sector
            extra = np.array([
                min(side_dist / 3000.0, 1.0),                           # side dist normalized
                min(self.wall_follow_target / 3000.0, 1.0),             # target dist normalized
                min(front_dist / 3000.0, 1.0),                          # front dist normalized
            ], dtype=np.float32)
            return np.concatenate([lidar_bins, extra])

        return lidar_bins

    def _compute_reward(self, newly_explored, collision, distances):
        """Compute task-specific reward."""
        if self.task == "explore":
            reward = -0.1  # time penalty
            reward += newly_explored * 1.0
            if collision:
                reward += -50.0
            return reward

        elif self.task == "floor_plan":
            reward = -0.05  # smaller time penalty (floor plan takes longer)
            reward += newly_explored * 2.0  # stronger exploration reward

            # Reward for moving toward frontiers
            frontier_angle, frontier_dist = self._find_nearest_frontier()
            if frontier_dist < MAX_RANGE_MM:
                # Small reward for facing the frontier
                facing_reward = math.cos(frontier_angle) * 0.1  # max 0.1 if facing frontier
                reward += max(0, facing_reward)

            # Coverage milestone bonuses
            coverage = len(self.explored_cells) / self.total_free
            if not hasattr(self, '_last_milestone'):
                self._last_milestone = 0
            milestone = int(coverage * 10)  # every 10%
            if milestone > self._last_milestone:
                reward += 3.0 * (milestone - self._last_milestone)
                self._last_milestone = milestone

            if collision:
                reward += -20.0  # lighter collision penalty (need to explore tight spaces)
            return reward

        elif self.task == "wall_follow":
            sectors = self._compute_sectors(distances)
            if self.wall_follow_side == "left":
                side_dist = sectors[6]
            else:
                side_dist = sectors[2]
            front_dist = sectors[0]

            # Reward for maintaining target wall distance
            dist_error = abs(side_dist - self.wall_follow_target)
            if dist_error < 50:
                reward = 1.0   # within 50mm of target = good
            elif dist_error < 150:
                reward = 0.3   # within 150mm = ok
            else:
                reward = -0.2  # too far or too close

            # Reward forward progress
            reward += 0.1 if front_dist > STOP_DIST else -0.5

            # Penalize collision
            if collision:
                reward += -30.0

            # Small reward for smooth movement (explored cells as proxy)
            reward += newly_explored * 0.1

            return reward

        return -0.1

    def _find_nearest_frontier(self):
        """Find angle and distance to nearest unexplored frontier from robot.

        Returns (angle_relative, distance_mm).
        angle_relative is relative to robot heading, in [-pi, pi].
        """
        rx = int(self.pose[0] / CELL_SIZE_MM)
        ry = int(self.pose[1] / CELL_SIZE_MM)

        best_dist = float('inf')
        best_angle = 0.0

        # Sample frontiers: free cells adjacent to unexplored cells
        # Check sparse grid (every 5th cell for speed)
        for y in range(0, GRID_SIZE, 5):
            for x in range(0, GRID_SIZE, 5):
                cell = (x, y)
                if cell in self.explored_cells:
                    continue
                if cell not in self.free_cells:
                    continue
                # This is an unexplored free cell — a frontier
                dx = x - rx
                dy = y - ry
                dist = math.sqrt(dx * dx + dy * dy) * CELL_SIZE_MM
                if dist < best_dist:
                    best_dist = dist
                    world_angle = math.atan2(dy, dx)
                    best_angle = world_angle - self.pose[2]
                    # Normalize
                    best_angle = math.atan2(math.sin(best_angle), math.cos(best_angle))

        if best_dist == float('inf'):
            return 0.0, MAX_RANGE_MM  # no frontiers left

        return best_angle, best_dist

    def render(self):
        if self.render_mode is None:
            return None

        try:
            import matplotlib.pyplot as plt
            import matplotlib.patches as patches
        except ImportError:
            return None

        fig, ax = plt.subplots(1, 1, figsize=(8, 8))

        # Draw grid
        img = np.zeros((GRID_SIZE, GRID_SIZE, 3), dtype=np.uint8)
        for y in range(GRID_SIZE):
            for x in range(GRID_SIZE):
                v = self.grid[y, x]
                if v > 2.0:
                    img[y, x] = [180, 0, 0]       # wall (confirmed)
                elif v > 0.8:
                    img[y, x] = [255, 140, 40]     # wall (hint)
                elif v < -1.5:
                    img[y, x] = [40, 180, 40]      # free
                else:
                    img[y, x] = [60, 60, 70]       # unknown

        # Mark explored cells
        for cx, cy in self.explored_cells:
            if 0 <= cx < GRID_SIZE and 0 <= cy < GRID_SIZE:
                img[cy, cx] = [80, 220, 80]  # bright green = explored

        ax.imshow(img, origin='upper')

        # Draw robot
        rx = self.pose[0] / CELL_SIZE_MM
        ry = self.pose[1] / CELL_SIZE_MM
        ax.plot(rx, ry, 'ro', markersize=8)

        # Direction arrow
        arrow_len = 10
        ax.arrow(rx, ry,
                 math.cos(self.pose[2]) * arrow_len,
                 math.sin(self.pose[2]) * arrow_len,
                 head_width=3, head_length=2, fc='yellow', ec='yellow')

        # Ray-cast visualization (every 10th ray)
        distances = self._raycast()
        for i in range(0, NUM_RAYS, 10):
            angle = self.pose[2] + math.radians(i)
            dist = distances[i] / CELL_SIZE_MM
            ex = rx + math.cos(angle) * dist
            ey = ry + math.sin(angle) * dist
            ax.plot([rx, ex], [ry, ey], 'y-', alpha=0.2, linewidth=0.5)

        coverage = len(self.explored_cells) / self.total_free * 100
        ax.set_title(f"Step {self.step_count}  Coverage: {coverage:.1f}%  Reward: {self.total_reward:.1f}")
        ax.set_xlim(0, GRID_SIZE)
        ax.set_ylim(GRID_SIZE, 0)

        if self.render_mode == "rgb_array":
            fig.canvas.draw()
            img_arr = np.frombuffer(fig.canvas.buffer_rgba(), dtype=np.uint8)
            img_arr = img_arr.reshape(fig.canvas.get_width_height()[::-1] + (4,))
            plt.close(fig)
            return img_arr[:, :, :3]
        else:
            plt.show(block=False)
            plt.pause(0.01)
            plt.close(fig)
            return None

    def close(self):
        pass
