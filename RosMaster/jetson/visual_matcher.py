"""Visual place recognition using ORB features.

Matches current camera frame against recorded route keyframes to determine
the robot's position along the route. Uses OpenCV ORB + FLANN matcher.

Works on both Jetson (deployment) and Dreamer (testing with saved frames).
"""

import os
import numpy as np


class VisualMatcher:
    """ORB-based visual place recognition for route following."""

    def __init__(self):
        import cv2
        self.cv2 = cv2
        self.orb = cv2.ORB_create(nfeatures=500)
        self.clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))

        # FLANN matcher for binary descriptors (ORB)
        FLANN_INDEX_LSH = 6
        self.matcher = cv2.FlannBasedMatcher(
            dict(algorithm=FLANN_INDEX_LSH, table_number=6, key_size=12, multi_probe_level=1),
            dict(checks=50)
        )

        # Route features
        self.route_descriptors = []  # list of descriptor arrays per keyframe
        self.route_keypoints = []    # list of keypoint info per keyframe
        self.loaded = False

    def load_route_features(self, route_dir):
        """Load precomputed ORB features from a route directory."""
        features_path = os.path.join(route_dir, "features.npz")
        if not os.path.exists(features_path):
            print(f"No features file: {features_path}")
            return False

        try:
            data = np.load(features_path, allow_pickle=True)
            self.route_descriptors = list(data["descriptors"])
            self.route_keypoints = list(data["keypoints"])
            self.loaded = True
            print(f"Loaded {len(self.route_descriptors)} keyframe features")
            return True
        except Exception as e:
            print(f"Feature load error: {e}")
            return False

    def extract_features(self, frame):
        """Extract ORB features from a single frame.

        Args:
            frame: BGR image (any size)

        Returns:
            (keypoints, descriptors) or (None, None) if extraction fails
        """
        cv2 = self.cv2
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (320, 240))
        gray = self.clahe.apply(gray)
        kps, descs = self.orb.detectAndCompute(gray, None)
        return kps, descs

    def match(self, current_frame, expected_idx=0, search_window=20):
        """Match current camera frame against route keyframes.

        Args:
            current_frame: BGR image from camera
            expected_idx: expected position along route (keyframe index)
            search_window: number of keyframes to search around expected_idx

        Returns:
            dict with:
                match_idx: best matching keyframe index (-1 if no match)
                confidence: match quality [0, 1]
                inliers: number of good matches
                total_features: features in current frame
        """
        if not self.loaded or not self.route_descriptors:
            return {"match_idx": -1, "confidence": 0, "inliers": 0, "total_features": 0}

        # Extract features from current frame
        kps, descs = self.extract_features(current_frame)
        if descs is None or len(descs) < 10:
            return {"match_idx": -1, "confidence": 0, "inliers": 0,
                    "total_features": len(descs) if descs is not None else 0}

        # Search window around expected position
        start = max(0, expected_idx - search_window)
        end = min(len(self.route_descriptors), expected_idx + search_window + 1)

        best_idx = -1
        best_inliers = 0
        best_confidence = 0

        for i in range(start, end):
            route_descs = self.route_descriptors[i]
            if route_descs is None or len(route_descs) < 5:
                continue

            try:
                matches = self.matcher.knnMatch(descs, route_descs, k=2)
            except Exception:
                continue

            # Lowe's ratio test
            good = []
            for m_list in matches:
                if len(m_list) == 2:
                    m, n = m_list
                    if m.distance < 0.7 * n.distance:
                        good.append(m)

            inliers = len(good)
            confidence = inliers / max(len(descs), 1)

            if inliers > best_inliers:
                best_inliers = inliers
                best_idx = i
                best_confidence = confidence

        return {
            "match_idx": best_idx,
            "confidence": round(best_confidence, 3),
            "inliers": best_inliers,
            "total_features": len(descs),
        }

    def match_from_file(self, frame_path, expected_idx=0, search_window=20):
        """Match a saved frame file against route features."""
        cv2 = self.cv2
        frame = cv2.imread(frame_path)
        if frame is None:
            return {"match_idx": -1, "confidence": 0, "inliers": 0, "total_features": 0}
        return self.match(frame, expected_idx, search_window)
