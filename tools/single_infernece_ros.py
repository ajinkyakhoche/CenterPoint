
import rospy
import ros_numpy
import tf
import numpy as np
import copy
import json
import os
import sys
import torch
import time
import argparse

from std_msgs.msg import Header
import sensor_msgs.point_cloud2 as pc2
from sensor_msgs.msg import PointCloud2, PointField
from jsk_recognition_msgs.msg import BoundingBox, BoundingBoxArray
from vision_msgs.msg import Detection3DArray, Detection3D, BoundingBox3D
from pyquaternion import Quaternion
from geometry_msgs.msg import TwistStamped

# from det3d import __version__, torchie
from det3d.models import build_detector
from det3d.torchie import Config
from det3d.core.input.voxel_generator import VoxelGenerator
from nusc_tracking.pub_tracker_ros import PubTracker as Tracker

def yaw2quaternion(yaw: float) -> Quaternion:
    return Quaternion(axis=[0,0,1], radians=yaw)

def get_annotations_indices(types, thresh, label_preds, scores):
    indexs = []
    annotation_indices = []
    for i in range(label_preds.shape[0]):
        if label_preds[i] == types:
            indexs.append(i)
    for index in indexs:
        if scores[index] >= thresh:
            annotation_indices.append(index)
    return annotation_indices


def remove_low_score_nu(image_anno, thresh):
    img_filtered_annotations = {}
    label_preds_ = image_anno["label_preds"].detach().cpu().numpy()
    scores_ = image_anno["scores"].detach().cpu().numpy()

    car_indices =                  get_annotations_indices(0, 0.4, label_preds_, scores_)
    truck_indices =                get_annotations_indices(1, 0.4, label_preds_, scores_)
    construction_vehicle_indices = get_annotations_indices(2, 0.4, label_preds_, scores_)
    bus_indices =                  get_annotations_indices(3, 0.3, label_preds_, scores_)
    trailer_indices =              get_annotations_indices(4, 0.4, label_preds_, scores_)
    barrier_indices =              get_annotations_indices(5, 0.4, label_preds_, scores_)
    motorcycle_indices =           get_annotations_indices(6, 0.15, label_preds_, scores_)
    bicycle_indices =              get_annotations_indices(7, 0.15, label_preds_, scores_)
    pedestrain_indices =           get_annotations_indices(8, 0.1, label_preds_, scores_)
    traffic_cone_indices =         get_annotations_indices(9, 0.1, label_preds_, scores_)

    for key in image_anno.keys():
        if key == 'metadata':
            continue
        img_filtered_annotations[key] = (
            image_anno[key][car_indices +
                            pedestrain_indices +
                            bicycle_indices +
                            bus_indices +
                            construction_vehicle_indices +
                            traffic_cone_indices +
                            trailer_indices +
                            barrier_indices +
                            truck_indices
                            ])

    return img_filtered_annotations


class Processor_ROS:
    def __init__(self, config_path, model_path):
        self.points = None
        self.config_path = config_path
        self.model_path = model_path
        self.device = None
        self.net = None
        self.voxel_generator = None
        self.inputs = None

    def initialize(self):
        self.read_config()

    def read_config(self):
        config_path = self.config_path
        cfg = Config.fromfile(self.config_path)
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.net = build_detector(cfg.model, train_cfg=None, test_cfg=cfg.test_cfg)
        self.net.load_state_dict(torch.load(self.model_path)["state_dict"])
        self.net = self.net.to(self.device).eval()

        self.range = cfg.voxel_generator.range
        self.voxel_size = cfg.voxel_generator.voxel_size
        self.max_points_in_voxel = cfg.voxel_generator.max_points_in_voxel
        self.max_voxel_num = cfg.voxel_generator.max_voxel_num
        if isinstance(self.max_voxel_num, list):
            self.max_voxel_num = self.max_voxel_num[0]
        self.voxel_generator = VoxelGenerator(
            voxel_size=self.voxel_size,
            point_cloud_range=self.range,
            max_num_points=self.max_points_in_voxel,
            max_voxels=self.max_voxel_num,
        )

    def run(self, points):
        t_t = time.time()
        print(f"input points shape: {points.shape}")
        num_features = 5
        self.points = points.reshape([-1, num_features])
        self.points[:, 4] = 0 # timestamp value

        voxels, coords, num_points = self.voxel_generator.generate(self.points)
        num_voxels = np.array([voxels.shape[0]], dtype=np.int64)
        grid_size = self.voxel_generator.grid_size
        coords = np.pad(coords, ((0, 0), (1, 0)), mode='constant', constant_values = 0)

        voxels = torch.tensor(voxels, dtype=torch.float32, device=self.device)
        coords = torch.tensor(coords, dtype=torch.int32, device=self.device)
        num_points = torch.tensor(num_points, dtype=torch.int32, device=self.device)
        num_voxels = torch.tensor(num_voxels, dtype=torch.int32, device=self.device)

        self.inputs = dict(
            voxels = voxels,
            num_points = num_points,
            num_voxels = num_voxels,
            coordinates = coords,
            shape = [grid_size]
        )
        torch.cuda.synchronize()
        t = time.time()

        with torch.no_grad():
            outputs = self.net(self.inputs, return_loss=False)[0]

        # print(f"output: {outputs}")

        torch.cuda.synchronize()
        print("  network predict time cost:", time.time() - t)

        outputs = remove_low_score_nu(outputs, 0.45)

        boxes_lidar = outputs["box3d_lidar"].detach().cpu().numpy()
        print("  predict boxes:", boxes_lidar.shape)

        scores = outputs["scores"].detach().cpu().numpy()
        types = outputs["label_preds"].detach().cpu().numpy()

        boxes_lidar[:, -1] = -boxes_lidar[:, -1] - np.pi / 2

        print(f"  total cost time: {time.time() - t_t}")

        return scores, boxes_lidar, types

