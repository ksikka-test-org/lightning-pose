"""Data pipelines based on efficient video reading by nvidia dali package."""

from nvidia.dali import pipeline_def
import nvidia.dali.fn as fn
from nvidia.dali.plugin.pytorch import LastBatchPolicy
from nvidia.dali.plugin.pytorch import DALIGenericIterator
import nvidia.dali.types as types
from omegaconf import DictConfig
import torch
import numpy as np
from typeguard import typechecked
from typing import List, Dict, Optional, Union, Literal
from torchtyping import patch_typeguard


from lightning_pose.data import _IMAGENET_MEAN, _IMAGENET_STD
from lightning_pose.data.utils import count_frames

_DALI_DEVICE = "gpu" if torch.cuda.is_available() else "cpu"

patch_typeguard()  # use before @typechecked. TODO: new, make sure it doesn't mess things


# cannot typecheck due to way pipeline_def decorator consumes additional args
@pipeline_def
def video_pipe(
    filenames: Union[List[str], str],
    resize_dims: Optional[List[int]] = None,
    random_shuffle: bool = False,
    seed: int = 123456,
    sequence_length: int = 16,
    pad_sequences: bool = True,
    initial_fill: int = 16,
    normalization_mean: List[float] = _IMAGENET_MEAN,
    normalization_std: List[float] = _IMAGENET_STD,
    device: str = _DALI_DEVICE,
    name: str = "reader",
    step: int = 1,
    pad_last_batch: bool = False,
    imgaug: str = "default",
    # arguments consumed by decorator:
    # batch_size,
    # num_threads,
    # device_id
) -> tuple:
    """Generic video reader pipeline that loads videos, resizes, and normalizes.

    Args:
        filenames: list of absolute paths of video files to feed through
            pipeline
        resize_dims: [height, width] to resize raw frames
        random_shuffle: True to grab random batches of frames from videos;
            False to sequential read
        seed: random seed when `random_shuffle` is True
        sequence_length: number of frames to load per sequence
        pad_sequences: allows creation of incomplete sequences if there is an
            insufficient number of frames at the very end of the video
        initial_fill: size of the buffer that is used for random shuffling
        normalization_mean: mean values in (0, 1) to subtract from each channel
        normalization_std: standard deviation values to subtract from each
            channel
        device: "cpu" | "gpu"
        name: pipeline name, used to string together DataNode elements
        step: number of frames to advance on each read
        pad_last_batch
        imgaug

    Returns:
        pipeline object to be fed to DALIGenericIterator
        data augmentation matrix (returned so that geometric transforms can be undone)

    """
    video = fn.readers.video(
        device=device,
        filenames=filenames,
        random_shuffle=random_shuffle,
        seed=seed,
        sequence_length=sequence_length,
        step=step,
        pad_sequences=pad_sequences,
        initial_fill=initial_fill,
        normalized=False,
        name=name,
        dtype=types.DALIDataType.FLOAT,
        pad_last_batch=pad_last_batch,  # Important for context loaders
    )
    if resize_dims:
        video = fn.resize(video, size=resize_dims)
    if imgaug == "dlc" or imgaug == "dlc-light":
        size = (resize_dims[0] / 2, resize_dims[1] / 2)
        center = size  # / 2
        # rotate + scale
        angle = fn.random.uniform(range=(-10, 10))
        matrix = fn.transforms.rotation(angle=angle, center=center)
        scale = fn.random.uniform(range=(0.8, 1.2), shape=2)
        matrix = fn.transforms.scale(matrix, scale=scale, center=center)
        video = fn.warp_affine(video, matrix=matrix, fill_value=0, inverse_map=False)
        # brightness contrast:
        contrast = fn.random.uniform(range=(0.75, 1.25))
        brightness = fn.random.uniform(range=(0.75, 1.25))
        video = fn.brightness_contrast(video, brightness=brightness, contrast=contrast)
        # # shot noise
        factor = fn.random.uniform(range=(0.0, 10.0))
        video = fn.noise.shot(video, factor=factor)
        # jpeg compression
        # quality = fn.random.uniform(range=(50, 100), dtype=types.INT32)
        # video = fn.jpeg_compression_distortion(video, quality=quality)
    else:
        matrix = -1
    # video pixel range is [0, 255]; transform it to [0, 1].
    # happens naturally in the torchvision transform to tensor.
    video = video / 255.0
    # permute dimensions and normalize to imagenet statistics
    transform = fn.crop_mirror_normalize(
        video,
        output_layout="FCHW",
        mean=normalization_mean,
        std=normalization_std,
    )
    return transform, matrix


