import math
import warnings
warnings.filterwarnings("ignore")

import os
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

#---------------------------

import numpy as np
import tensorflow as tf
import cv2

from retinaface.model import retinaface_model
from retinaface.commons import preprocess, postprocess

#---------------------------

import tensorflow as tf
tf_version = int(tf.__version__.split(".")[0])

if tf_version == 2:
    import logging
    tf.get_logger().setLevel(logging.ERROR)

#---------------------------
from tensorflow.python.client import device_lib


def get_available_gpus():
    local_device_protos = device_lib.list_local_devices()
    return [x.name for x in local_device_protos if x.device_type == 'GPU']
num_gpus = len(get_available_gpus())

strategy = tf.distribute.MirroredStrategy()
print('Number of devices: {}'.format(strategy.num_replicas_in_sync))
#---------------------------
def build_model():
    
    global model #singleton design pattern
    
    if not "model" in globals():
        
        model = tf.function(
            retinaface_model.build_model(),
            input_signature=(tf.TensorSpec(shape=[None, None, None, 3], dtype=np.float32),)
        )

    return model

def get_image(img_path):
    if type(img_path) == str:  # Load from file path
        if not os.path.isfile(img_path):
            raise ValueError("Input image file path (", img_path, ") does not exist.")
        img = cv2.imread(img_path)

    elif isinstance(img_path, np.ndarray):  # Use given NumPy array
        img = img_path.copy()

    else:
        raise ValueError("Invalid image input. Only file paths or a NumPy array accepted.")

    # Validate image shape
    if len(img.shape) != 3 or np.prod(img.shape) == 0:
        raise ValueError("Input image needs to have 3 channels at must not be empty.")

    return img