def get_xyz_points(cloud_array, remove_nans=True, dtype=np.float):
    '''
    '''
    if remove_nans:
        mask = np.isfinite(cloud_array['x']) & np.isfinite(cloud_array['y']) & np.isfinite(cloud_array['z'])
        cloud_array = cloud_array[mask]

    points = np.zeros(cloud_array.shape + (5,), dtype=dtype)
    points[...,0] = cloud_array['x']
    points[...,1] = cloud_array['y']
    points[...,2] = cloud_array['z']
    return points

def xyz_array_to_pointcloud2(points_sum, stamp=None, frame_id=None):
    '''
    Create a sensor_msgs.PointCloud2 from an array of points.
    '''
    msg = PointCloud2()
    if stamp:
        msg.header.stamp = stamp
    if frame_id:
        msg.header.frame_id = frame_id
    msg.height = 1
    msg.width = points_sum.shape[0]
    msg.fields = [
        PointField('x', 0, PointField.FLOAT32, 1),
        PointField('y', 4, PointField.FLOAT32, 1),
        PointField('z', 8, PointField.FLOAT32, 1)
        # PointField('i', 12, PointField.FLOAT32, 1)
        ]
    msg.is_bigendian = False
    msg.point_step = 12
    msg.row_step = points_sum.shape[0]
    msg.is_dense = int(np.isfinite(points_sum).all())
    msg.data = np.asarray(points_sum, np.float32).tostring()
    return msg

def rslidar_callback(msg):
    t_t = time.time()
    arr_bbox = BoundingBoxArray()

    msg_cloud = ros_numpy.point_cloud2.pointcloud2_to_array(msg)
    np_p = get_xyz_points(msg_cloud, True)
    print("  ")
    scores, dt_box_lidar, types = proc_1.run(np_p)

    # (pos, rot) = listener.lookupTransform('map', msg.header.frame_id, rospy.Time(0))

    if scores.size != 0:
        for i in range(scores.size):
            bbox = BoundingBox()
            bbox.header.frame_id = msg.header.frame_id #'map'
            bbox.header.stamp = msg.header.stamp#rospy.Time.now()
            q = yaw2quaternion(float(dt_box_lidar[i][8]))
            bbox.pose.orientation.x = q[1]#* rot[0]
            bbox.pose.orientation.y = q[2]#* rot[1]
            bbox.pose.orientation.z = q[3]#* rot[2]
            bbox.pose.orientation.w = q[0]#* rot[3]
            bbox.pose.position.x = float(dt_box_lidar[i][0])# + pos[0])
            bbox.pose.position.y = float(dt_box_lidar[i][1])# + pos[1])
            bbox.pose.position.z = float(dt_box_lidar[i][2])# + pos[2])
            bbox.dimensions.x = float(dt_box_lidar[i][4])
            bbox.dimensions.y = float(dt_box_lidar[i][3])
            bbox.dimensions.z = float(dt_box_lidar[i][5])
            bbox.value = scores[i]
            bbox.label = int(types[i])
            if bbox.value>0.5:
                arr_bbox.boxes.append(bbox)
    print("total callback time: ", time.time() - t_t)
    arr_bbox.header.frame_id = msg.header.frame_id#'map'
    arr_bbox.header.stamp = msg.header.stamp
    if len(arr_bbox.boxes) is not 0:
        pub_arr_bbox.publish(arr_bbox)
        arr_bbox.boxes = []
    else:
        arr_bbox.boxes = []
        pub_arr_bbox.publish(arr_bbox)

    
