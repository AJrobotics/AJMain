"""
PPO training script for robot navigation policy.

Uses stable-baselines3 PPO with the RobotNavEnv gymnasium environment.
Trains on procedurally generated maps for domain randomization.

Usage:
    python train.py                          # Train on default room
    python train.py --maps training/maps     # Train on generated maps
    python train.py --resume models/nav_ppo  # Resume training
    python train.py --export                 # Export to ONNX after training
"""

import os
import sys
import argparse
import glob
import numpy as np

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stable_baselines3 import PPO
from stable_baselines3.common.env_util import make_vec_env
from stable_baselines3.common.callbacks import (
    EvalCallback, CheckpointCallback, BaseCallback
)
from stable_baselines3.common.monitor import Monitor

from robot_env import RobotNavEnv, TASK_TYPES, NUM_BINS


class MapCurriculumCallback(BaseCallback):
    """Switch to a different map every N episodes for domain randomization."""

    def __init__(self, map_files, switch_every=20, verbose=0):
        super().__init__(verbose)
        self.map_files = map_files
        self.switch_every = switch_every
        self.episode_count = 0
        self.map_idx = 0

    def _on_step(self):
        # Check if any episode ended
        if self.locals.get("dones") is not None:
            for done in self.locals["dones"]:
                if done:
                    self.episode_count += 1
                    if self.episode_count % self.switch_every == 0 and self.map_files:
                        self.map_idx = (self.map_idx + 1) % len(self.map_files)
                        new_map = self.map_files[self.map_idx]
                        # Update environment map
                        env = self.training_env.envs[0]
                        if hasattr(env, 'env'):
                            env = env.env  # unwrap Monitor
                        env._load_map(new_map)
                        env._compute_explorable_cells()
                        env.wall_mask = env.grid > 0.8
                        if self.verbose > 0:
                            print(f"[Curriculum] Episode {self.episode_count}: "
                                  f"switched to {os.path.basename(new_map)}")
        return True


class RewardLogCallback(BaseCallback):
    """Log episode rewards and coverage for monitoring."""

    def __init__(self, log_interval=10, verbose=0):
        super().__init__(verbose)
        self.log_interval = log_interval
        self.episode_rewards = []
        self.episode_coverages = []
        self.episode_count = 0

    def _on_step(self):
        if self.locals.get("dones") is not None:
            for i, done in enumerate(self.locals["dones"]):
                if done:
                    self.episode_count += 1
                    info = self.locals["infos"][i]
                    coverage = info.get("coverage", 0)
                    self.episode_coverages.append(coverage)

                    if self.episode_count % self.log_interval == 0:
                        avg_cov = np.mean(self.episode_coverages[-self.log_interval:])
                        print(f"[Episode {self.episode_count}] "
                              f"Avg coverage: {avg_cov:.1%} "
                              f"(last {self.log_interval} episodes)")
        return True


def make_env(map_path=None, task="explore"):
    """Create a wrapped environment."""
    def _init():
        env = RobotNavEnv(map_path=map_path, task=task)
        return Monitor(env)
    return _init


