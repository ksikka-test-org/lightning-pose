import os
import torch
from torch import nn
import torch.nn.functional as F
from torchvision import transforms
from torch.utils.data import DataLoader, random_split
import pytorch_lightning as pl
from pytorch_lightning.tuner.tuning import Tuner
from pose_est_nets.models.heatmap_tracker import DLC
from pose_est_nets.datasets.datasets import DLCHeatmapDataset, TrackingDataModule
from typing import Any, Callable, Optional, Tuple, List
import json
import matplotlib.pyplot as plt
import argparse
import pandas as pd
import imgaug.augmenters as iaa
#from PIL import Image, ImageDraw
from deepposekit.utils.image import largest_factor
from deepposekit.models.backend.backend import find_subpixel_maxima
import numpy as np

def upsampleArgmax(heatmap_pred, heatmap_y):
    heatmap_pred = nn.Upsample(scale_factor = 4)(heatmap_pred)
    heatmap_pred = heatmap_pred[0]
    y = nn.Upsample(scale_factor = 4)(heatmap_y)
    y = y[0]
    pred_keypoints = torch.empty(size = (y.shape[0], 2))
    y_keypoints = torch.empty(size = (y.shape[0], 2))
    for bp_idx in range(y.shape[0]):
        pred_keypoints[bp_idx] = torch.tensor(np.unravel_index(heatmap_pred[bp_idx].argmax(), heatmap_pred[bp_idx].shape))
        y_keypoints[bp_idx] = torch.tensor(np.unravel_index(y[bp_idx].argmax(), y[bp_idx].shape))    
    return pred_keypoints, y_keypoints

def saveNumericalPredictions(threshold):
    i = 0
    #hardcoded for mouse data
    rev_augmenter = []
    rev_augmenter.append(iaa.Resize({"height": 406, "width": 396})) #get rid of this for the fish
    rev_augmenter = iaa.Sequential(rev_augmenter)
    model.eval()
    full_dl = datamod.full_dataloader()
    test_dl = datamod.test_dataloader()
    final_gt_keypoints = np.empty(shape = (len(test_dl), model.num_keypoints, 2))
    final_imgs = np.empty(shape = (len(test_dl), 406, 396, 1))
    final_preds = np.empty(shape = (len(test_dl), model.num_keypoints, 2))

    #dpk_final_preds = np.empty(shape = (len(test_dl), model.num_keypoints, 2))

    for idx, batch in enumerate(test_dl):
        x, y = batch
        heatmap_pred = model.forward(x)
        output_shape = data.output_shape #changed to small
        #dpk_pred_keypoints, dpk_y_keypoints = computeSubPixMax(heatmap_pred, y, output_shape, threshold)
        pred_keypoints, y_keypoints = model.computeSubPixMax(heatmap_pred.cuda(), y.cuda(), threshold)
        #dpk_final_preds[i] = pred_keypoints
        pred_keypoints = pred_keypoints.cpu()
        y_keypoints = y_keypoints.cpu()
        x = x[:,0,:,:] #only taking one image dimension
        x = np.expand_dims(x, axis = 3)
        final_imgs[i], final_gt_keypoints[i] = rev_augmenter(images = x, keypoints = np.expand_dims(y_keypoints, axis = 0))
        final_imgs[i], final_preds[i] = rev_augmenter(images = x, keypoints = np.expand_dims(pred_keypoints, axis = 0))
        #final_gt_keypoints[i] = y_keypoints
        #final_preds[i] = pred_keypoints
        i += 1

    final_gt_keypoints = np.reshape(final_gt_keypoints, newshape = (len(test_dl), model.num_targets))
    final_preds = np.reshape(final_preds, newshape = (len(test_dl), model.num_targets))
    #dpk_final_preds = np.reshape(dpk_final_preds, newshape = (len(test_dl), model.num_targets))

    #np.savetxt('../preds/mouse_gt.csv', final_gt_keypoints, delimiter = ',', newline = '\n')
    np.savetxt('../preds/mouse_pca2view_larger_preds.csv', final_preds, delimiter = ',', newline = '\n')
    #np.savetxt('../preds/dpk_fish_predictions.csv', dpk_final_preds, delimiter = ',', newline = '\n')
    return

def plotPredictions(save_heatmaps, threshold, mode):
    model.eval()
    if mode == 'train':
        dl = datamod.train_dataloader()
    else:
        dl = datamod.test_dataloader()
    i = 0
    for idx, batch in enumerate(dl):
        x, y = batch
        heatmap_pred = model.forward(x)
        if (save_heatmaps):
            plt.imshow(heatmap_pred[0, 4].detach())
            plt.savefig('../preds/mouse_pca2view_maps/pred_map' + str(i) + '.png')
            plt.clf()
            plt.imshow(y[0, 4].detach())
            plt.savefig('../preds/mouse_pca2view_maps/gt_map' + str(i) + '.png')
            plt.clf()
        output_shape = data.output_shape #changed from train_data
        #print(heatmap_pred.device, y.device, model.device)
        #exit()
        pred_keypoints, y_keypoints = model.computeSubPixMax(heatmap_pred.cuda(), y.cuda(), threshold)
        plt.imshow(x[0][0])
        pred_keypoints = pred_keypoints.cpu()
        y_keypoints = y_keypoints.cpu()
        plt.scatter(pred_keypoints[:,0], pred_keypoints[:,1], c = 'blue')
        plt.scatter(y_keypoints[:,0], y_keypoints[:,1], c = 'orange')
        plt.savefig('../preds/mouse_pca2view_larger_preds/pred' + str(i) + '.png')
        plt.clf()
        i += 1


