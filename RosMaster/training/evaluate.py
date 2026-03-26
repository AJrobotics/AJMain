"""
Evaluate a trained navigation policy.

Runs the policy on test maps and generates:
- Coverage metrics per map
- Collision count
- Episode length
- Visualization GIF of the policy navigating
- Comparison with rule-based explorer

Usage:
    python evaluate.py --model models/nav_ppo_final
    python evaluate.py --model models/nav_ppo_final --maps training/maps --gif
    python evaluate.py --model models/nav_ppo_final --compare-rule-based
"""

import os
import sys
import argparse
import glob
import numpy as np
import math

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stable_baselines3 import PPO
from robot_env import (
    RobotNavEnv, GRID_SIZE, CELL_SIZE_MM, NUM_RAYS, MAX_RANGE_MM,
    NUM_BINS, STOP_DIST, MAX_STEPS
)


def evaluate_policy(model, env, n_episodes=10, deterministic=True, verbose=True):
    """Run policy for n_episodes and collect metrics."""
    results = []

    for ep in range(n_episodes):
        obs, info = env.reset()
        total_reward = 0
        steps = 0
        collisions = 0

        while True:
            action, _ = model.predict(obs, deterministic=deterministic)
            obs, reward, terminated, truncated, info = env.step(action)
            total_reward += reward
            steps += 1

            if info.get("was_blocked"):
                collisions += 1

            if terminated or truncated:
                break

        coverage = info.get("coverage", 0)
        result = {
            "episode": ep,
            "coverage": coverage,
            "reward": total_reward,
            "steps": steps,
            "collisions": collisions,
            "terminated": terminated,
            "in_wall": info.get("in_wall", False),
        }
        results.append(result)

        if verbose:
            status = "CRASH" if info.get("in_wall") else ("DONE" if coverage > 0.9 else "TIMEOUT")
            print(f"  Ep {ep:3d}: coverage={coverage:.1%}  "
                  f"reward={total_reward:7.1f}  steps={steps:4d}  "
                  f"collisions={collisions:3d}  [{status}]")

    return results


def rule_based_policy(obs, pose, sectors):
    """Simple rule-based policy for comparison.

    Mimics basic explorer logic:
    - Find direction with most open space
    - Move toward it, avoiding obstacles
    """
    # Reconstruct approximate distances from normalized bins
    distances = obs * MAX_RANGE_MM  # 36 bins

    # Find the most open direction (bin with max distance)
    best_bin = int(np.argmax(distances))
    best_angle = (best_bin * 10) * math.pi / 180  # bin angle relative to heading

    # Heading error to best direction
    heading_error = best_angle
    if heading_error > math.pi:
        heading_error -= 2 * math.pi

    # Action: turn toward best direction, move forward if aligned
    vz_norm = np.clip(heading_error / math.pi, -1, 1)  # rotation

    if abs(heading_error) < 0.3:  # ~17° — aligned enough
        vx_norm = 0.8  # mostly forward
    elif abs(heading_error) < 1.0:  # ~57°
        vx_norm = 0.3  # slow forward while turning
    else:
        vx_norm = -0.2  # mostly turning

    vy_norm = 0.0  # no strafe in rule-based

    return np.array([vx_norm, vy_norm, vz_norm], dtype=np.float32)


def evaluate_rule_based(env, n_episodes=10, verbose=True):
    """Run rule-based policy for comparison."""
    results = []

    for ep in range(n_episodes):
        obs, info = env.reset()
        total_reward = 0
        steps = 0
        collisions = 0

        while True:
            pose = info.get("pose", [0, 0, 0])
            sectors = info.get("sectors", [9999] * 8)
            action = rule_based_policy(obs, pose, sectors)

            obs, reward, terminated, truncated, info = env.step(action)
            total_reward += reward
            steps += 1

            if info.get("was_blocked"):
                collisions += 1

            if terminated or truncated:
                break

        coverage = info.get("coverage", 0)
        result = {
            "episode": ep,
            "coverage": coverage,
            "reward": total_reward,
            "steps": steps,
            "collisions": collisions,
        }
        results.append(result)

        if verbose:
            print(f"  Ep {ep:3d}: coverage={coverage:.1%}  "
                  f"reward={total_reward:7.1f}  steps={steps:4d}  "
                  f"collisions={collisions:3d}")

    return results