def train(args):
    """Main training function."""
    task = args.task
    task_info = TASK_TYPES[task]
    obs_size = task_info["obs_size"]

    # Task-specific model directory
    model_dir = os.path.join(args.model_dir, task)
    log_dir = os.path.join(args.log_dir, task)
    os.makedirs(model_dir, exist_ok=True)
    os.makedirs(log_dir, exist_ok=True)

    # Collect map files for curriculum
    map_files = []
    if args.maps and os.path.isdir(args.maps):
        map_files = sorted(glob.glob(os.path.join(args.maps, "*.npz")))
        print(f"Found {len(map_files)} training maps in {args.maps}")

    # Create environment
    initial_map = map_files[0] if map_files else None
    env = make_env(initial_map, task=task)()

    # Create or load model
    resume_path = args.resume or os.path.join(model_dir, f"{task}_ppo_final")
    if os.path.exists(resume_path + ".zip"):
        print(f"Resuming from {resume_path}")
        model = PPO.load(resume_path, env=env)
    else:
        print(f"Creating new PPO model for task: {task} ({task_info['desc']})")
        print(f"  Observation size: {obs_size}")
        model = PPO(
            "MlpPolicy",
            env,
            policy_kwargs=dict(
                net_arch=[128, 64],     # 2-layer MLP
            ),
            learning_rate=3e-4,
            n_steps=2048,              # steps per rollout
            batch_size=64,
            n_epochs=10,               # PPO epochs per update
            gamma=0.99,                # discount factor
            gae_lambda=0.95,           # GAE lambda
            clip_range=0.2,            # PPO clip range
            ent_coef=0.01,             # entropy bonus (encourage exploration)
            verbose=1,
            tensorboard_log=log_dir,
            device="auto",
        )

    # Callbacks
    callbacks = []

    # Checkpoint every 10K steps
    callbacks.append(CheckpointCallback(
        save_freq=10000,
        save_path=model_dir,
        name_prefix=f"{task}_ppo",
    ))

    # Reward logging
    callbacks.append(RewardLogCallback(log_interval=10, verbose=1))

    # Map curriculum
    if map_files:
        callbacks.append(MapCurriculumCallback(
            map_files=map_files,
            switch_every=args.curriculum_interval,
            verbose=1,
        ))

    # Train
    print(f"\nTraining for {args.timesteps:,} timesteps...")
    print(f"  Task: {task} ({task_info['desc']})")
    print(f"  Model: MLP [{obs_size} -> 128 -> 64 -> 3]")
    print(f"  Maps: {len(map_files) if map_files else 'default room'}")
    print(f"  TensorBoard: tensorboard --logdir {log_dir}\n")

    model.learn(
        total_timesteps=args.timesteps,
        callback=callbacks,
        progress_bar=True,
    )

    # Save final model
    save_path = os.path.join(model_dir, f"{task}_ppo_final")
    model.save(save_path)
    print(f"\nModel saved to {save_path}")

    # Export to ONNX
    if args.export:
        export_onnx(model, model_dir, task=task)

    env.close()


def export_onnx(model, output_dir, task="explore"):
    """Export the trained policy to ONNX format for browser deployment."""
    import torch

    obs_size = TASK_TYPES[task]["obs_size"]
    print(f"\nExporting {task} model to ONNX (obs_size={obs_size})...")

    # Get the policy network
    policy = model.policy

    # Create a wrapper that takes observation and returns action (deterministic)
    class PolicyWrapper(torch.nn.Module):
        def __init__(self, policy):
            super().__init__()
            self.mlp = policy.mlp_extractor
            self.action_net = policy.action_net

        def forward(self, obs):
            features = self.mlp.forward_actor(obs)
            action_mean = self.action_net(features)
            return torch.tanh(action_mean)

    wrapper = PolicyWrapper(policy)
    wrapper.cpu()
    wrapper.eval()

    # Dummy input matching observation size
    dummy = torch.randn(1, obs_size, device='cpu')

    # Export ONNX
    onnx_path = os.path.join(output_dir, f"{task}_policy.onnx")
    torch.onnx.export(
        wrapper,
        dummy,
        onnx_path,
        input_names=["observation"],
        output_names=["action"],
        dynamic_axes={
            "observation": {0: "batch"},
            "action": {0: "batch"},
        },
        opset_version=11,
        dynamo=False,
    )
    print(f"ONNX model saved to {onnx_path}")

    # Also save as TorchScript for Jetson deployment
    scripted = torch.jit.trace(wrapper, dummy)
    ts_path = os.path.join(output_dir, f"{task}_policy.pt")
    scripted.save(ts_path)
    print(f"TorchScript model saved to {ts_path}")

    # Verify ONNX
    try:
        import onnx
        onnx_model = onnx.load(onnx_path)
        onnx.checker.check_model(onnx_model)
        print("ONNX model verified OK")
    except ImportError:
        print("(onnx package not installed, skipping verification)")

    return onnx_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train robot navigation policy")
    parser.add_argument("--task", type=str, default="explore",
                        choices=list(TASK_TYPES.keys()),
                        help=f"Task type: {', '.join(TASK_TYPES.keys())}")
    parser.add_argument("--timesteps", type=int, default=100000,
                        help="Total training timesteps (default: 100K)")
    parser.add_argument("--maps", type=str, default=None,
                        help="Directory with .npz map files for curriculum")
    parser.add_argument("--resume", type=str, default=None,
                        help="Path to saved model to resume training")
    parser.add_argument("--model-dir", type=str, default="training/models",
                        help="Base directory to save models")
    parser.add_argument("--log-dir", type=str, default="training/tb_logs",
                        help="TensorBoard log directory")
    parser.add_argument("--export", action="store_true",
                        help="Export to ONNX after training")
    parser.add_argument("--curriculum-interval", type=int, default=20,
                        help="Switch map every N episodes")
    args = parser.parse_args()

    train(args)