@typechecked
class LitDaliWrapper(DALIGenericIterator):
    """wrapper around a DALI pipeline to get batches for ptl."""

    def __init__(
        self,
        *args,
        eval_mode: Literal["train", "predict"],
        num_iters: int = 1,
        do_context: bool = False,
        context_sequences_successive: bool = False,
        **kwargs
    ):
        """ wrapper around DALIGenericIterator to get batches for pl.
        Args: 
            num_iters: number of enumerations of dataloader (should be computed outside for now;
                should be fixed by lightning/dali teams)
            do_context: whether model/loader use 5-frame context or not

        """
        # TODO: add a case where we 
        self.num_iters = num_iters
        self.do_context = do_context
        self.eval_mode = eval_mode
        self.context_sequences_successive = context_sequences_successive
        self.batch_sampler = 1  # hack to get around DALI-ptl issue
        # call parent
        super().__init__(*args, **kwargs)

    def __len__(self):
        return self.num_iters
    
    def _modify_output(
        self, out
    ) -> tuple:
          #-> Union[
          #      TensorType["sequence_length", 3, "image_height", "image_width"],
          #      TensorType["batch", 5, 3, "image_height", "image_width"]
          #  ]:
        """ modify output to be torch tensor. 
        looks different for context and non-context."""
        if self.do_context:
            if self.eval_mode == "predict":
                # shape (sequence_length, 3, H, W)
                images = out[0]["x"]
                # shape (1,)
                transform = out[0]["transform"]
            else:
                # train with context. pipeline is like for "base" model, but we reshape images
                # further down. assume batch_size=1
                if self.context_sequences_successive:
                    # shape (sequence_length, 3, H, W)
                    images = out[0]["x"][0, :, :, :, :]  # .clone().detach().type(torch.float)
                    # shape (1,) or (2, 3)
                    transform = out[0]["transform"][0]
                else:
                    # grabbing independent 5-frame sequences. batch_size > 1
                    # shape (batch_size, sequence_length, 3, H, W)
                    images = out[0]["x"]
                    # shape (batch_size, 1) or (batch_size, 2, 3)
                    transform = out[0]["transform"]
        else:
            # shape (sequence_length, 3, H, W)
            # careful: only valid for one sequence, i.e., batch size of 1.
            images = out[0]["x"][0, :, :, :, :]  # .clone().detach().type(torch.float)
            # shape (1,) or (2, 3)
            transform = out[0]["transform"][0]

        return images, transform

    def __next__(self):
        out = super().__next__()
        return self._modify_output(out)


