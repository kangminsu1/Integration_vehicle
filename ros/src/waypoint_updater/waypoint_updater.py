#!/usr/bin/env python

import rospy
from geometry_msgs.msg import PoseStamped, TwistStamped
from styx_msgs.msg import Lane, Waypoint
from std_msgs.msg import Int32, Float32

import math
import numpy as np
import tf

'''
This node will publish waypoints from the car's current position to some `x` distance ahead.

As mentioned in the doc, you should ideally first implement a version which does not care
about traffic lights or obstacles.

Once you have created dbw_node, you will update this node to use the status of traffic lights too.

Please note that our simulator also provides the exact location of traffic lights and their
current status in `/vehicle/traffic_lights` message. You can use this message to build this node
as well as to verify your TL classifier.

TODO (for Yousuf and Aaron): Stopline location for each traffic light.
'''

LOOKAHEAD_WPS = 200 # Number of waypoints we will publish. You can change this number
BUFFER_BREAK = 5 # How far away are you from the stop line, how much space you want your car to have.
TIME_TRIGGER = 1  # Defines whether the action is triggered by a timer or by an incoming pose.

class WaypointUpdater(object):
    def __init__(self):
        rospy.init_node('waypoint_updater')

        rospy.Subscriber('/current_pose', PoseStamped, self.pose_cb)
        rospy.Subscriber('/base_waypoints', Lane, self.waypoints_cb)
        rospy.Subscriber('/traffic_waypoint',  Int32, self.traffic_cb)
        rospy.Subscriber('/obstacle_waypoint', Int32, self.obstacle_cb)
        rospy.Subscriber('/current_velocity', TwistStamped, self.current_velocity_cb)

        self.final_waypoints_pub = rospy.Publisher('final_waypoints', Lane, queue_size=1)

        self.waypoint_final = None
        self.waypoint_traffic = -1

        # FSM state
        # 0 = drive normally
        # 1 = brake
        self.state = 0
        self.acceleration_break = None
        self.limit_acceleration_break = rospy.get_param('~decel_limit', -5)

        if TIME_TRIGGER:
            rate = rospy.Rate(10)
            while not rospy.is_shutdown():
                self.action()
                rate.sleep()
        else:
            self.decimator_i = 0
            self.decimator_n = 10
            rospy.spin()

    """
    Main action
    """
    def action(self):
        # Proceed only if the first messages have been received
        if hasattr(self, 'current_pose') and hasattr(self, 'base_waypoints'):

            # Start from the next (see later whether to make it more sophisticated)
            next_wp = self.get_next_waypoint()

            # State transition
            if self.state == 0:
                # Check if there's a traffic light in sight
                if self.waypoint_traffic != -1:
                    # Useful variables to take decisions
                    rospy.logwarn("[WU] DEBUG: {}".format(self.waypoint_traffic))
                    traffic_light_distance = self.distance_wp(self.base_waypoints.waypoints, next_wp, self.waypoint_traffic)

                    # Get minimum stopping distance
                    min_distance = abs(self.current_velocity**2 / (2*self.limit_acceleration_break))

                    # rospy.logwarn("[WU] Traffic light distance / min stop distance: {:0.1f} / {:0.1f}".format(traffic_light_distance, min_distance))

                    # Decide what to do if there's not enough room to brake
                    if traffic_light_distance > min_distance:
                        self.state = 1
                        # compute braking deceleration
                        self.acceleration_break = abs(self.current_velocity**2 / (2*traffic_light_distance))
                        rospy.logwarn("[WU] Braking deceleration: {}".format(self.acceleration_break))
                    else:
                        # NOTE: Is this really the behavior we want? Should we not try to break even if we violate the acceleration limit?
                        rospy.logwarn("[WU] Too late to break !!")
                        self.state = 0
                # Nothing in sight
                else:
                    self.state = 0

            elif self.state == 1:
                if self.waypoint_traffic == -1:
                    # There's either no traffic light in sight or it's not red anymore, get back to speed
                    self.state = 0

            # rospy.logwarn('[WU] Next wp: {}, Traffic wp: {}, State: {}, Vel: {:0.1f}'.format(next_wp, self.waypoint_traffic, self.state, self.current_velocity))

            # State action, calculate next waypoints
            self.calculate_final_waypoints(next_wp)
            #self.print_final_waypoints(10)

            # Publish final waypoints
            self.publish_waypoints()

    """
    Calculate the final waypoints to follow
    """
    # TODO
    def calculate_final_waypoints(self, start_wp):

        # Empty output list
        self.waypoint_final = []

        if self.state == 0:
            for i in range(start_wp, start_wp + LOOKAHEAD_WPS):
                j = i % len(self.base_waypoints.waypoints)
                tmp = Waypoint()
                tmp.pose.pose = self.base_waypoints.waypoints[j].pose.pose
                tmp.twist.twist.linear.x = self.base_waypoints.waypoints[j].twist.twist.linear.x
                self.waypoint_final.append(tmp)

        elif self.state == 1:
            stop_bw = self.waypoint_traffic

            # Waypoints before the traffic light -> set pose/speed to base_waypoint's values
            for i in range(start_wp, stop_bw):
                j = i % len(self.base_waypoints.waypoints)
                tmp = Waypoint()
                tmp.pose.pose = self.base_waypoints.waypoints[j].pose.pose
                tmp.twist.twist.linear.x = self.base_waypoints.waypoints[j].twist.twist.linear.x
                self.waypoint_final.append(tmp)

            # Brake to target
            target_wp = len(self.waypoint_final)

            # Waypoints after the traffic light -> set pose to base_waypoint's pose and set speed to 0
            i_max = max(start_wp + LOOKAHEAD_WPS, stop_bw+1)
            for i in range(stop_bw, i_max):
                j = i % len(self.base_waypoints.waypoints)
                tmp = Waypoint()
                tmp.pose.pose = self.base_waypoints.waypoints[j].pose.pose
                tmp.twist.twist.linear.x  = 0.0
                self.waypoint_final.append(tmp)

            # Waypoints before the traffic light -> set their speed considering a specific braking acceleration
            last = self.waypoint_final[target_wp]
            last.twist.twist.linear.x = 0.0

            for wp in self.waypoint_final[:target_wp][::-1]:
                dist = self.distance_poses(wp.pose.pose.position, last.pose.pose.position)
                dist = max(0.0, dist-BUFFER_BREAK)
                vel  = math.sqrt(2*self.acceleration_break*dist)  # use maximum braking acceleration
                if vel < 1.0:
                    vel = 0.0
                wp.twist.twist.linear.x = min(vel, wp.twist.twist.linear.x)

    """
    Return the id (wp) of the waypoint closest to the pose
    """
    def get_closest_waypoint(self):
        dist = float('inf')
        dl = lambda a, b: math.sqrt((a.x-b.x)**2 + (a.y-b.y)**2  + (a.z-b.z)**2)
        wp = 0
        for i in range(len(self.base_waypoints.waypoints)):
            new_dist = dl(self.current_pose.pose.position, self.base_waypoints.waypoints[i].pose.pose.position)
            if new_dist < dist:
                dist = new_dist
                wp = i
        return wp

    """
    Returns the id (wp) of the waypoint immediately ahead of the current pose
    """
    def get_next_waypoint(self):
        next_wp = self.get_closest_waypoint()
        heading = math.atan2((self.base_waypoints.waypoints[next_wp].pose.pose.position.y - self.current_pose.pose.position.y), (self.base_waypoints.waypoints[next_wp].pose.pose.position.x - self.current_pose.pose.position.x))
        theta = tf.transformations.euler_from_quaternion([self.current_pose.pose.orientation.x,
                                                          self.current_pose.pose.orientation.y,
                                                          self.current_pose.pose.orientation.z,
                                                          self.current_pose.pose.orientation.w])[-1]
        angle = math.fabs(theta-heading)
        if angle > math.pi / 4.0:
            next_wp += 1
        return next_wp

    """
    Return distance between two waypoints
    """
    def distance_wp(self, waypoints, wp1, wp2):
        dist = 0
        dl = lambda a, b: math.sqrt((a.x-b.x)**2 + (a.y-b.y)**2 + (a.z-b.z)**2)
        for i in range(wp1, wp2+1):
            dist += dl(waypoints[wp1].pose.pose.position, waypoints[i].pose.pose.position)
            wp1 = i
        return dist

    """
    Return distance between to poses
    """
    def distance_poses(self, p1, p2):
        x = p1.x - p2.x
        y = p1.y - p2.y
        z = p1.z - p2.z
        return math.sqrt(x*x + y*y + z*z)

    """
    Publish waypoints on the appropriate topic
    """
    def publish_waypoints(self):
        msg = Lane()
        # msg.header = self.base_waypoints.header
        msg.header.stamp = rospy.Time().now()
        msg.header.frame_id = 'world'
        msg.waypoints = self.waypoint_final[:LOOKAHEAD_WPS]
        self.final_waypoints_pub.publish(msg)

    """
    Print the next N final waypoints
    """
    def print_final_waypoints(self, n):
        s = "[WU] Final: "
        for i in range(0,n):
            s = s + "  {:0.1f}".format(self.waypoint_final[i].twist.twist.linear.x)
        rospy.logwarn(s)

    """
    Get and set linear velocity for a single waypoint id (wp) in a list of waypoints
    Unused
    """
    def get_waypoint_velocity(self, waypoint):
        return waypoint.twist.twist.linear.x

    def set_waypoint_velocity(self, waypoints, waypoint, velocity):
        waypoints[waypoint].twist.twist.linear.x = velocity

    """
    Callbacks
    """
    def pose_cb(self, msg):
        # First thing first, get the current pose
        self.current_pose = msg
        #rospy.logwarn('{} New pose received'.format(rospy.Time().now()))

        # Trigger action
        if not TIME_TRIGGER:
            if self.decimator_i == self.decimator_n:
                self.decimator_i = 0
                self.action()
            else:
                self.decimator_i = self.decimator_i + 1

    def waypoints_cb(self, waypoints):
        # Storing waypoints given that they are published only once
        self.base_waypoints = waypoints
    	#rospy.logwarn('Waypoint msg received: {}'.format(self.base_waypoints))

    def traffic_cb(self, msg):
        # TODO: Callback for /traffic_waypoint message. Implement
        self.waypoint_traffic = msg.data
        #rospy.logwarn('Traffic msg received: {}'.format(self.waypoint_traffic))

    def obstacle_cb(self, msg):
        # TODO: Callback for /obstacle_waypoint message. We will implement it later
        self.obstacle_waypoint = msg.data
        # rospy.logwarn('Obstacle msg received: {}'.format(self.obstacle_waypoint))

    def current_velocity_cb(self, msg):
        # TODO: Callback for /obstacle_waypoint message. We will implement it later
        self.current_velocity = msg.twist.linear.x


if __name__ == '__main__':
    try:
        WaypointUpdater()
    except rospy.ROSInterruptException:
        rospy.logerr('Could not start waypoint updater node.')
