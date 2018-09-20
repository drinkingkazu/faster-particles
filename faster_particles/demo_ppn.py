# *-* encoding: utf-8 *-*
# Demo for PPN
# Usage: python demo_ppn.py model.ckpt
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np
import tensorflow as tf
from tensorflow.python.client import timeline
import os
import glob
import time

from faster_particles.display_utils import display, display_uresnet, \
                                            display_ppn_uresnet
from faster_particles.ppn import PPN
from faster_particles.base_net.uresnet import UResNet
from faster_particles.base_net import basenets
from faster_particles.metrics import PPNMetrics, UResNetMetrics
from faster_particles.data import ToydataGenerator, LarcvGenerator, \
                                HDF5Generator, CSVGenerator
from faster_particles.cropping import cropping_algorithms


def get_data(cfg):
    """
    Define data generators (toydata or LArCV)
    """
    if cfg.TEST_DATA == "":
        cfg.TEST_DATA = cfg.DATA
    if cfg.DATA_TYPE == 'toydata':
        train_data = ToydataGenerator(cfg)
        test_data = ToydataGenerator(cfg)
    elif cfg.DATA_TYPE == 'hdf5':
        train_data = HDF5Generator(cfg, filelist=cfg.DATA)
        test_data = HDF5Generator(cfg, filelist=cfg.TEST_DATA, is_testing=True)
    elif cfg.DATA_TYPE == 'csv':
        train_data = CSVGenerator(cfg, filelist=cfg.DATA)
        test_data = CSVGenerator(cfg, filelist=cfg.TEST_DATA)
    else:  # default is LArCV data
        train_data = LarcvGenerator(cfg, ioname="train",
                                    filelist=get_filelist(cfg.DATA))
        test_data = LarcvGenerator(cfg, ioname="test",
                                   filelist=get_filelist(cfg.TEST_DATA))
    return train_data, test_data


def load_weights(cfg, sess):
    print("Restoring checkpoint file...")
    scopes = []
    if cfg.WEIGHTS_FILE_PPN is not None:
        scopes.append((lambda x: True, cfg.WEIGHTS_FILE_PPN))
    # Restore variables for base net if given checkpoint file
    elif cfg.WEIGHTS_FILE_BASE is not None:
        if cfg.NET == 'ppn': # load only relevant layers of base network
		    scopes.append((lambda x: cfg.BASE_NET in x and "optimizer" not in x, cfg.WEIGHTS_FILE_BASE))
            #scopes.append((lambda x: cfg.BASE_NET in x, cfg.WEIGHTS_FILE_BASE))
        else: # load for full base network
            scopes.append((lambda x: cfg.BASE_NET in x, cfg.WEIGHTS_FILE_BASE))

    for scope, weights_file in scopes:
        print('Restoring %s...' % weights_file)
        variables_to_restore = [v for v in tf.global_variables() if scope(v.name)]
        print("- ignoring %d/%d variables" % (len(tf.global_variables()) - len(variables_to_restore), len(tf.global_variables())))
        if len(variables_to_restore) > 0:
            saver = tf.train.Saver(variables_to_restore)
            saver.restore(sess, weights_file)
        else:
            print("WARNING No variable was restored from weights file %s." % weights_file)
    print("Done.")


def get_filelist(ls_command):
    """
    Returns a list of files as a string *without spaces*
    e.g. "["file1.root","file2.root"]"
    """
    filelist = glob.glob(ls_command)
    for f in filelist:
        if not os.path.isfile(f):
            raise Exception("Datafile %s not found!" % f)
    return str(filelist).replace('\'', '\"').replace(" ", "")


def inference_simple(cfg, blobs, net, num_test=10):
    net.init_placeholders()
    net.create_architecture(is_training=False)
    inference = []
    with tf.Session() as sess:
        sess.run(tf.global_variables_initializer())
        load_weights(cfg, sess)
        for i in range(num_test):
            summary, results = net.test_image(sess, blobs[i])
            inference.append(results)
    return inference


