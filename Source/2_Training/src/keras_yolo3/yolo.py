# -*- coding: utf-8 -*-
"""
Class definition of YOLO_v3 style detection model on image and video
"""

import colorsys
import os
from timeit import default_timer as timer

import numpy as np
from keras.models import load_model
from keras.layers import Input
from PIL import Image, ImageFont, ImageDraw

from .yolo3.model import yolo_eval, yolo_body, tiny_yolo_body
from .yolo3.utils import letterbox_image
import os
from keras.utils import multi_gpu_model
import tensorflow.compat.v1 as tf
import tensorflow.python.keras.backend as K

import cv2
import math

tf.disable_eager_execution()


class YOLO(object):
    _defaults = {
        "model_path": "model_data/yolo.h5",
        "anchors_path": "model_data/yolo_anchors.txt",
        "classes_path": "model_data/coco_classes.txt",
        "score": 0.3,
        "iou": 0.45,
        "model_image_size": (416, 416),
        "gpu_num": 1,
    }

    HUMAN_HEIGHT = 1.7  # meters

    @classmethod
    def get_defaults(cls, n):
        if n in cls._defaults:
            return cls._defaults[n]
        else:
            return "Unrecognized attribute name '" + n + "'"

    def __init__(self, **kwargs):
        self.__dict__.update(self._defaults)  # set up default values
        self.__dict__.update(kwargs)  # and update with user overrides
        self.class_names = self._get_class()
        self.anchors = self._get_anchors()
        self.sess = K.get_session()
        self.boxes, self.scores, self.classes = self.generate()

    def _get_class(self):
        classes_path = os.path.expanduser(self.classes_path)
        with open(classes_path) as f:
            class_names = f.readlines()
        class_names = [c.strip() for c in class_names]
        return class_names

    def _get_anchors(self):
        anchors_path = os.path.expanduser(self.anchors_path)
        with open(anchors_path) as f:
            anchors = f.readline()
        anchors = [float(x) for x in anchors.split(",")]
        return np.array(anchors).reshape(-1, 2)

    def generate(self):
        model_path = os.path.expanduser(self.model_path)
        assert model_path.endswith(
            ".h5"), "Keras model or weights must be a .h5 file."

        # Load model, or construct model and load weights.
        start = timer()
        num_anchors = len(self.anchors)
        num_classes = len(self.class_names)
        is_tiny_version = num_anchors == 6  # default setting
        try:
            self.yolo_model = load_model(model_path, compile=False)
        except:
            self.yolo_model = (
                tiny_yolo_body(
                    Input(shape=(None, None, 3)), num_anchors // 2, num_classes
                )
                if is_tiny_version
                else yolo_body(
                    Input(shape=(None, None, 3)), num_anchors // 3, num_classes
                )
            )
            self.yolo_model.load_weights(
                self.model_path
            )  # make sure model, anchors and classes match
        else:
            assert self.yolo_model.layers[-1].output_shape[-1] == num_anchors / len(
                self.yolo_model.output
            ) * (
                num_classes + 5
            ), "Mismatch between model and given anchor and class sizes"

        end = timer()
        print(
            "{} model, anchors, and classes loaded in {:.2f}sec.".format(
                model_path, end - start
            )
        )

        # Generate colors for drawing bounding boxes. @TODO
        if len(self.class_names) == 1:
            self.colors = ["GreenYellow"]
        else:
            hsv_tuples = [
                (x / len(self.class_names), 1.0, 1.0)
                for x in range(len(self.class_names))
            ]
            self.colors = list(
                map(lambda x: colorsys.hsv_to_rgb(*x), hsv_tuples))
            self.colors = list(
                map(
                    lambda x: (int(x[0] * 255),
                               int(x[1] * 255), int(x[2] * 255)),
                    self.colors,
                )
            )
            # Fixed seed for consistent colors across runs.
            np.random.seed(10101)
            np.random.shuffle(
                self.colors
            )  # Shuffle colors to decorrelate adjacent classes.
            np.random.seed(None)  # Reset seed to default.

        # Generate output tensor targets for filtered bounding boxes.
        self.input_image_shape = K.placeholder(shape=(2,))
        if self.gpu_num >= 2:
            self.yolo_model = multi_gpu_model(
                self.yolo_model, gpus=self.gpu_num)
        boxes, scores, classes = yolo_eval(
            self.yolo_model.output,
            self.anchors,
            len(self.class_names),
            self.input_image_shape,
            score_threshold=self.score,
            iou_threshold=self.iou,
        )
        return boxes, scores, classes

    def get_dist_from_cam(self, box, calibr_param):
        # see reference: https://www.pyimagesearch.com/2015/01/19/find-distance-camera-objectmarker-using-python-opencv/
        # consider average human height to be 1.7m

        top, left, bottom, right = box
        P = abs(top-bottom)
        D = (self.HUMAN_HEIGHT*calibr_param)/P

        return D  # in meters

    def get_horizontal_dist(self, box_a, box_b, calibr_param, image):
        # x-axis distance between objects

        top_a, left_a, bottom_a, right_a = box_a
        top_b, left_b, bottom_b, right_b = box_b

        center_a = self.get_center(box_a, image)
        center_b = self.get_center(box_b, image)

        h_a = abs(top_a-bottom_a)  # height a in pixels

        return abs(center_a[0]-center_b[0])*self.HUMAN_HEIGHT/h_a  # in meters

    def get_dist_between_obj(self, box_a, box_b, calibr_param, image):

        # horizontal distance between objects
        h = self.get_horizontal_dist(box_a, box_b, calibr_param, image)
        # distance of each object from camera
        d_a = self.get_dist_from_cam(box_a, calibr_param)
        d_b = self.get_dist_from_cam(box_b, calibr_param)

        # return distance between objects using the Pythagorean Theorem
        return math.sqrt((d_b-d_a)**2 + (h)**2)  # in meters

    def get_center(self, box, image):
        top, left, bottom, right = box
        top = max(0, np.floor(top + 0.5).astype("int32"))
        left = max(0, np.floor(left + 0.5).astype("int32"))
        bottom = min(image.size[1], np.floor(bottom + 0.5).astype("int32"))
        right = min(image.size[0], np.floor(right + 0.5).astype("int32"))

        return [(left + right)/2, (top + bottom)/2]

    def check(self, box_a, box_b, calibr_param, image):
        # Returns if the calibrated euclidean distance between the given objects
        # is below a threshold
        dist = self.get_dist_between_obj(box_a, box_b, calibr_param, image)
        if 0 < dist < 2:
            return True
        else:
            return False

    def get_colors(self, boxes, image, calibr_param):
        # Sets colors of boxes based on the calibrated euclidean distance
        # between the objects
        pairs = set()
        box_colors = ['GreenYellow'] * len(boxes)  # initialize with green
        for i in range(len(boxes)):
            for j in range(len(boxes)):
                center_i = self.get_center(boxes[i], image)
                center_j = self.get_center(boxes[j], image)

                if self.check(boxes[i], boxes[j], calibr_param, image):
                    box_colors[i] = 'Red'
                    box_colors[j] = 'Red'

                    pairs.add((tuple(center_i), tuple(center_j)))

        return box_colors, pairs

    def detect_image(self, image, show_stats=True, calibr_param=0.25):
        start = timer()

        if self.model_image_size != (None, None):
            assert self.model_image_size[0] % 32 == 0, "Multiples of 32 required"
            assert self.model_image_size[1] % 32 == 0, "Multiples of 32 required"
            boxed_image = letterbox_image(
                image, tuple(reversed(self.model_image_size)))
        else:
            new_image_size = (
                image.width - (image.width % 32),
                image.height - (image.height % 32),
            )
            boxed_image = letterbox_image(image, new_image_size)
        image_data = np.array(boxed_image, dtype="float32")
        if show_stats:
            print(image_data.shape)
        image_data /= 255.0
        image_data = np.expand_dims(image_data, 0)  # Add batch dimension.

        out_boxes, out_scores, out_classes = self.sess.run(
            [self.boxes, self.scores, self.classes],
            feed_dict={
                self.yolo_model.input: image_data,
                self.input_image_shape: [image.size[1], image.size[0]],
                K.learning_phase(): 0,
            },
        )
        if show_stats:
            print("Found {} boxes for {}".format(len(out_boxes), "img"))
        out_prediction = []

        font_path = os.path.join(os.path.dirname(
            __file__), "font/FiraMono-Medium.otf")
        font = ImageFont.truetype(
            font=font_path, size=np.floor(
                3e-2 * image.size[1] + 0.5).astype("int32")
        )
        thickness = (image.size[0] + image.size[1]) // 300

        # Find close pairs
        box_colors, pairs = self.get_colors(out_boxes, image, calibr_param)
        for p in pairs:
            draw = ImageDraw.Draw(image)
            p1, p2 = p
            draw.line((p1[0], p1[1], p2[0], p2[1]), fill='Red', width=5)

        for i, c in reversed(list(enumerate(out_classes))):
            predicted_class = self.class_names[c]
            box = out_boxes[i]
            score = out_scores[i]
            box_color = box_colors[i]

            label = "{} {:.2f}".format(predicted_class, score)
            draw = ImageDraw.Draw(image)
            label_size = draw.textsize(label, font)

            top, left, bottom, right = box

            top = max(0, np.floor(top + 0.5).astype("int32"))
            left = max(0, np.floor(left + 0.5).astype("int32"))
            bottom = min(image.size[1], np.floor(bottom + 0.5).astype("int32"))
            right = min(image.size[0], np.floor(right + 0.5).astype("int32"))

            # image was expanded to model_image_size: make sure it did not pick
            # up any box outside of original image (run into this bug when
            # lowering confidence threshold to 0.01)
            if top > image.size[1] or right > image.size[0]:
                continue
            if show_stats:
                print(label, (left, top), (right, bottom))

            # output as xmin, ymin, xmax, ymax, class_index, confidence
            out_prediction.append([left, top, right, bottom, c, score])

            if top - label_size[1] >= 0:
                text_origin = np.array([left, top - label_size[1]])
            else:
                text_origin = np.array([left, bottom])

            # My kingdom for a good redistributable image drawing library.
            for i in range(thickness):
                draw.rectangle(
                    [left + i, top + i, right - i, bottom - i], outline=box_color
                )
            draw.rectangle(
                [tuple(text_origin), tuple(text_origin + label_size)],
                fill=box_color,
            )

            draw.text(text_origin, label, fill=(0, 0, 0), font=font)
            del draw

        end = timer()
        if show_stats:
            print("Time spent: {:.3f}sec".format(end - start))

        return out_prediction, image

    def close_session(self):
        self.sess.close()


