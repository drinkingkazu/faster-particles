# *-* encoding: utf-8 *-*
# Generate toy dataset
# with labels = feature space points (track and shower start/end points)
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np
import matplotlib
matplotlib.use('Agg')
import sys
from track_generator import generate_toy_tracks
from shower_generator import make_shower

class ToydataGenerator(object):
    CLASSES = ('__background__', 'track_edge', 'shower_start', 'track_and_shower')

    def __init__(self, N, max_tracks, max_kinks,
                max_track_length=None, classification=False,
                seed=123, kinks=None, dtheta=-1, batch_size=20):
        self.N = N # shape of canvas

        # Track options
        self.max_tracks = max_tracks
        self.max_kinks = max_kinks
        self.max_track_length = max_track_length
        self.kinks = kinks

        # Shower options
        self.args_def = dict(
            nx = N,
            ny = N,
            nlines = 10,
            dtheta = dtheta,
            lmin = 40,
            lmax = 127,
            keep = 7,
            keep_prob = 0.6,
            nimages = 2,
            out_png = False,
        )
        self.gt_box_padding = 5
        self.batch_size = batch_size
        self.classification = classification
        if classification:
            self.max_tracks = 1
        np.random.seed(seed)

    def num_classes(self):
        return 3

    def forward(self):
        track_length = 0.0
        kinks = 0
        if self.classification:
            if self.kinks is None:
                if np.random.uniform() < 0.5:
                    output_showers, shower_start_points = np.zeros((self.N, self.N)), []
                    output_tracks, track_start_points, track_end_points = generate_toy_tracks(self.N, self.max_tracks,
                                                                                            max_kinks=self.max_kinks,
                                                                                            max_track_length=self.max_track_length,
                                                                                            padding=self.gt_box_padding)
                    # start and end are ill-defined without charge gradient
                    track_edges = track_start_points + track_end_points
                    image_label = 1 # Track image
                    kinks = len(track_start_points)
                    track_length = np.sqrt(np.power(track_start_points[0][0]-track_end_points[0][0], 2) + np.power(track_start_points[0][1] - track_end_points[0][1], 2))
                else:
                    output_showers, shower_start_points, angle = make_shower(self.args_def)
                    output_tracks = np.zeros((self.N, self.N))
                    track_edges = []
                    image_label = 2 # shower image
            else:
                output_showers, shower_start_points = np.zeros((self.N, self.N)), []
                output_tracks, track_start_points, track_end_points = generate_toy_tracks(self.N, self.max_tracks,
                                                                                        max_kinks=self.max_kinks,
                                                                                        max_track_length=self.max_track_length,
                                                                                        padding=self.gt_box_padding,
                                                                                        kinks=self.kinks)
                # start and end are ill-defined without charge gradient
                track_edges = track_start_points + track_end_points
                image_label = 1 # Track image
                kinks = len(track_start_points)
                track_length = np.sqrt(np.power(track_start_points[0][0]-track_end_points[0][0], 2) + np.power(track_start_points[0][1] - track_end_points[0][1], 2))

        else:
            output_showers, shower_start_points, angle = make_shower(self.args_def)
            output_tracks, track_start_points, track_end_points = generate_toy_tracks(self.N, self.max_tracks, max_kinks=self.max_kinks, max_track_length=self.max_track_length, padding=self.gt_box_padding)
            # start and end are ill-defined without charge gradient
            track_edges = track_start_points + track_end_points

        bbox_labels = []
        simple_labels = []
        gt_pixels = []

        # find bbox for shower
        # FIXME what happens if output_showers is empty ?
        if shower_start_points:
            bbox_labels.append([shower_start_points[0]-self.gt_box_padding,
                                shower_start_points[1]-self.gt_box_padding,
                                shower_start_points[0]+self.gt_box_padding,
                                shower_start_points[1]+self.gt_box_padding,
                                2]) # 2 for shower_start
            simple_labels.append([2])
            gt_pixels.append([shower_start_points[0], shower_start_points[1], 2])
            simple_label = 2
            opening_angle = angle

        # find bbox for tracks
        if track_edges:
            for i in range(len(track_edges)):
                bbox_labels.append([track_edges[i][0]-self.gt_box_padding,
                                    track_edges[i][1]-self.gt_box_padding,
                                    track_edges[i][0]+self.gt_box_padding,
                                    track_edges[i][1]+self.gt_box_padding,
                                    1 # 1 for track_edge
                             ])
                simple_labels.append([1])
                gt_pixels.append([track_edges[i][0], track_edges[i][1], 1])
            simple_label = 1
            opening_angle = None

        output = np.maximum(output_showers, output_tracks).reshape([1, self.N, self.N, 1])

        #output = output[np.newaxis,:,:,np.newaxis]
        output = np.repeat(output, 3, axis=3) # FIXME VGG needs RGB channels?

        blob = {}
        #img = np.concatenate([img,img,img],axis=3)
        blob['data'] = output.astype(np.float32)
        blob['im_info'] = [1, self.N, self.N, 3]
        blob['gt_boxes'] = np.array(bbox_labels)
        # Ji Won
        blob['class_labels'] = np.array([[simple_label]])
        blob['angles'] = np.array([[opening_angle]])
        # Laura
        blob['gt_labels'] = np.array(simple_labels)
        blob['gt_pixels'] = np.array(gt_pixels)
        if self.classification:
            blob['image_label'] = np.array([[image_label]])
        if self.classification and image_label == 1:
            blob['track_length'] = track_length
            blob['kinks'] = kinks

        return blob

    def fetch_batch(self):
        batch_blob = [self.forward() for i in range(self.batch_size)]
        batch_data = np.concatenate([d['data'] for d in batch_blob], axis=0)
        batch_labels = np.concatenate([d['class_labels'] for d in batch_blob], axis=0).reshape(-1)
        batch_angles = np.concatenate([d['angles'] for d in batch_blob], axis=0).reshape(-1)

        blob = {}
        blob['data'] = batch_data
        blob['class_labels'] = batch_labels
        blob['angles'] = batch_angles
        return blob

if __name__ == '__main__':
    t = ToydataGenerator(256, 3, 1, batch_size=20, classification=False)
    blobdict = t.forward()
    print(blobdict['gt_boxes'])
    print(blobdict['data'].shape)
    print(blobdict['class_labels'].shape)
    print("gt pixels shape ", blobdict['gt_pixels'].shape)

    #b = t.fetch_batch()
    #print(b['data'].shape)
    #print(b['class_labels'].shape)
    #print(b['angles'].shape)