def inference_full(cfg):
    # if cfg.WEIGHTS_FILE_BASE is None or cfg.WEIGHTS_FILE_PPN is None:
    #     raise Exception("Need both weights files for full inference.")

    num_test = cfg.MAX_STEPS
    inference_base, inference_ppn, blobs = [], [], []
    weights_file_ppn = cfg.WEIGHTS_FILE_PPN
    print("Retrieving data...")
    _, data = get_data(cfg)
    for i in range(num_test):
        blobs.append(data.forward())
    print("Done.")

    # First base
    print("Base network...")
    cfg.WEIGHTS_FILE_PPN = None
    net_base = basenets[cfg.BASE_NET](cfg=cfg)
    inference_base = inference_simple(cfg, blobs, net_base, num_test=num_test)
    print("Done.")
    print(inference_base)
    tf.reset_default_graph()
    print("PPN network...")
    cfg.WEIGHTS_FILE_PPN = weights_file_ppn
    net_ppn = PPN(cfg=cfg, base_net=basenets[cfg.BASE_NET])
    inference_ppn = inference_simple(cfg, blobs, net_ppn, num_test=num_test)
    print("Done.")
    print(inference_ppn)

    # Display
    print("Saving displays...")
    metrics = PPNMetrics(cfg, dim1=net_ppn.dim1, dim2=net_ppn.dim2)
    for i in range(num_test):
        # results = {**inference_base[i], **inference_ppn[i]}
        results = inference_base[i].copy()
        results.update(inference_ppn[i])
        display_ppn_uresnet(
            blobs[i],
            cfg,
            index=i,
            directory=os.path.join(cfg.DISPLAY_DIR, 'demo_full'),
            **results
        )
        metrics.add(blobs[i], results)
    metrics.plot()
    print("Done.")
    # Clustering: k-means? DBSCAN?


def crop_step(crops, coords0, coords1, data, N):
    j = tf.shape(crops)[0]-1
    j = tf.Print(j, [j])
    padding = []
    dim = coords0.get_shape()[1]
    for d in range(dim):
        pad = tf.maximum(N - (coords1[j, d] - coords0[j, d]), 0)
        if coords0[j, d] == 0.0:
            padding.append((pad, 0))
        else:
            padding.append((0, pad))
    new_crop = tf.pad(data[0, coords0[j, 0]:coords1[j, 0], coords0[j, 1]:coords1[j, 1], 0], padding, mode='constant')
    crops = tf.concat([crops, [new_crop]], axis=0)
    return crops, coords0, coords1, data, N


def crop_proposals(cfg, data, proposals):
    N = cfg.CROP_SIZE
    coords0 = tf.cast(tf.floor(proposals - N/2.0), tf.int32)
    coords1 = tf.cast(tf.floor(proposals + N/2.0), tf.int32)
    dim = 3 if cfg.DATA_3D else 2
    smear = tf.random_uniform((dim,), minval=-3, maxval=3, dtype=tf.int32)
    coords0 = tf.clip_by_value(coords0 + smear, 0, cfg.IMAGE_SIZE-1)
    coords1 = tf.clip_by_value(coords1 + smear, 0, cfg.IMAGE_SIZE-1)
    crops = tf.zeros((1, N, N))#tf.zeros((tf.shape(coords0)[0], N, N))
    results = tf.while_loop(lambda crops, coords0, *args: tf.shape(crops)[0] <= tf.shape(coords0)[0], crop_step, [crops, coords0, coords1, data, N], shape_invariants=[tf.TensorShape((None, N, N)), coords0.get_shape(), coords1.get_shape(), data.get_shape(), tf.TensorShape(())])
    crops = results[0][1:, :, :]
    return crops


