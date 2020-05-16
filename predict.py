from flask import Flask, request, jsonify, json

#LaneNet update
from tensorflow.keras.models import load_model
from skimage import transform
from skimage import exposure
import numpy as np
import cv2
import tensorflow as tf
import glog as log
from models.lane_detection_lanenet.lanenet_model import lanenet_postprocess, lanenet, global_config
import time

from models.lane_detection.binarization_utils import binarize
from models.lane_detection.calibration_utils import calibrate_camera, undistort
from models.lane_detection.globals import xm_per_pix, time_window
from models.lane_detection.line_utils import get_fits_by_sliding_windows, Line, get_fits_by_previous_fits
from models.lane_detection.perspective_utils import birdeye
from models.vehicle_detectionn.lane import *
from models.vehicle_detectionn.yolo_pipeline import *

CFG = global_config.cfg
net = lanenet.LaneNet(phase='test', net_flag='vgg', reuse=tf.AUTO_REUSE)
postprocessor = lanenet_postprocess.LaneNetPostProcessor()


def process_lane_recognition_model(image):

    weights_path="models/lane_detection_lanenet/downloaded_model/tusimple_lanenet_vgg.ckpt"

    # preprocess
    image_vis = image
    image = cv2.resize(image, (512, 256), interpolation=cv2.INTER_LINEAR)
    image = image / 127.5 - 1.0
    log.info('Image load complete')

    # model run
    tf.compat.v1.disable_eager_execution()
    input_tensor = tf.compat.v1.placeholder(dtype=tf.float32, shape=[1, 256, 512, 3], name='input_tensor')
    binary_seg_ret, instance_seg_ret = net.inference(input_tensor=input_tensor, name='lanenet_model')
    saver = tf.compat.v1.train.Saver()

    # Set sess configuration
    sess_config = tf.compat.v1.ConfigProto()
    sess_config.gpu_options.per_process_gpu_memory_fraction = CFG.TEST.GPU_MEMORY_FRACTION
    sess_config.gpu_options.allow_growth = CFG.TRAIN.TF_ALLOW_GROWTH
    sess_config.gpu_options.allocator_type = 'BFC'

    sess = tf.compat.v1.Session(config=sess_config)

    with sess.as_default():
        saver.restore(sess=sess, save_path=weights_path)
        binary_seg_image, instance_seg_image = sess.run(
            [binary_seg_ret, instance_seg_ret],
            feed_dict={input_tensor: [image]}
        )
        postprocess_result = postprocessor.postprocess(
            binary_seg_result=binary_seg_image[0],
            instance_seg_result=instance_seg_image[0],
            source_image=image_vis
        )
        mask_image = postprocess_result['mask_image']

        for i in range(CFG.TRAIN.EMBEDDING_FEATS_DIMS):
            instance_seg_image[0][:, :, i] = minmax_scale(instance_seg_image[0][:, :, i])
        embedding_image = np.array(instance_seg_image[0], np.uint8)
    lists = mask_image[:, :, (2, 1, 0)].tolist()
    json_str = json.dumps(lists)
    # encoded_img = base64.encodebytes(img_byte_arr.getvalue()).decode('ascii')
    sess.close()
    return json_str


def minmax_scale(input_arr):
    min_val = np.min(input_arr)
    max_val = np.max(input_arr)
    output_arr = (input_arr - min_val) * 255.0 / (max_val - min_val)

    return output_arr


def pipeline_yolo(img):

    #img_undist, img_lane_augmented, lane_info = lane_process(img)
    print("lane info %")
    # print( lane_info)
    img_undist =img
    img_lane_augmented = img
    lane_info={}
    output = vehicle_detection_yolo(img_undist, img_lane_augmented, lane_info)
    return output


processed_frames = 0                    # counter of frames processed (when processing video)
line_lt = Line(buffer_len=time_window)  # line on the left of the lane
line_rt = Line(buffer_len=time_window)  # line on the right of the lane
sign_recognition_model = None
sign_recognition_label_names = None
app = Flask(__name__)