def generate_gif(model, env, output_path, max_steps=500, fps=5):
    """Generate a GIF visualization of the policy navigating."""
    try:
        import matplotlib.pyplot as plt
        import matplotlib.animation as animation
        from matplotlib.patches import FancyArrowPatch
    except ImportError:
        print("matplotlib required for GIF generation")
        return

    frames = []
    obs, info = env.reset()

    for step in range(max_steps):
        # Render frame
        frame = env.render()
        if frame is not None:
            frames.append(frame)

        action, _ = model.predict(obs, deterministic=True)
        obs, reward, terminated, truncated, info = env.step(action)

        if terminated or truncated:
            # Final frame
            frame = env.render()
            if frame is not None:
                frames.append(frame)
            break

    if not frames:
        print("No frames generated")
        return

    # Save as GIF
    fig, ax = plt.subplots(figsize=(8, 8))

    def animate(i):
        ax.clear()
        ax.imshow(frames[i])
        ax.set_title(f"Step {i}")
        ax.axis('off')

    anim = animation.FuncAnimation(fig, animate, frames=len(frames), interval=1000 // fps)
    anim.save(output_path, writer='pillow', fps=fps)
    plt.close(fig)
    print(f"GIF saved to {output_path} ({len(frames)} frames)")


def print_summary(results, label="Policy"):
    """Print summary statistics."""
    coverages = [r["coverage"] for r in results]
    rewards = [r["reward"] for r in results]
    steps = [r["steps"] for r in results]
    collisions = [r["collisions"] for r in results]

    print(f"\n{'='*50}")
    print(f"  {label} — {len(results)} episodes")
    print(f"{'='*50}")
    print(f"  Coverage:    {np.mean(coverages):.1%} ± {np.std(coverages):.1%}  "
          f"(min={np.min(coverages):.1%}, max={np.max(coverages):.1%})")
    print(f"  Reward:      {np.mean(rewards):.1f} ± {np.std(rewards):.1f}")
    print(f"  Steps:       {np.mean(steps):.0f} ± {np.std(steps):.0f}")
    print(f"  Collisions:  {np.mean(collisions):.1f} ± {np.std(collisions):.1f}")
    print(f"{'='*50}\n")


def main():
    parser = argparse.ArgumentParser(description="Evaluate navigation policy")
    parser.add_argument("--model", type=str, required=True,
                        help="Path to trained model (without .zip)")
    parser.add_argument("--maps", type=str, default=None,
                        help="Directory with test maps")
    parser.add_argument("--episodes", type=int, default=10,
                        help="Episodes per map")
    parser.add_argument("--gif", action="store_true",
                        help="Generate visualization GIF")
    parser.add_argument("--gif-output", type=str, default="eval_navigation.gif",
                        help="GIF output path")
    parser.add_argument("--compare-rule-based", action="store_true",
                        help="Also evaluate rule-based policy for comparison")
    args = parser.parse_args()

    # Load model
    print(f"Loading model from {args.model}...")
    model = PPO.load(args.model)

    # Collect test maps
    if args.maps and os.path.isdir(args.maps):
        map_files = sorted(glob.glob(os.path.join(args.maps, "*.npz")))[:5]
        print(f"Testing on {len(map_files)} maps")
    else:
        map_files = [None]  # default room
        print("Testing on default room")

    # Evaluate NN policy
    all_nn_results = []
    all_rb_results = []

    for map_path in map_files:
        map_name = os.path.basename(map_path) if map_path else "default"
        print(f"\n--- Map: {map_name} ---")

        env = RobotNavEnv(map_path=map_path, render_mode="rgb_array" if args.gif else None)

        print("Neural Network policy:")
        nn_results = evaluate_policy(model, env, n_episodes=args.episodes)
        all_nn_results.extend(nn_results)

        if args.compare_rule_based:
            print("\nRule-based policy:")
            rb_results = evaluate_rule_based(env, n_episodes=args.episodes)
            all_rb_results.extend(rb_results)

        env.close()

    # Summary
    print_summary(all_nn_results, "Neural Network")
    if args.compare_rule_based:
        print_summary(all_rb_results, "Rule-based")

        # Comparison
        nn_cov = np.mean([r["coverage"] for r in all_nn_results])
        rb_cov = np.mean([r["coverage"] for r in all_rb_results])
        diff = nn_cov - rb_cov
        print(f"  NN vs Rule-based coverage: {diff:+.1%}")

    # Generate GIF
    if args.gif:
        print("\nGenerating visualization GIF...")
        env = RobotNavEnv(map_path=map_files[0], render_mode="rgb_array")
        generate_gif(model, env, args.gif_output)
        env.close()


if __name__ == "__main__":
    main()