def detect_faces(img_path, threshold=0.9, model = None, allow_upscaling = True):
    resp = {}

    img = get_image(img_path)

    #---------------------------

    if model is None:
        model = build_model()

    #---------------------------

    nms_threshold = 0.4; decay4=0.5

    _feat_stride_fpn = [32, 16, 8]

    _anchors_fpn = {
        'stride32': np.array([[-248., -248.,  263.,  263.], [-120., -120.,  135.,  135.]], dtype=np.float32),
        'stride16': np.array([[-56., -56.,  71.,  71.], [-24., -24.,  39.,  39.]], dtype=np.float32),
        'stride8': np.array([[-8., -8., 23., 23.], [ 0.,  0., 15., 15.]], dtype=np.float32)
    }

    _num_anchors = {'stride32': 2, 'stride16': 2, 'stride8': 2}

    #---------------------------

    proposals_list = []
    scores_list = []
    landmarks_list = []
    im_tensor, im_info, im_scale = preprocess.preprocess_image(img, allow_upscaling)
    net_out = model(im_tensor)
    net_out = [elt.numpy() for elt in net_out]
    sym_idx = 0

    for _idx, s in enumerate(_feat_stride_fpn):
        _key = 'stride%s'%s
        scores = net_out[sym_idx]
        scores = scores[:, :, :, _num_anchors['stride%s'%s]:]

        bbox_deltas = net_out[sym_idx + 1]
        height, width = bbox_deltas.shape[1], bbox_deltas.shape[2]

        A = _num_anchors['stride%s'%s]
        K = height * width
        anchors_fpn = _anchors_fpn['stride%s'%s]
        anchors = postprocess.anchors_plane(height, width, s, anchors_fpn)
        anchors = anchors.reshape((K * A, 4))
        scores = scores.reshape((-1, 1))

        bbox_stds = [1.0, 1.0, 1.0, 1.0]
        bbox_deltas = bbox_deltas
        bbox_pred_len = bbox_deltas.shape[3]//A
        bbox_deltas = bbox_deltas.reshape((-1, bbox_pred_len))
        bbox_deltas[:, 0::4] = bbox_deltas[:,0::4] * bbox_stds[0]
        bbox_deltas[:, 1::4] = bbox_deltas[:,1::4] * bbox_stds[1]
        bbox_deltas[:, 2::4] = bbox_deltas[:,2::4] * bbox_stds[2]
        bbox_deltas[:, 3::4] = bbox_deltas[:,3::4] * bbox_stds[3]
        proposals = postprocess.bbox_pred(anchors, bbox_deltas)

        proposals = postprocess.clip_boxes(proposals, im_info[:2])

        if s==4 and decay4<1.0:
            scores *= decay4

        scores_ravel = scores.ravel()
        order = np.where(scores_ravel>=threshold)[0]
        proposals = proposals[order, :]
        scores = scores[order]

        proposals[:, 0:4] /= im_scale
        proposals_list.append(proposals)
        scores_list.append(scores)

        landmark_deltas = net_out[sym_idx + 2]
        landmark_pred_len = landmark_deltas.shape[3]//A
        landmark_deltas = landmark_deltas.reshape((-1, 5, landmark_pred_len//5))
        landmarks = postprocess.landmark_pred(anchors, landmark_deltas)
        landmarks = landmarks[order, :]

        landmarks[:, :, 0:2] /= im_scale
        landmarks_list.append(landmarks)
        sym_idx += 3

    proposals = np.vstack(proposals_list)
    
    if proposals.shape[0]==0:
        return resp

    scores = np.vstack(scores_list)
    scores_ravel = scores.ravel()
    order = scores_ravel.argsort()[::-1]

    proposals = proposals[order, :]
    scores = scores[order]
    landmarks = np.vstack(landmarks_list)
    landmarks = landmarks[order].astype(np.float32, copy=False)

    pre_det = np.hstack((proposals[:,0:4], scores)).astype(np.float32, copy=False)

    #nms = cpu_nms_wrapper(nms_threshold)
    #keep = nms(pre_det)
    keep = postprocess.cpu_nms(pre_det, nms_threshold)

    det = np.hstack( (pre_det, proposals[:,4:]) )
    det = det[keep, :]
    landmarks = landmarks[keep]

    for idx, face in enumerate(det):

        label = 'face_'+str(idx+1)
        resp[label] = {}
        resp[label]["score"] = face[4]

        resp[label]["facial_area"] = list(face[0:4].astype(int))

        resp[label]["landmarks"] = {}
        resp[label]["landmarks"]["right_eye"] = list(landmarks[idx][0])
        resp[label]["landmarks"]["left_eye"] = list(landmarks[idx][1])
        resp[label]["landmarks"]["nose"] = list(landmarks[idx][2])
        resp[label]["landmarks"]["mouth_right"] = list(landmarks[idx][3])
        resp[label]["landmarks"]["mouth_left"] = list(landmarks[idx][4])

    return resp


def detect_batch_faces(numpy_rgb_images, threshold=0.9, model=None):
    # ---------------------------
    # buiding a model inside the strategy scope
    if num_gpus > 1:
        with strategy.scope():
            if model is None:
                model = build_model()
    else:
        if model is None:
            model = build_model()
    # ---------------------------

    nms_threshold = 0.4
    decay4 = 0.5

    _feat_stride_fpn = [32, 16, 8]

    _anchors_fpn = {
        'stride32': np.array([[-248., -248., 263., 263.], [-120., -120., 135., 135.]], dtype=np.float32),
        'stride16': np.array([[-56., -56., 71., 71.], [-24., -24., 39., 39.]], dtype=np.float32),
        'stride8': np.array([[-8., -8., 23., 23.], [0., 0., 15., 15.]], dtype=np.float32)
    }

    _num_anchors = {'stride32': 2, 'stride16': 2, 'stride8': 2}

    scales = [1024, 1980]
    batch_resp = []
    # ---------------------------
    images, img_scales = preprocess.preprocess_batch_images(numpy_rgb_images)

    batch_net_out = model(images)
    batch_net_out = [elt.numpy() for elt in batch_net_out]
    for i in range(batch_net_out[0].shape[0]):
        proposals_list = []
        scores_list = []
        landmarks_list = []
        sym_idx = 0
        for s in _feat_stride_fpn:
            scores = batch_net_out[sym_idx][i:i+1]
            A = _num_anchors['stride%s' % s]
            scores = scores[:, :, :, A:]
            scores_ravel = scores.ravel()
            order = np.where(scores_ravel >= threshold)[0]
            scores = scores_ravel[order]

            if scores.shape[0] == 0:
                sym_idx += 3
                continue

            bbox_deltas = batch_net_out[sym_idx + 1][i:i+1]
            height, width = bbox_deltas.shape[1], bbox_deltas.shape[2]
            bbox_pred_len = bbox_deltas.shape[3] // A
            bbox_deltas = bbox_deltas.reshape((-1, bbox_pred_len))

            landmark_deltas = batch_net_out[sym_idx + 2][i:i+1]
            landmark_pred_len = landmark_deltas.shape[3] // A
            landmark_deltas = landmark_deltas.reshape((-1, 5, landmark_pred_len // 5))

            anchors_fpn = _anchors_fpn['stride%s' % s]
            proposals = []
            landmarks = []
            for index in order:
                cell_y = index // (width * A)
                cell_x = (index - cell_y * width * A) // A
                n = index % A
                anchor = np.array([cell_x, cell_y, cell_x, cell_y]) * s + anchors_fpn[n]
                w = anchor[2] - anchor[0] + 1.0
                h = anchor[3] - anchor[1] + 1.0
                anchor_c = np.array([anchor[0] + 0.5 * (w - 1.0), anchor[1] + 0.5 * (h - 1.0)])
                dx, dy, ln_dw, ln_dh = bbox_deltas[index]
                pred_w = 0.5 * math.exp(ln_dw) * w
                pred_h = 0.5 * math.exp(ln_dh) * h
                pred_c = [anchor_c[0] + dx * w, anchor_c[1] + dy * h]
                max_x = numpy_rgb_images[i].shape[1] * img_scales[i]
                max_y = numpy_rgb_images[i].shape[0] * img_scales[i]
                pred_box = np.array([max(0, pred_c[0] - pred_w), max(0, pred_c[1] - pred_h),
                                     min(max_x, pred_c[0] + pred_w), min(max_y, pred_c[1] + pred_h)])
                proposals.append(pred_box / img_scales[i])

                landmark = landmark_deltas[index]
                for l in range(5):
                    landmark[l, 0] = landmark[l, 0] * w + anchor_c[0]
                    landmark[l, 1] = landmark[l, 1] * h + anchor_c[1]
                landmarks.append(landmark / img_scales[i])

            proposals_list.append(np.array(proposals))
            scores_list.append(scores)
            landmarks_list.append(np.array(landmarks))
            sym_idx += 3

        if len(scores_list) == 0:
            batch_resp.append({})
            continue

        scores = np.hstack(scores_list)
        scores_ravel = scores.ravel()
        order = scores_ravel.argsort()[::-1]

        proposals = np.vstack(proposals_list)
        proposals = proposals[order, :]
        scores = scores[order]
        landmarks = np.vstack(landmarks_list)
        landmarks = landmarks[order].astype(np.float32)

        pre_det = np.hstack((proposals, scores.reshape(-1,1))).astype(np.float32, copy=False)

        # nms = cpu_nms_wrapper(nms_threshold)
        # keep = nms(pre_det)
        keep = postprocess.cpu_nms(pre_det, nms_threshold)
        det = pre_det[keep, :]
        landmarks = landmarks[keep]

        resp = {}
        for idx, face in enumerate(det):
            label = 'face_' + str(idx + 1)
            resp[label] = {}
            resp[label]["score"] = face[4]

            resp[label]["facial_area"] = list(face[0:4].astype(int))

            resp[label]["landmarks"] = {}
            resp[label]["landmarks"]["right_eye"] = list(landmarks[idx][0])
            resp[label]["landmarks"]["left_eye"] = list(landmarks[idx][1])
            resp[label]["landmarks"]["nose"] = list(landmarks[idx][2])
            resp[label]["landmarks"]["mouth_right"] = list(landmarks[idx][3])
            resp[label]["landmarks"]["mouth_left"] = list(landmarks[idx][4])

        batch_resp.append(resp)
    return batch_resp


def extract_faces(img_path, threshold=0.9, model = None, align = True, allow_upscaling = True):

    resp = []

    #---------------------------

    img = get_image(img_path)

    #---------------------------

    obj = detect_faces(img_path = img, threshold = threshold, model = model, allow_upscaling = allow_upscaling)

    if type(obj) == dict:
        for key in obj:
            identity = obj[key]

            facial_area = identity["facial_area"]
            facial_img = img[facial_area[1]: facial_area[3], facial_area[0]: facial_area[2]]

            if align == True:
                landmarks = identity["landmarks"]
                left_eye = landmarks["left_eye"]
                right_eye = landmarks["right_eye"]
                nose = landmarks["nose"]
                mouth_right = landmarks["mouth_right"]
                mouth_left = landmarks["mouth_left"]

                facial_img = postprocess.alignment_procedure(facial_img, right_eye, left_eye, nose)

            resp.append(facial_img[:, :, ::-1])
    #elif type(obj) == tuple:

    return resp