def prepare_out_blend_frame(blend_on_road, img_binary, img_birdeye, img_fit, line_lt, line_rt, offset_meter):
    h, w = blend_on_road.shape[:2]

    thumb_ratio = 0.2
    thumb_h, thumb_w = int(thumb_ratio * h), int(thumb_ratio * w)

    off_x, off_y = 20, 15

    # add a gray rectangle to highlight the upper area
    mask = blend_on_road.copy()
    mask = cv2.rectangle(mask, pt1=(0, 0), pt2=(w, thumb_h+2*off_y), color=(0, 0, 0), thickness=cv2.FILLED)
    blend_on_road = cv2.addWeighted(src1=mask, alpha=0.2, src2=blend_on_road, beta=0.8, gamma=0)

    # add thumbnail of binary image
    thumb_binary = cv2.resize(img_binary, dsize=(thumb_w, thumb_h))
    thumb_binary = np.dstack([thumb_binary, thumb_binary, thumb_binary]) * 255
    blend_on_road[off_y:thumb_h+off_y, off_x:off_x+thumb_w, :] = thumb_binary

    # add thumbnail of bird's eye view
    thumb_birdeye = cv2.resize(img_birdeye, dsize=(thumb_w, thumb_h))
    thumb_birdeye = np.dstack([thumb_birdeye, thumb_birdeye, thumb_birdeye]) * 255
    blend_on_road[off_y:thumb_h+off_y, 2*off_x+thumb_w:2*(off_x+thumb_w), :] = thumb_birdeye

    # add thumbnail of bird's eye view (lane-line highlighted)
    thumb_img_fit = cv2.resize(img_fit, dsize=(thumb_w, thumb_h))
    blend_on_road[off_y:thumb_h+off_y, 3*off_x+2*thumb_w:3*(off_x+thumb_w), :] = thumb_img_fit

    # add text (curvature and offset info) on the upper right of the blend
    mean_curvature_meter = np.mean([line_lt.curvature_meter, line_rt.curvature_meter])
    font = cv2.FONT_HERSHEY_SIMPLEX
    cv2.putText(blend_on_road, 'Curvature radius: {:.02f}m'.format(mean_curvature_meter), (860, 60), font, 0.9, (255, 255, 255), 2, cv2.LINE_AA)
    cv2.putText(blend_on_road, 'Offset from center: {:.02f}m'.format(offset_meter), (860, 130), font, 0.9, (255, 255, 255), 2, cv2.LINE_AA)

    return blend_on_road


def compute_offset_from_center(line_lt, line_rt, frame_width):
    if line_lt.detected and line_rt.detected:
        line_lt_bottom = np.mean(line_lt.all_x[line_lt.all_y > 0.95 * line_lt.all_y.max()])
        line_rt_bottom = np.mean(line_rt.all_x[line_rt.all_y > 0.95 * line_rt.all_y.max()])
        lane_width = line_rt_bottom - line_lt_bottom
        midpoint = frame_width / 2
        offset_pix = abs((line_lt_bottom + lane_width / 2) - midpoint)
        offset_meter = xm_per_pix * offset_pix
    else:
        offset_meter = -1

    return offset_meter


def process_pipeline(frame, keep_state=True):
    global line_lt, line_rt, processed_frames

    # undistort the image using coefficients found in calibration
    img_undistorted = undistort(frame, mtx, dist, verbose=False)

    # binarize the frame s.t. lane lines are highlighted as much as possible
    img_binary = binarize(img_undistorted, verbose=False)

    # compute perspective transform to obtain bird's eye view
    img_birdeye, M, Minv = birdeye(img_binary, verbose=False)

    # fit 2-degree polynomial curve onto lane lines found
    if processed_frames > 0 and keep_state and line_lt.detected and line_rt.detected:
        line_lt, line_rt, img_fit = get_fits_by_previous_fits(img_birdeye, line_lt, line_rt, verbose=False)
    else:
        line_lt, line_rt, img_fit = get_fits_by_sliding_windows(img_birdeye, line_lt, line_rt, n_windows=9, verbose=False)

    # compute offset in meter from center of the lane
    offset_meter = compute_offset_from_center(line_lt, line_rt, frame_width=frame.shape[1])
    processed_frames += 1

    return offset_meter


def vehicle_method(img):
    #print(img)
    #newsize = (1280, 720)
    #img = img.resize(newsize)
    #img = np.array(img)
    # (1) Yolo pipeline
    yolo_result = pipeline_yolo(img)
    return yolo_result


@app.route('/predict', methods=['POST'])
def get_prediction():
    vehicles =[]
    load_times = []
    for key in request.files:
        filestr = request.files[key].read()  # "file" key'i ile gonderilen resmi al
        npimg = np.frombuffer(filestr, np.uint8)
        image = cv2.imdecode(npimg, cv2.IMREAD_COLOR)
        load_time = process_lane_recognition_model(image)
        load_times.append('Lane: ' + str(load_time))
        # distance_from_center = process_pipeline(image, keep_state=False)
        # distance_from_center_arr.append(distance_from_center)
        vehicle = vehicle_method(image)
        vehicles.append(vehicle)
    device = tf.test.gpu_device_name()
    return jsonify(vehicles=json.dumps(str(vehicles)), time=json.dumps(device))


if __name__ == '__main__':
    ret, mtx, dist, rvecs, tvecs = calibrate_camera(calib_images_dir='camera_cal')

    app.run(debug=True, host='0.0.0.0', port=8000)