parser = argparse.ArgumentParser()

parser.add_argument("--no_train", help= "whether you want to skip training the model")
parser.add_argument("--load", help = "set true to load model from checkpoint")
parser.add_argument("--predict", help = "whether or not to generate predictions on test data")
parser.add_argument("--ckpt", type = str, default = "lightning_logs2/version_1/checkpoints/epoch=271-step=12511.ckpt", help = "path to model checkpoint if you want to load model from checkpoint")
parser.add_argument("--train_batch_size", type = int, default = 16)
parser.add_argument("--validation_batch_size", type = int, default = 10)
parser.add_argument("--test_batch_size", type = int, default = 1)
parser.add_argument("--num_gpus", type = int, default = 1)
parser.add_argument("--num_workers", type = int, default = 8)
#parser.add_argument("--num_keypoints", type = int, default = 108) #fish data default
parser.add_argument("--data_dir", type = str, default = '../../deepposekit-tests/dlc_test/mouse_data/data')
#fish = '../data'
#mouse = '../../deepposekit-tests/dlc_test/mouse_data/data'
parser.add_argument("--data_path", type = str, default = 'CollectedData_.csv')
#fish = 'tank_dataset_13.h5'
#mouse = 'CollectedData_.csv'
parser.add_argument("--select_data_mode", type = str, default = 'random', help = "set to deterministic if you want to train and test on specific data for mouse dataset, set to random if you want a random train/test split") 
parser.add_argument("--downsample_factor", type = int, default = 3)
args = parser.parse_args()

torch.manual_seed(11)

#Hardcoded for fish data for now, in the future we can have feature which will automatically check if a data_transform needs to be applied and select the right transformation
data_transform = []
data_transform.append(iaa.Resize({"height": 384, "width": 384})) #dlc dimensions need to be repeatably divisable by 2
data_transform = iaa.Sequential(data_transform)

mode = args.data_path.split('.')[-1]
#header rows are hardcoded
header_rows = [1, 2]

if args.select_data_mode == 'deterministic':
    train_data = DLCHeatmapDataset(root_directory= args.data_dir, data_path=args.data_path, header_rows=header_rows, mode = mode, transform=data_transform, noNans = True, downsample_factor = args.downsample_factor)
    train_data.image_names = train_data.image_names[:183]
    train_data.labels = train_data.labels[:183]
    train_data.compute_heatmaps()
    val_data = DLCHeatmapDataset(root_directory= args.data_dir, data_path=args.data_path, header_rows=header_rows, mode = mode, transform=data_transform, noNans = True, downsample_factor = args.downsample_factor)
    val_data.image_names = val_data.image_names[183:183+22]
    val_data.labels = val_data.labels[183:183+22]
    val_data.compute_heatmaps()
    test_data = DLCHeatmapDataset(root_directory= args.data_dir, data_path=args.data_path, header_rows=header_rows, mode = mode, transform=data_transform, noNans = True, downsample_factor = args.downsample_factor)
    test_data.image_names = test_data.image_names[205:]
    test_data.labels = test_data.labels[205:]
    test_data.compute_heatmaps()
    datamod = TrackingDataModule(train_data, mode = args.select_data_mode, train_batch_size = 16, validation_batch_size = 10, test_batch_size = 1, num_workers = args.num_workers) #dlc configs
    datamod.train_set = train_data
    datamod.valid_set = val_data
    datamod.test_set = test_data
    data = train_data
else:
    full_data = DLCHeatmapDataset(root_directory= args.data_dir, data_path=args.data_path, header_rows=header_rows, mode = mode, noNans = True, transform = data_transform)
    datamod = TrackingDataModule(full_data, mode = args.select_data_mode, train_batch_size = 16, validation_batch_size = 10, test_batch_size = 1, num_workers = args.num_workers) #dlc configs
    data = full_data

datamod.setup()
datamod.computePPCA_params()

model = DLC(num_targets = data.num_targets, resnet_version = 50, transfer = False, downsample_factor = args.downsample_factor)
if (args.load):
    model = model.load_from_checkpoint(checkpoint_path = args.ckpt, num_targets = data.num_targets, resnet_version = 50, transfer = False, downsample_factor = args.downsample_factor)

model.pca_param_dict = datamod.pca_param_dict
model.output_shape = data.output_shape
model.output_sigma = data.output_sigma
model.upsample_factor = torch.tensor(100, device = 'cuda')
#model.coordinate_scale = torch.tensor(8, device = 'cuda')
model.confidence_scale = torch.tensor(255.0, device = 'cuda')

early_stopping = pl.callbacks.EarlyStopping(
    monitor="val_loss", patience=100, mode="min"
)
lr_monitor = pl.callbacks.LearningRateMonitor(logging_interval = 'epoch')


trainer = pl.Trainer(gpus=args.num_gpus, log_every_n_steps = 15, callbacks=[early_stopping, lr_monitor], auto_scale_batch_size = False, reload_dataloaders_every_epoch=False)

if (not(args.no_train)):
    trainer.fit(model = model, datamodule = datamod)
else:
    datamod.setup()

if args.predict:
    model.eval()
    trainer.test(model = model, datamodule = datamod)
    threshold = False #whether or not to refrain from plotting a keypoint if the max value of the heatmap is below a certain threshold
    save_heatmaps = False #whether or not to save heatmap images, note they will be in the downsampled dimensions
    mode = 'test'
    plotPredictions(save_heatmaps, threshold, mode)
    threshold = False
    saveNumericalPredictions(threshold)
    

