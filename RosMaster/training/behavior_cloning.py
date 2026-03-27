"""Behavior cloning: learn navigation from recorded route data.

Uses optical flow between consecutive RGB keyframes to compute the
movement direction (desired action), then trains a CNN+MLP model to
predict actions from (RGB + LiDAR) inputs.

Input: RGB frame (resized) + 36 LiDAR bins
Output: (vx, vy, vz) motor commands

Usage:
    python behavior_cloning.py --route P2 --epochs 100
    python behavior_cloning.py --route P2 --export  # export to ONNX
"""

import os
import sys
import json
import math
import argparse
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def compute_actions_from_optical_flow(route_dir):
    """Compute movement actions from consecutive RGB keyframes using optical flow.

    Returns list of (frame_path, lidar_36bins, action) tuples.
    """
    import cv2

    frames_dir = os.path.join(route_dir, "frames")
    wp_path = os.path.join(route_dir, "waypoints.json")

    with open(wp_path) as f:
        waypoints = json.load(f)

    # Get keyframe waypoints (frame_idx >= 0) in order
    keyframe_wps = sorted(
        [w for w in waypoints if w["frame_idx"] >= 0],
        key=lambda w: w["frame_idx"]
    )
    print(f"Found {len(keyframe_wps)} keyframes")

    # Load all keyframe images
    frames = []
    for wp in keyframe_wps:
        path = os.path.join(frames_dir, f"frame_{wp['frame_idx']:06d}.jpg")
        if os.path.exists(path):
            img = cv2.imread(path)
            frames.append((path, img, wp))

    print(f"Loaded {len(frames)} frames")

    # Compute optical flow between consecutive frames
    samples = []
    for i in range(len(frames) - 1):
        path_curr, img_curr, wp_curr = frames[i]
        path_next, img_next, wp_next = frames[i + 1]

        # Convert to grayscale
        gray_curr = cv2.cvtColor(img_curr, cv2.COLOR_BGR2GRAY)
        gray_next = cv2.cvtColor(img_next, cv2.COLOR_BGR2GRAY)

        # Resize for optical flow
        gray_curr = cv2.resize(gray_curr, (160, 120))
        gray_next = cv2.resize(gray_next, (160, 120))

        # Dense optical flow (Farneback)
        flow = cv2.calcOpticalFlowFarneback(
            gray_curr, gray_next, None,
            pyr_scale=0.5, levels=3, winsize=15,
            iterations=3, poly_n=5, poly_sigma=1.2, flags=0
        )

        # Average flow gives overall movement direction
        mean_flow_x = np.mean(flow[:, :, 0])  # horizontal (left-right)
        mean_flow_y = np.mean(flow[:, :, 1])  # vertical (up-down in image)

        # Image flow to robot action mapping:
        # - flow_x negative = camera moved right = robot moved right (vy > 0) or turned left (vz > 0)
        # - flow_y negative = camera moved down = robot moved forward (vx > 0)
        # - Large flow = fast movement

        # Time delta between frames
        dt = wp_next["t"] - wp_curr["t"]
        if dt < 0.05:
            dt = 0.2  # fallback

        # Scale flow to velocity-like values
        # These are approximate — the NN will learn the mapping
        flow_magnitude = math.sqrt(mean_flow_x ** 2 + mean_flow_y ** 2)

        # Forward speed from vertical flow (image moves down when going forward)
        vx = np.clip(-mean_flow_y * 0.01, -0.05, 0.15)

        # Lateral strafe from horizontal flow
        # Remove rotation component: subtract left-right flow difference
        flow_x_left = np.mean(flow[:, :80, 0])
        flow_x_right = np.mean(flow[:, 80:, 0])
        rotation_flow = (flow_x_right - flow_x_left) / 2
        pure_lateral_flow = mean_flow_x - rotation_flow  # remove rotation from horizontal flow
        vy = np.clip(-pure_lateral_flow * 0.005, -0.12, 0.12)

        # Rotation from IMU yaw (ground truth — more reliable than optical flow)
        imu_curr = wp_curr.get("imu_yaw", 0)
        imu_next = wp_next.get("imu_yaw", 0)
        imu_delta = math.atan2(math.sin(math.radians(imu_next - imu_curr)),
                               math.cos(math.radians(imu_next - imu_curr)))
        vz = np.clip(imu_delta / dt, -1.0, 1.0)  # rad/s

        # Downsample LiDAR to 36 bins
        scan = wp_curr.get("scan", [])
        lidar_bins = _scan_to_36bins(scan)

        # Normalize action to [-1, 1] for training
        action = np.array([
            np.clip((vx - 0.05) / 0.10, -1, 1),   # vx: [-0.05,0.15] -> [-1,1]
            np.clip(vy / 0.12, -1, 1),               # vy: [-0.12,0.12] -> [-1,1]
            np.clip(vz / 1.0, -1, 1),                # vz: [-1,1] -> [-1,1]
        ], dtype=np.float32)

        samples.append({
            "frame_path": path_curr,
            "lidar_bins": lidar_bins,
            "action": action,
            "flow_magnitude": flow_magnitude,
            "dt": dt,
        })

    # Filter out stationary frames (very low flow)
    moving = [s for s in samples if s["flow_magnitude"] > 0.3]
    stationary = [s for s in samples if s["flow_magnitude"] <= 0.3]
    print(f"Moving frames: {len(moving)}, Stationary: {len(stationary)}")

    # Keep some stationary frames (teach robot to stop when no movement needed)
    # but subsample to avoid bias
    if len(stationary) > len(moving) // 3:
        np.random.shuffle(stationary)
        stationary = stationary[:len(moving) // 3]
    # Set stationary action to (0, 0, 0)
    for s in stationary:
        s["action"] = np.array([0, 0, 0], dtype=np.float32)

    all_samples = moving + stationary
    np.random.shuffle(all_samples)
    print(f"Total training samples: {len(all_samples)}")
    return all_samples


def _scan_to_36bins(scan):
    """Convert LiDAR scan to 36 normalized bins."""
    distances = np.full(360, 6000.0, dtype=np.float32)
    for angle, dist in scan:
        idx = int(round(angle)) % 360
        if 0 < dist < distances[idx]:
            distances[idx] = dist
    bins = distances.reshape(36, 10).min(axis=1)
    return np.clip(bins / 6000.0, 0, 1).astype(np.float32)


class NavigationNet:
    """CNN (for RGB) + MLP (for LiDAR) navigation network."""

    def __init__(self):
        import torch
        import torch.nn as nn

        class Net(nn.Module):
            def __init__(self):
                super().__init__()
                # CNN for RGB image (80x60 grayscale)
                self.cnn = nn.Sequential(
                    nn.Conv2d(1, 16, 5, stride=2, padding=2),  # -> 40x30
                    nn.ReLU(),
                    nn.Conv2d(16, 32, 3, stride=2, padding=1),  # -> 20x15
                    nn.ReLU(),
                    nn.Conv2d(32, 32, 3, stride=2, padding=1),  # -> 10x8
                    nn.ReLU(),
                    nn.AdaptiveAvgPool2d((2, 2)),  # -> 2x2 (ONNX compatible)
                    nn.Flatten(),  # -> 128
                )
                # MLP for LiDAR bins
                self.lidar_mlp = nn.Sequential(
                    nn.Linear(36, 64),
                    nn.ReLU(),
                )
                # Combined head
                self.head = nn.Sequential(
                    nn.Linear(128 + 64, 128),
                    nn.ReLU(),
                    nn.Linear(128, 64),
                    nn.ReLU(),
                    nn.Linear(64, 3),
                    nn.Tanh(),  # output in [-1, 1]
                )

            def forward(self, img, lidar):
                img_feat = self.cnn(img)
                lidar_feat = self.lidar_mlp(lidar)
                combined = torch.cat([img_feat, lidar_feat], dim=1)
                return self.head(combined)

        self.model = Net()
        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        self.model.to(self.device)

    def train_on_samples(self, samples, epochs=100, lr=1e-3, batch_size=32):
        """Train the model on behavior cloning samples."""
        import torch
        import torch.nn as nn
        import cv2

        # Prepare data
        images = []
        lidars = []
        actions = []

        for s in samples:
            # Load and preprocess image
            img = cv2.imread(s["frame_path"], cv2.IMREAD_GRAYSCALE)
            if img is None:
                continue
            img = cv2.resize(img, (80, 60))
            img = img.astype(np.float32) / 255.0
            images.append(img)
            lidars.append(s["lidar_bins"])
            actions.append(s["action"])

        if len(images) < 10:
            print("Too few samples for training")
            return

        images = torch.tensor(np.array(images), dtype=torch.float32).unsqueeze(1)  # (N,1,60,80)
        lidars = torch.tensor(np.array(lidars), dtype=torch.float32)
        actions = torch.tensor(np.array(actions), dtype=torch.float32)

        # Move to device
        images = images.to(self.device)
        lidars = lidars.to(self.device)
        actions = actions.to(self.device)

        # Train
        optimizer = torch.optim.Adam(self.model.parameters(), lr=lr)
        loss_fn = nn.MSELoss()
        n = len(images)

        self.model.train()
        for epoch in range(epochs):
            # Shuffle
            perm = torch.randperm(n)
            total_loss = 0
            batches = 0

            for i in range(0, n, batch_size):
                idx = perm[i:i + batch_size]
                pred = self.model(images[idx], lidars[idx])
                loss = loss_fn(pred, actions[idx])

                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

                total_loss += loss.item()
                batches += 1

            avg_loss = total_loss / batches
            if (epoch + 1) % 10 == 0 or epoch == 0:
                print(f"Epoch {epoch+1}/{epochs}  Loss: {avg_loss:.6f}")

        self.model.eval()
        print("Training complete")

    def save(self, output_dir, name="route_nav"):
        """Save model as TorchScript and ONNX."""
        import torch
        os.makedirs(output_dir, exist_ok=True)

        self.model.cpu()
        self.model.eval()

        # Dummy inputs
        dummy_img = torch.randn(1, 1, 60, 80)
        dummy_lidar = torch.randn(1, 36)

        # TorchScript
        ts_path = os.path.join(output_dir, f"{name}_policy.pt")
        traced = torch.jit.trace(self.model, (dummy_img, dummy_lidar))
        traced.save(ts_path)
        print(f"TorchScript saved: {ts_path}")

        # ONNX
        onnx_path = os.path.join(output_dir, f"{name}_policy.onnx")
        torch.onnx.export(
            self.model,
            (dummy_img, dummy_lidar),
            onnx_path,
            input_names=["image", "lidar"],
            output_names=["action"],
            opset_version=11,
            dynamo=False,
        )
        print(f"ONNX saved: {onnx_path}")
        return ts_path, onnx_path


def train(args):
    """Main training function."""
    # Support multiple routes: --route P2,P3 or --route ALL
    route_names = args.route.split(",")
    if args.route.upper() == "ALL":
        route_names = [d for d in os.listdir(args.routes_dir)
                       if os.path.isdir(os.path.join(args.routes_dir, d))
                       and os.path.exists(os.path.join(args.routes_dir, d, "waypoints.json"))]

    print(f"=== Behavior Cloning: {', '.join(route_names)} ===")

    # Step 1: Compute actions from optical flow for all routes
    all_samples = []
    for name in route_names:
        route_dir = os.path.join(args.routes_dir, name.strip())
        if not os.path.exists(route_dir):
            print(f"Route not found: {route_dir}, skipping")
            continue
        print(f"\n--- Processing route: {name} ---")
        samples = compute_actions_from_optical_flow(route_dir)
        all_samples.extend(samples)
        print(f"  Added {len(samples)} samples (total: {len(all_samples)})")

    if not all_samples:
        print("No samples generated")
        return

    np.random.shuffle(all_samples)
    samples = all_samples
    print(f"\n--- Total training samples: {len(samples)} ---")

    # Step 2: Train model
    print(f"\n--- Training ({args.epochs} epochs) ---")
    net = NavigationNet()
    net.train_on_samples(samples, epochs=args.epochs, lr=args.lr)

    # Step 3: Export
    output_dir = os.path.join(args.model_dir, "route_nav")
    ts_path, onnx_path = net.save(output_dir, name="route_nav")

    # Step 4: Quick eval — predict on first few samples
    import torch
    print("\n--- Sample predictions ---")
    net.model.eval()
    import cv2
    for s in samples[:5]:
        img = cv2.imread(s["frame_path"], cv2.IMREAD_GRAYSCALE)
        img = cv2.resize(img, (80, 60)).astype(np.float32) / 255.0
        img_t = torch.tensor(img).unsqueeze(0).unsqueeze(0)
        lidar_t = torch.tensor(s["lidar_bins"]).unsqueeze(0)
        with torch.no_grad():
            pred = net.model(img_t, lidar_t).numpy()[0]
        actual = s["action"]
        print(f"  Pred: ({pred[0]:+.2f},{pred[1]:+.2f},{pred[2]:+.2f})  "
              f"Actual: ({actual[0]:+.2f},{actual[1]:+.2f},{actual[2]:+.2f})  "
              f"Flow: {s['flow_magnitude']:.1f}")

    print(f"\nDone! Model saved to {output_dir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Behavior cloning from recorded routes")
    parser.add_argument("--route", type=str, required=True,
                        help="Route name (e.g., P2)")
    parser.add_argument("--routes-dir", type=str, default=None,
                        help="Routes directory (default: auto-detect)")
    parser.add_argument("--model-dir", type=str, default="training/models",
                        help="Output model directory")
    parser.add_argument("--epochs", type=int, default=100,
                        help="Training epochs")
    parser.add_argument("--lr", type=float, default=1e-3,
                        help="Learning rate")
    args = parser.parse_args()

    # Auto-detect routes directory
    if args.routes_dir is None:
        # Check Jetson path first, then local
        if os.path.exists("/home/jetson/RosMaster/routes"):
            args.routes_dir = "/home/jetson/RosMaster/routes"
        else:
            args.routes_dir = os.path.join(os.path.dirname(__file__), "..", "routes")

    train(args)
