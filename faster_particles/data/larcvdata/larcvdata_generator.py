from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np
import os
import ROOT
# PyROOT hijacks help option otherwise
ROOT.PyConfig.IgnoreCommandLineOptions = True
from larcv import larcv
larcv.ThreadProcessor
from larcv.dataloader2 import larcv_threadio
import tempfile
from faster_particles.ppn_utils import crop


class LarcvGenerator(object):

    def __init__(self, cfg, ioname="ThreadProcessor", filelist=""):
        self.N = cfg.IMAGE_SIZE  # shape of canvas
        self.cfg = cfg
        self.dim = 3 if cfg.DATA_3D else 2

        np.random.seed(cfg.SEED)

        self.train_uresnet = (cfg.NET == 'base' and cfg.BASE_NET == 'uresnet')
        if cfg.DATA_3D:
            if self.train_uresnet and not self.cfg.URESNET_WEIGHTING:
                replace = 4
                config_file = 'uresnet_3d.cfg'
            elif self.train_uresnet and self.cfg.URESNET_WEIGHTING:
                replace = 6
                config_file = 'uresnet_3d_weight.cfg'
            elif self.cfg.NET == 'full':
                replace = 8
                config_file = 'ppn_uresnet_3d.cfg'
            else:
                replace = 6
                config_file = 'ppn_3d.cfg'
        else:
            if self.train_uresnet and not self.cfg.URESNET_WEIGHTING:
                replace = 4
                config_file = 'uresnet_2d.cfg'
            elif self.train_uresnet and self.cfg.URESNET_WEIGHTING:
                replace = 6
                config_file = 'uresnet_2d_weight.cfg'
            elif self.cfg.NET == 'full':
                replace = 8
                config_file = 'ppn_uresnet_2d.cfg'
            else:
                replace = 6
                config_file = 'ppn_2d.cfg'
        io_config = open(os.path.join(
            os.path.dirname(__file__),
            config_file)).read() % (
                (ioname, cfg.SEED, filelist) + (ioname,)*replace
                )
        self.ioname = ioname

        filler_config = tempfile.NamedTemporaryFile('w')
        filler_config.write(io_config)
        filler_config.flush()

        dataloader_cfg = {}
        dataloader_cfg["filler_name"] = "%sIO" % ioname
        dataloader_cfg["verbosity"]   = 0,
        dataloader_cfg['filler_cfg']  = filler_config.name
        # make explicit numpy array copy as we'll play w/ image data
        dataloader_cfg['make_copy']   = False

        self.proc = larcv_threadio()
        self.proc.configure(dataloader_cfg)
        self.proc.set_next_index(cfg.NEXT_INDEX)
        self.proc.start_manager(self.cfg.BATCH_SIZE)
        self.proc.next()

    def __del__(self):
        self.proc.stop_manager()
        self.proc.reset()

    def reset(self):
        self.proc.stop_manager()
        self.proc.reset()
        self.proc.set_next_index(self.cfg.NEXT_INDEX)
        self.proc.start_manager(self.cfg.BATCH_SIZE)
        self.proc.next()

    def extract_voxels(self, image):
        voxels, voxels_value = [], []
        indices = np.nonzero(image)[0]
        for i in indices:
            voxels_value.append(image[i])
            x = i % self.N
            i = (i-x)/self.N
            y = i % self.N
            i = (i-y)/self.N
            z = i % self.N
            voxels.append([x, y, z])
        return voxels, voxels_value

    def extract_pixels(self, image):
        pixels, pixels_value = [], []
        indices = np.nonzero(image)[0]
        for i in indices:
            pixels_value.append(image[i])
            x = i % self.N
            i = (i-x)/self.N
            y = i % self.N
            pixels.append([x, y])
        return pixels, pixels_value

    def extract_gt_pixels(self, t_points, s_points):
        gt_pixels = []
        if self.cfg.DATA_3D:
            for pt_index in np.arange(int(len(t_points)/3)):
                x = t_points[ 3*pt_index     ]
                y = t_points[ 3*pt_index + 1 ]
                z = t_points[ 3*pt_index + 2 ]
                if x < 0: break
                gt_pixels.append([z, y, x, 1])
            for pt_index in np.arange(int(len(s_points)/3)):
                x = s_points[ 3*pt_index     ]
                y = s_points[ 3*pt_index + 1 ]
                z = s_points[ 3*pt_index + 2 ]
                if x < 0: break
                gt_pixels.append([z, y, x, 2])
        else:
            for pt_index in np.arange(int(len(t_points)/2)):
                x = t_points[ 2*pt_index     ]
                y = t_points[ 2*pt_index + 1 ]
                if x < 0: break
                gt_pixels.append([y, x, 1])
            for pt_index in np.arange(int(len(s_points)/2)):
                x = s_points[ 2*pt_index     ]
                y = s_points[ 2*pt_index + 1 ]
                if x < 0: break
                gt_pixels.append([y, x, 2])
        return gt_pixels

    def forward(self):
        # Boolean: whether to include labels information
        # (only at UResNet training or full PPN+UResNet training)
        include_labels = self.train_uresnet or self.cfg.NET == 'full'
        include_ppn = self.cfg.NET == 'full' or self.cfg.NET == 'ppn' or self.cfg.NET == 'ppn_ext'

        self.proc.next(store_entries=True, store_event_ids=True)
        entries = self.proc.fetch_entries()
        batch_image  = self.proc.fetch_data ( '%s_data' % self.ioname   )
        if include_labels:
            batch_labels = self.proc.fetch_data('%s_labels' % self.ioname)
        if self.cfg.URESNET_WEIGHTING:
            batch_weight = self.proc.fetch_data('%s_weight' % self.ioname)
        if include_ppn:
            batch_track  = self.proc.fetch_data ( '%s_track' % self.ioname  )
            batch_shower = self.proc.fetch_data ( '%s_shower' % self.ioname )
        # batch_entries = self.proc.fetch_entries()
        # batch_event_ids = self.proc.fetch_event_ids()

        gt_pixels, output, output_labels, output_weight, final_entries = [], [], [], [], []
        output_voxels, output_voxels_value, batch_index = [], [], []
        img_shape = (self.cfg.BATCH_SIZE,) + (self.N,) * self.dim + (1,)
        labels_shape = (self.cfg.BATCH_SIZE,) + (self.N,) * self.dim
        weight_shape = labels_shape
        voxels_shape = (self.cfg.BATCH_SIZE, -1, self.dim)
        voxels_value_shape = (self.cfg.BATCH_SIZE, -1)

        for index in np.arange(self.cfg.BATCH_SIZE):
            image = batch_image.data()[index]
            if include_labels:
                labels = batch_labels.data()[index]
            if self.cfg.URESNET_WEIGHTING:
                weight = batch_weight.data()[index]
            if include_ppn:
                t_points = batch_track.data()[index]
                s_points = batch_shower.data()[index]
            entry_id = entries.data()[index]

            final_entries.append(entry_id)
            voxels, voxels_value = self.extract_voxels(image) if self.cfg.DATA_3D else self.extract_pixels(image)

            image = image.reshape(img_shape[1:])
            if include_labels:
                labels = labels.reshape(labels_shape[1:])
            if self.cfg.URESNET_WEIGHTING:
                weight = weight.reshape(weight_shape[1:])

            # TODO set N from this
            # TODO For now we only consider batch size 1
            if include_ppn:
                gt_pixels.extend(self.extract_gt_pixels(t_points, s_points))

            if not include_ppn or len(gt_pixels) > 0:
                output.append(image)
                if include_labels:
                    output_labels.append(labels)
                if self.cfg.URESNET_WEIGHTING:
                    output_weight.append(weight)
            voxels = np.array(voxels)
            voxels_value = np.array(voxels_value)
            if self.cfg.BATCH_SIZE > 1 and self.cfg.SPARSE:
                output_voxels.append(np.pad(voxels, [(0, 0), (0, 1)], 'constant', constant_values=index))
            else:
                output_voxels.append(voxels)
            output_voxels_value.append(voxels_value)

        if len(output) == 0:  # No gt pixels in this batch - try next batch
            print("DUMP - no gt pixels in this batch, try next batch")
            return self.forward()

        output = np.reshape(np.array(output), img_shape)
        if include_labels:
            output_labels = np.reshape(np.array(output_labels), labels_shape)
        if self.cfg.URESNET_WEIGHTING:
            output_weight = np.reshape(np.array(output_weight), weight_shape)

        output_voxels = np.vstack(output_voxels)
        output_voxels_value = np.hstack(output_voxels_value)

        blob = {}
        blob['data'] = output.astype(np.float32)
        if include_labels:
            blob['labels'] = output_labels.astype(np.int32)
        if self.cfg.URESNET_WEIGHTING:
            blob['weight'] = output_weight.astype(np.float32)
        if include_ppn:
            blob['gt_pixels'] = np.array(gt_pixels)
        blob['voxels'] = output_voxels  # np.array(voxels)
        blob['voxels_value'] = output_voxels_value  # np.array(voxels_value)
        blob['entries'] = final_entries
        # Crop regions around gt points for small UResNet
        if self.cfg.NET == 'small_uresnet':
            blob['crops'], blob['crops_labels'] = crop(
                blob['gt_pixels'][:, :-1],
                self.cfg.CROP_SIZE,
                blob['data'],
                use_smear=True)

        return blob


if __name__ == '__main__':
    class MyCfg:
        IMAGE_SIZE = 512
        SEED = 123
        BATCH_SIZE = 1
        DATA_3D = False
        NET = "small_uresnet"
        BASE = "uresnet"
        NEXT_INDEX = 0
        CROP_SIZE = 24

    t = LarcvGenerator(MyCfg(), ioname='test',
                       filelist='["/stage/drinkingkazu/fuckgrid/p00/larcv.root"]')
    for i in range(1):
        blobdict = t.forward()
        print(blobdict['data'].shape)
        print("gt pixels shape ", blobdict['gt_pixels'].shape)