def inference_ppn_ext(cfg):
    num_test = cfg.MAX_STEPS
    inference_ppn, blobs = [], []

    print("Retrieving data...")
    _, data = get_data(cfg)
    for i in range(num_test):
        blobs.append(data.forward())
    print("Done.")

    net_ppn = PPN(cfg=cfg, base_net=basenets[cfg.BASE_NET])
    net_ppn.init_placeholders()
    net_ppn.create_architecture(is_training=False)
    # FIXME better way to control the number of crops here?
    crops = crop_proposals(cfg, net_ppn.image_placeholder, net_ppn._predictions['im_proposals'])[:512]
    print(crops)
    # Cannot use tf.train.batch because the call to tf.train.start_queue_runners
    # requires image placeholder to be fed already
    #crops = tf.train.batch([crops], 1, shapes=[tf.TensorShape((cfg.CROP_SIZE, cfg.CROP_SIZE))], dynamic_pad=True, allow_smaller_final_batch=False, enqueue_many=True)
    net_uresnet = UResNet(cfg=cfg, N=cfg.CROP_SIZE)
    # FIXME remove dependency on labels at test time
    net_uresnet.init_placeholders(
        image=tf.reshape(crops, (-1, cfg.CROP_SIZE, cfg.CROP_SIZE, 1)),
        labels=tf.cast(tf.reshape(crops, (-1, cfg.CROP_SIZE, cfg.CROP_SIZE)),
                       dtype=tf.int32)
        )
    net_uresnet.create_architecture(is_training=False, scope='small_uresnet')
    inference = []
    with tf.Session() as sess:
        sess.run(tf.global_variables_initializer())
        sess.run(tf.local_variables_initializer())
        # Restore weights
        saver = tf.train.Saver([v for v in tf.global_variables() if 'small_uresnet' not in v.name])
        saver.restore(sess, cfg.WEIGHTS_FILE_PPN)
        saver2 = tf.train.Saver([v for v in tf.global_variables() if "small_uresnet" in v.name ])
        saver2.restore(sess, cfg.WEIGHTS_FILE_SMALL)

        for i in range(num_test):
            print(i)
            results = sess.run([
                net_ppn._predictions['im_proposals'],
                net_ppn._predictions['im_labels'],
                net_ppn._predictions['im_scores'],
                net_ppn._predictions['rois'],
                crops,
                net_uresnet._predictions,
                net_uresnet._scores
            ], feed_dict={net_ppn.image_placeholder: blobs[i]['data'], net_ppn.gt_pixels_placeholder: blobs[i]['gt_pixels']})
            print(results[5].shape)
            print(results[0].shape)
            print(results[4].shape)
            display(
                blobs[i],
                cfg,
                index=i,
                dim1=net_ppn.dim1,
                dim2=net_ppn.dim2,
                directory=os.path.join(cfg.DISPLAY_DIR, 'demo'),
                im_proposals = results[0],
                im_labels=results[1],
                im_scores=results[2],
                rois=results[3]
            )
            crops_np = results[4]
            N = cfg.IMAGE_SIZE
            cfg.IMAGE_SIZE = cfg.CROP_SIZE
            for j in range(len(crops_np)):
                print(j, crops_np[j].shape, results[5][j].shape)
                blob_j = {'data': np.reshape(crops_np[j], (1, cfg.CROP_SIZE, cfg.CROP_SIZE, 1))}
                # FIXME generate labels from gt ?
                blob_j['labels'] = blob_j['data'][:, :, :, 0]
                pred = np.reshape(results[5][j], (1, cfg.CROP_SIZE, cfg.CROP_SIZE))
                scores = np.reshape(results[6][j], (1, cfg.CROP_SIZE, cfg.CROP_SIZE))
                display_uresnet(blob_j, cfg, index=i*100+j, name='display_train', directory=os.path.join(cfg.DISPLAY_DIR, 'demo'), vmin=0, vmax=1, predictions=pred, scores=scores)

            cfg.IMAGE_SIZE = N