class PrepareDALI(object):
    
    """All the DALI stuff in one place.

    TODO: make sure the order of args when you mix is valid.
    TODO: consider changing LightningWrapper args from num_batches to num_iter

    Big picture: this will initialize the pipes and dataloaders which will be called only in
    Trainer.predict().

    Another option -- make this valid for Trainer.train() as well, so the unlabeled stuff will be
    initialized here.

    Thoughts: define a dict with args for pipe and data loader, per condition.
    """

    def __init__(
        self,
        train_stage: Literal["predict", "train"],
        model_type: Literal["base", "context"],
        filenames: List[str],
        resize_dims: List[int],
        dali_config: Union[dict, DictConfig] = None,
        imgaug: Optional[str] = "default",
    ):
        self.train_stage = train_stage
        self.model_type = model_type
        self.resize_dims = resize_dims
        self.dali_config = dali_config
        self.filenames = filenames
        self.frame_count = count_frames(self.filenames)
        self.context_sequences_successive = \
            self.dali_config["context"]["train"]["consecutive_sequences"]
        self._pipe_dict: dict = self._setup_pipe_dict(self.filenames, imgaug)

    @property
    def num_iters(self) -> int:
        # count frames
        # "how many times should we enumerate the data loader?"
        # sum across vids
        pipe_dict = self._pipe_dict[self.train_stage][self.model_type]
        if self.model_type == "base":
            return int(np.ceil(self.frame_count / (pipe_dict["sequence_length"])))
        elif self.model_type == "context":
            if pipe_dict["step"] == 1: # 0-5, 1-6, 2-7, 3-8, 4-9 ...
                return int(np.ceil(self.frame_count / (pipe_dict["batch_size"])))
            elif pipe_dict["step"] == pipe_dict["sequence_length"]:
                # context_sequences_successive = False
                # taking the floor because during training we don't care about missing the last
                # non-full batch. we prefer having fewer batches but valid.
                return int(np.floor(
                    self.frame_count / (pipe_dict["batch_size"] * pipe_dict["sequence_length"])))
            else:
                raise NotImplementedError
    
    def _setup_pipe_dict(self, filenames: List[str], imgaug: str) -> Dict[str, dict]:
        """all of the pipe() args in one place"""
        dict_args = {}
        dict_args["predict"] = {"context": {}, "base": {}}
        dict_args["train"] = {"context": {}, "base": {}}
        gen_cfg = self.dali_config["general"]

        # base (vanilla single-frame model), 
        # train pipe args
        base_train_cfg = self.dali_config["base"]["train"]
        dict_args["train"]["base"] = {
            "filenames": filenames,
            "resize_dims": self.resize_dims,
            "sequence_length": base_train_cfg["sequence_length"],
            "step": base_train_cfg["sequence_length"],
            "batch_size": 1,
            "seed": gen_cfg["seed"],
            "num_threads": gen_cfg["num_threads"],
            "device_id": gen_cfg["device_id"],
            "random_shuffle": True,
            "device": gen_cfg["device"],
            "imgaug": imgaug,
        }

        # base (vanilla model), predict pipe args 
        base_pred_cfg = self.dali_config["base"]["predict"]
        dict_args["predict"]["base"] = {
            "filenames": filenames,
            "resize_dims": self.resize_dims,
            "sequence_length": base_pred_cfg["sequence_length"],
            "step": base_pred_cfg["sequence_length"],
            "batch_size": 1,
            "seed": gen_cfg["seed"],
            "num_threads":  gen_cfg["num_threads"],
            "device_id": gen_cfg["device_id"],
            "random_shuffle": False,
            "device": gen_cfg["device"],
            "name": "reader",
            "pad_sequences": True,
            "imgaug": "default",
        }

        # context (five-frame) model
        # predict pipe args
        context_pred_cfg = self.dali_config["context"]["predict"]
        dict_args["predict"]["context"] = {
            "filenames": filenames,
            "resize_dims": self.resize_dims,
            "sequence_length": 5,
            "step": 1,
            "batch_size": context_pred_cfg["batch_size"],
            "num_threads": gen_cfg["num_threads"],
            "device_id": gen_cfg["device_id"],
            "random_shuffle": False,
            "device": gen_cfg["device"],
            "name": "reader",
            "seed": gen_cfg["seed"],
            "pad_sequences": True,
            "pad_last_batch": True,
            "imgaug": "default",
        }

        # train pipe args
        context_train_cfg = self.dali_config["context"]["train"]
        if self.context_sequences_successive:
            # note: reusing the batch size argument
            dict_args["train"]["context"] = {
                "filenames": filenames,
                "resize_dims": self.resize_dims,
                "sequence_length": context_train_cfg["batch_size"],
                "step": context_train_cfg["batch_size"],
                "batch_size": 1,
                "seed": gen_cfg["seed"],
                "num_threads": gen_cfg["num_threads"],
                "device_id": gen_cfg["device_id"],
                "random_shuffle": True,
                "device": gen_cfg["device"],
                "imgaug": imgaug,
            }
        else:
            dict_args["train"]["context"] = {
                "filenames": filenames,
                "resize_dims": self.resize_dims,
                "sequence_length": 5,
                "step": 5,
                "batch_size": context_train_cfg["batch_size"],
                "num_threads": gen_cfg["num_threads"],
                "device_id": gen_cfg["device_id"],
                "random_shuffle": True,
                "device": gen_cfg["device"],
                "name": "reader",
                "seed": gen_cfg["seed"],
                "pad_sequences": True,
                "pad_last_batch": False,
                "imgaug": imgaug,
            }
            # our floor above should prevent us from getting to the very final batch.
        
        return dict_args
    
    def _get_dali_pipe(self):
        """
        Return a DALI pipe with predefined args.
        """

        pipe_args = self._pipe_dict[self.train_stage][self.model_type]
        pipe = video_pipe(**pipe_args)
        return pipe
    
    def _setup_dali_iterator_args(self) -> dict:
        """ builds args for Lightning iterator"""
        dict_args = {}
        dict_args["predict"] = {"context": {}, "base": {}}
        dict_args["train"] = {"context": {}, "base": {}}

        # base models (single-frame)
        dict_args["train"]["base"] = {
            "num_iters": self.num_iters,
            "eval_mode": "train",
            "do_context": False,
            "output_map": ["x", "transform"],
            "last_batch_policy": LastBatchPolicy.PARTIAL,
            "auto_reset": True
        }
        dict_args["predict"]["base"] = {
            "num_iters": self.num_iters,
            "eval_mode": "predict",
            "do_context": False,
            "output_map": ["x", "transform"],
            "last_batch_policy": LastBatchPolicy.FILL,
            "last_batch_padded": False,
            "auto_reset": False,
            "reader_name": "reader"
        }

        # 5-frame context models
        dict_args["train"]["context"] = {
            "num_iters": self.num_iters,
            "eval_mode": "train",
            "do_context": True,
            "context_sequences_successive": self.context_sequences_successive,
            "output_map": ["x", "transform"],
            "last_batch_policy": LastBatchPolicy.PARTIAL,
            "auto_reset": True
        }  # taken from datamodules.py. only difference is that we need to do context
        dict_args["predict"]["context"] = {
            "num_iters": self.num_iters,
            "eval_mode": "predict",
            "do_context": True,
            "output_map": ["x", "transform"],
            "last_batch_policy": LastBatchPolicy.PARTIAL,
            "last_batch_padded": False,
            "auto_reset": False,
            "reader_name": "reader"
        }

        return dict_args
    
    def __call__(self) -> LitDaliWrapper:
        """
        Returns a LightningWrapper object.
        """
        pipe = self._get_dali_pipe()
        args = self._setup_dali_iterator_args()
        return LitDaliWrapper(pipe, **args[self.train_stage][self.model_type])
