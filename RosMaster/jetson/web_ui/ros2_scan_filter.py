#!/usr/bin/env python3
"""
ROS2 LaserScan filter node for RosMaster X3.
Subscribes to /scan, filters out the rear ignore zone (blocked by robot body),
and republishes to /scan_filtered for SLAM nodes.

Usage:
    source /opt/ros/humble/setup.bash
    python3 ros2_scan_filter.py --ignore-angle 140
"""

import math
import sys
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import LaserScan


class ScanFilterNode(Node):
    def __init__(self, ignore_angle=140):
        super().__init__('scan_filter')
        self.ignore_angle = ignore_angle  # degrees centered at 180° (rear)
        self.half_ignore_rad = math.radians(ignore_angle / 2.0)

        self.sub = self.create_subscription(LaserScan, '/scan', self.scan_callback, 10)
        self.pub = self.create_publisher(LaserScan, '/scan_filtered', 10)

        self.get_logger().info(
            f'Scan filter: ignoring {ignore_angle}° rear zone '
            f'({180 - ignore_angle//2}° to {180 + ignore_angle//2}°)')

    def scan_callback(self, msg):
        """Filter out points in the rear ignore zone by setting range to inf."""
        filtered = LaserScan()
        filtered.header = msg.header
        filtered.angle_min = msg.angle_min
        filtered.angle_max = msg.angle_max
        filtered.angle_increment = msg.angle_increment
        filtered.time_increment = msg.time_increment
        filtered.scan_time = msg.scan_time
        filtered.range_min = msg.range_min
        filtered.range_max = msg.range_max

        ranges = list(msg.ranges)
        intensities = list(msg.intensities) if msg.intensities else []

        for i in range(len(ranges)):
            # Compute angle for this ray
            angle = msg.angle_min + i * msg.angle_increment

            # Normalize to 0..2pi (0 = forward)
            angle_norm = angle % (2 * math.pi)

            # Check if in rear ignore zone (centered at pi = 180°)
            angle_from_rear = abs(angle_norm - math.pi)
            if angle_from_rear < self.half_ignore_rad:
                ranges[i] = float('inf')  # mark as no-return
                if intensities:
                    intensities[i] = 0.0

        filtered.ranges = ranges
        filtered.intensities = intensities
        self.pub.publish(filtered)


def main():
    ignore_angle = 140
    for i, arg in enumerate(sys.argv):
        if arg == '--ignore-angle' and i + 1 < len(sys.argv):
            ignore_angle = int(sys.argv[i + 1])

    rclpy.init()
    node = ScanFilterNode(ignore_angle=ignore_angle)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()