def inference(cfg):
    """
    Inference for either PPN or (xor) base network (e.g. UResNet)
    """
    if not os.path.isdir(cfg.DISPLAY_DIR):
        os.makedirs(cfg.DISPLAY_DIR)

    _, data = get_data(cfg)

    is_ppn = cfg.NET == 'ppn'
    is_uresnet = cfg.BASE_NET == 'uresnet' and cfg.NET == 'base'

    if is_ppn:
        net = PPN(cfg=cfg, base_net=basenets[cfg.BASE_NET])
        if cfg.WEIGHTS_FILE_PPN is None:
            pass
            # raise Exception("Need a checkpoint file for PPN at least")
    elif cfg.NET == 'base':
        net = basenets[cfg.BASE_NET](cfg=cfg)
        if cfg.WEIGHTS_FILE_PPN is None and cfg.WEIGHTS_FILE_BASE is None:
            raise Exception("Need a checkpoint file")

    net.init_placeholders()
    net.create_architecture(is_training=False)
    duration = 0

    if is_ppn:
        metrics = PPNMetrics(cfg, dim1=net.dim1, dim2=net.dim2)
    elif is_uresnet:
        metrics = UResNetMetrics(cfg)

    if cfg.PROFILE:
        print('WARNING PROFILING ENABLED')
        run_options = tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE)
        # print(getargspec(self.sess.run))
        run_metadata = tf.RunMetadata()
        old_run = tf.Session.run
        new_run = lambda self, fetches, feed_dict=None: old_run(self, fetches, feed_dict=feed_dict, options=run_options, run_metadata=run_metadata)
        tf.Session.run = new_run

    crop_algorithm = cropping_algorithms[cfg.CROP_ALGO](cfg)
    with tf.Session() as sess:
        sess.run(tf.global_variables_initializer())
        load_weights(cfg, sess)
        real_step = 0
        for i in range(cfg.MAX_STEPS):
            print("%d/%d" % (i, cfg.MAX_STEPS))
            blob = data.forward()
            # Cropping pre-processing
            patch_centers, patch_sizes = None, None
            if cfg.ENABLE_CROP:
                batch_blobs, patch_centers, patch_sizes = crop_algorithm.process(blob)
            else:
                batch_blobs = [blob]

            for j, blob in enumerate(batch_blobs):
                real_step += 1
                print(j)
                start = time.time()
                summary, results = net.test_image(sess, blob)
                end = time.time()
                duration += end - start
                # Drawing time
                if cfg.ENABLE_CROP:
                    N = cfg.IMAGE_SIZE
                    cfg.IMAGE_SIZE = cfg.SLICE_SIZE
                if is_ppn:
                    display(
                        blob,
                        cfg,
                        index=real_step,
                        dim1=net.dim1,
                        dim2=net.dim2,
                        directory=os.path.join(cfg.DISPLAY_DIR, 'demo'),
                        **results
                    )
                    metrics.add(blob, results)
                elif is_uresnet:
                    display_uresnet(blob, cfg, index=real_step, **results)
                    metrics.add(blob, results)
                else:
                    print(blob, results)
                if cfg.ENABLE_CROP:
                    cfg.IMAGE_SIZE = N

    if cfg.PROFILE:
        # Create the Timeline object, and write it to a json
        tl = timeline.Timeline(run_metadata.step_stats)
        ctf = tl.generate_chrome_trace_format()
        with open(cfg.PROFILE_NAME, 'w') as f:
            f.write(ctf)
            print("Wrote timeline to %s" % cfg.PROFILE_NAME)

        # # Print to stdout an analysis of the memory usage and the timing information
        # # broken down by python codes.
        # ProfileOptionBuilder = tf.profiler.ProfileOptionBuilder
        # opts = ProfileOptionBuilder(ProfileOptionBuilder.time_and_memory()
        #     ).with_node_names(show_name_regexes=['*']).build()
        #
        # tf.profiler.profile(
        #     tf.get_default_graph(),
        #     run_meta=run_metadata,
        #     cmd='code',
        #     options=opts)
        #
        # # Print to stdout an analysis of the memory usage and the timing information
        # # broken down by operation types.
        # tf.profiler.profile(
        #     tf.get_default_graph(),
        #     run_meta=run_metadata,
        #     cmd='op',
        #     options=tf.profiler.ProfileOptionBuilder.time_and_memory())

    duration /= cfg.MAX_STEPS
    print("Average duration of inference = %f ms" % duration)
    if is_ppn or is_uresnet:
        metrics.plot()


if __name__ == '__main__':
    # inference(cfg)
    inference_full(cfg)