def detect_video(yolo, video_path, output_path="", calibr_param=0.25):
    import cv2
    import pandas as pd

    # Make a dataframe for the prediction outputs
    out_df = pd.DataFrame(
        columns=[
            "frame",
            "xmin",
            "ymin",
            "xmax",
            "ymax",
            "label",
            "confidence",
            "x_size",
            "y_size",
        ]
    )

    vid = cv2.VideoCapture(video_path)
    if not vid.isOpened():
        raise IOError("Couldn't open webcam or video")
    # int(vid.get(cv2.CAP_PROP_FOURCC))
    video_FourCC = cv2.VideoWriter_fourcc(*"mp4v")
    video_fps = vid.get(cv2.CAP_PROP_FPS)
    video_size = (
        int(vid.get(cv2.CAP_PROP_FRAME_WIDTH)),
        int(vid.get(cv2.CAP_PROP_FRAME_HEIGHT)),
    )
    isOutput = True if output_path != "" else False
    if isOutput:
        print(
            "Processing {} with frame size {} at {:.1f} FPS".format(
                os.path.basename(video_path), video_size, video_fps
            )
        )
        # print("!!! TYPE:", type(output_path), type(video_FourCC), type(video_fps), type(video_size))
        out = cv2.VideoWriter(output_path, video_FourCC, video_fps, video_size)
    accum_time = 0
    curr_fps = 0
    fps = "FPS: ??"
    prev_time = timer()
    frame_count = 0
    while vid.isOpened():
        return_value, frame = vid.read()
        if not return_value:
            break
        # opencv images are BGR, translate to RGB
        frame = frame[:, :, ::-1]
        image = Image.fromarray(frame)
        out_pred, image = yolo.detect_image(image, calibr_param=calibr_param,
                                            show_stats=False)
        y_size, x_size, _ = np.array(image).shape
        for single_prediction in out_pred:
            out_df = out_df.append(
                pd.DataFrame(
                    [
                        [
                            frame_count
                        ]
                        + single_prediction
                        + [x_size, y_size]
                    ],
                    columns=[
                        "frame",
                        "xmin",
                        "ymin",
                        "xmax",
                        "ymax",
                        "label",
                        "confidence",
                        "x_size",
                        "y_size",
                    ],
                )
            )
        frame_count += 1
        result = np.asarray(image)
        curr_time = timer()
        exec_time = curr_time - prev_time
        prev_time = curr_time
        accum_time = accum_time + exec_time
        curr_fps = curr_fps + 1
        if accum_time > 1:
            accum_time = accum_time - 1
            fps = "FPS: " + str(curr_fps)
            curr_fps = 0
        cv2.putText(
            result,
            text=fps,
            org=(3, 15),
            fontFace=cv2.FONT_HERSHEY_SIMPLEX,
            fontScale=0.50,
            color=(255, 0, 0),
            thickness=2,
        )
        cv2.namedWindow("result", cv2.WINDOW_NORMAL)
        cv2.imshow("result", result[:, :, ::-1])
        if isOutput:
            out.write(result[:, :, ::-1])
        if cv2.waitKey(1) & 0xFF == ord('q'):
            exit(0)

    out_df.to_csv('Video_Prediction_Results_' +
                  video_path.split('/')[-1] + '.csv', index=False)
    vid.release()
    out.release()
    # yolo.close_session()