def rstracker_callback(msg):
    t_t = time.time()
    arr_bbox = BoundingBoxArray()

    msg_cloud = ros_numpy.point_cloud2.pointcloud2_to_array(msg)
    np_p = get_xyz_points(msg_cloud, True)
    print("  ")
    scores, dt_box_lidar, types = proc_1.run(np_p)

    # (pos, rot) = listener.lookupTransform('map', msg.header.frame_id, rospy.Time(0))

    # if args.tracking:
    if tracker.last_time_stamp == 0:
        tracker.last_time_stamp = msg.header.stamp.to_sec()
    time_lag = (msg.header.stamp.to_sec() - tracker.last_time_stamp) 
    tracker.last_time_stamp = msg.header.stamp.to_sec()
    pred = []
        for i in range(scores.size):
        if scores[i] > 0.65:
            q = yaw2quaternion(float(dt_box_lidar[i][8]))
            pred.append(dict(
                translation=[dt_box_lidar[i][0], dt_box_lidar[i][1], dt_box_lidar[i][2]],
                rotation=[q[1], q[2], q[3], q[0]],
                size=[dt_box_lidar[i][4], dt_box_lidar[i][3], dt_box_lidar[i][5]],
                velocity=[dt_box_lidar[i][6], dt_box_lidar[i][7]],
                detection_name=proc_1.class_names[types[i]],
                detection_score=scores[i])
            )

    outputs = tracker.step_centertrack(pred, time_lag=time_lag)

    if len(outputs) != 0:
        for i in range(len(outputs)):
            bbox = BoundingBox()
            bbox.header.frame_id = msg.header.frame_id #'map'
            bbox.header.stamp = msg.header.stamp#rospy.Time.now()
            # q = yaw2quaternion(float(dt_box_lidar[i][8]))
            bbox.pose.orientation.x = outputs[i]['rotation'][0]
            bbox.pose.orientation.y = outputs[i]['rotation'][1]
            bbox.pose.orientation.z = outputs[i]['rotation'][2]
            bbox.pose.orientation.w = outputs[i]['rotation'][3]
            bbox.pose.position.x = outputs[i]['translation'][0]
            bbox.pose.position.y = outputs[i]['translation'][1]
            bbox.pose.position.z = outputs[i]['translation'][2]
            bbox.dimensions.x = outputs[i]['size'][0]
            bbox.dimensions.y = outputs[i]['size'][1]
            bbox.dimensions.z = outputs[i]['size'][2]
            bbox.value = outputs[i]['tracking_id']#scores[i]
            bbox.label = int(outputs[i]['label_preds'])
            # if bbox.value>0.25:
            arr_bbox.boxes.append(bbox)
    print("total callback time: ", time.time() - t_t)
    arr_bbox.header.frame_id = msg.header.frame_id#'map'
    arr_bbox.header.stamp = msg.header.stamp
    if len(arr_bbox.boxes) is not 0:
        pub_arr_bbox.publish(arr_bbox)
        arr_bbox.boxes = []
    else:
        arr_bbox.boxes = []
        pub_arr_bbox.publish(arr_bbox)

def parse_args():
    parser = argparse.ArgumentParser(description="Tracking Evaluation")
    parser.add_argument("--config_path", help="config path to NN", default="configs/nusc/voxelnet/nusc_centerpoint_voxelnet_0075voxel_fix_bn_z.py")
    parser.add_argument(
        "--model_path", help="path to NN weights", default="work_dirs/nusc_centerpoint_voxelnet_0075voxel_fix_bn_z/epoch_20.pth"
    )
    parser.add_argument("--hungarian", action='store_true')
    parser.add_argument("--max_age", type=int, default=10)
    parser.add_argument("--tracking", action='store_true', default=True)
    parser.add_argument("--global_frame", type=str, default='/Smooth')
    
    args = parser.parse_args()

    return args

if __name__ == "__main__":
    # global proc
    args = parse_args()
    print('Deploy OK')

    global proc
    ## CenterPoint
    # config_path = 'configs/nusc/voxelnet/nusc_centerpoint_voxelnet_0075voxel_fix_bn_z.py'#'configs/nusc/pp/nusc_centerpoint_pp_02voxel_two_pfn_10sweep.py'
    # model_path = 'work_dirs/nusc_centerpoint_voxelnet_0075voxel_fix_bn_z/epoch_20.pth'#'work_dirs/nusc_02_pp/latest.pth'

    proc_1 = Processor_ROS(args.config_path, args.model_path)

    proc_1.initialize()

    rospy.init_node('centerpoint_ros_node')
    sub_lidar_topic = [ "/velodyne_points",
                        "/top/rslidar_points",
                        "/points_raw",
                        "/lidar_protector/merged_cloud",
                        "/merged_cloud",
                        "/lidar_top",
                        "/roi_pclouds",
    # # global listener
    # listener = tf.TransformListener()

    if args.tracking:
        # initialize tracking
        tracker = Tracker(max_age=args.max_age, hungarian=args.hungarian)
        tracker.reset()

    sub_ = rospy.Subscriber(sub_lidar_topic[-2], PointCloud2, rstracker_callback, queue_size=1, buff_size=2**24)

    pub_arr_bbox = rospy.Publisher("pp_boxes", BoundingBoxArray, queue_size=1)
    # pub_arr_bbox = rospy.Publisher("pp_boxes", Detection3DArray, queue_size=1)

    print("[+] CenterPoint ros_node has started!")
    rospy.spin()