# TO PROCESS VIDEOS DIRECTLY FROM WEBCAM
def detect_webcam(yolo):
    import cv2

    vid = cv2.VideoCapture(0)
    if not vid.isOpened():
        raise IOError("Couldn't open webcam")
    accum_time = 0
    curr_fps = 0
    fps = "FPS: ??"
    prev_time = timer()
    while vid.isOpened():
        return_value, frame = vid.read()
        if not return_value:
            break
        image = Image.fromarray(frame)
        out_pred, image = yolo.detect_image(image, show_stats=False)
        result = np.asarray(image)
        curr_time = timer()
        exec_time = curr_time - prev_time
        prev_time = curr_time
        accum_time = accum_time + exec_time
        curr_fps = curr_fps + 1
        if accum_time > 1:
            accum_time = accum_time - 1
            fps = "FPS: " + str(curr_fps)
            curr_fps = 0
        cv2.putText(
            result,
            text=fps,
            org=(3, 15),
            fontFace=cv2.FONT_HERSHEY_SIMPLEX,
            fontScale=0.50,
            color=(255, 0, 0),
            thickness=2,
        )
        cv2.namedWindow("Result", cv2.WINDOW_NORMAL)
        cv2.imshow("Result", result)
        cv2.waitKey(1)
        if cv2.getWindowProperty("Result", cv2.WND_PROP_VISIBLE) < 1:
            break
    vid.release()
    yolo.close_session()
