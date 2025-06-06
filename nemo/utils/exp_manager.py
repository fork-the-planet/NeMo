# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import glob
import os
import signal
import subprocess
import sys
import time
import warnings
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from shutil import copy, move
from typing import Any, Collection, Dict, List, Optional, Tuple, Union

import lightning.pytorch
import torch
from hydra.core.hydra_config import HydraConfig
from hydra.utils import get_original_cwd
from lightning.pytorch.callbacks import Callback, ModelCheckpoint
from lightning.pytorch.callbacks.early_stopping import EarlyStopping
from lightning.pytorch.callbacks.timer import Interval, Timer
from lightning.pytorch.loggers import MLFlowLogger, NeptuneLogger, TensorBoardLogger, WandbLogger
from lightning.pytorch.loops import _TrainingEpochLoop
from lightning.pytorch.strategies.ddp import DDPStrategy
from lightning.pytorch.trainer.connectors.checkpoint_connector import _CheckpointConnector
from omegaconf import DictConfig, OmegaConf, open_dict

from nemo.collections.common.callbacks import EMA
from nemo.constants import NEMO_ENV_VARNAME_TESTING, NEMO_ENV_VARNAME_VERSION
from nemo.utils import logging, timers
from nemo.utils.app_state import AppState
from nemo.utils.callbacks import NeMoModelCheckpoint, PreemptionCallback
from nemo.utils.env_var_parsing import get_envbool
from nemo.utils.exceptions import NeMoBaseException
from nemo.utils.get_rank import is_global_rank_zero
from nemo.utils.import_utils import safe_import_from
from nemo.utils.lightning_logger_patch import add_filehandlers_to_pl_logger
from nemo.utils.loggers import ClearMLLogger, ClearMLParams, DLLogger, DLLoggerParams, MLFlowParams
from nemo.utils.mcore_logger import add_handlers_to_mcore_logger
from nemo.utils.model_utils import uninject_model_parallel_rank
from nemo.utils.msc_utils import import_multistorageclient, is_multistorageclient_url

get_current_global_batch_size, HAVE_MCORE_MBATCH_CALCULATOR = safe_import_from(
    "megatron.core.num_microbatches_calculator", "get_current_global_batch_size"
)


try:
    # `ptl_resiliency` is included in `gwe_resiliency_pkg` package
    from ptl_resiliency import StragglerDetectionCallback

    HAVE_STRAGGLER_DET = True
except (ImportError, ModuleNotFoundError):
    HAVE_STRAGGLER_DET = False

try:
    from ptl_resiliency import FaultToleranceCallback

    HAVE_FT = True
except (ImportError, ModuleNotFoundError):
    HAVE_FT = False


class NotFoundError(NeMoBaseException):
    """Raised when a file or folder is not found"""


class LoggerMisconfigurationError(NeMoBaseException):
    """Raised when a mismatch between trainer.logger and exp_manager occurs"""

    def __init__(self, message):
        message = (
            message
            + " You can disable lighning's trainer from creating a logger by passing logger=False to its constructor."
        )
        super().__init__(message)


class CheckpointMisconfigurationError(NeMoBaseException):
    """Raised when a mismatch between trainer.callbacks and exp_manager occurs"""


@dataclass
class EarlyStoppingParams:
    """EarlyStoppingParams POD"""

    # The metric that early stopping should consider.
    monitor: str = "val_loss"
    # inform early stopping whether to look for increase or decrease in monitor.
    mode: str = "min"
    min_delta: float = 0.001  # smallest change to consider as improvement.
    # how many (continuous) validation cycles to wait with no improvement and stopping training.
    patience: int = 10
    verbose: bool = True
    strict: bool = True
    check_finite: bool = True
    stopping_threshold: Optional[float] = None
    divergence_threshold: Optional[float] = None
    check_on_train_epoch_end: Optional[bool] = None
    log_rank_zero_only: bool = False


@dataclass
class CallbackParams:
    """CallbackParams POD"""

    filepath: Optional[str] = None  # Deprecated
    # If None, exp_manager will attempt to handle the filepath
    dirpath: Optional[str] = None
    # If None, exp_manager will attempt to handle the filepath
    filename: Optional[str] = None
    monitor: Optional[str] = "val_loss"
    verbose: Optional[bool] = True
    save_last: Optional[bool] = True
    save_top_k: Optional[int] = 3
    save_weights_only: Optional[bool] = False
    mode: Optional[str] = "min"
    auto_insert_metric_name: bool = True
    every_n_epochs: Optional[int] = 1
    every_n_train_steps: Optional[int] = None
    train_time_interval: Optional[Any] = None
    # If None, exp_manager will attempt to handle the filepath
    prefix: Optional[str] = None
    postfix: str = ".nemo"
    save_best_model: bool = False
    always_save_nemo: bool = False
    # Whether to automatically save .nemo file durin on_train_end hook
    save_nemo_on_train_end: Optional[bool] = True
    # tensor parallel size * pipeline parallel size
    model_parallel_size: Optional[int] = None
    # Save after training, not after validation
    save_on_train_epoch_end: Optional[bool] = False
    async_save: Optional[bool] = False  # save the checkpoint asynchronously
    # a number of last checkpoints to be saved with optimizer states
    save_last_n_optim_states: Optional[int] = -1


@dataclass
class StepTimingParams:
    """StepTimingParams POD"""

    reduction: Optional[str] = "mean"
    # if True torch.cuda.synchronize() is called on start/stop
    sync_cuda: Optional[bool] = False
    # if positive, defines the size of a sliding window for computing mean
    buffer_size: Optional[int] = 1


@dataclass
class EMAParams:
    """EMAParams POD"""

    enable: Optional[bool] = False
    decay: Optional[float] = 0.999
    cpu_offload: Optional[bool] = False
    validate_original_weights: Optional[bool] = False
    every_n_steps: int = 1


@dataclass
class StragglerDetectionParams:
    """StragglerDetectionParams POD"""

    report_time_interval: float = 300
    calc_relative_gpu_perf: bool = True
    calc_individual_gpu_perf: bool = True
    num_gpu_perf_scores_to_log: int = 5
    gpu_relative_perf_threshold: float = 0.7
    gpu_individual_perf_threshold: float = 0.7
    stop_if_detected: bool = False


@dataclass
class FaultToleranceParams:
    """FaultToleranceParams POD"""

    # NOTE: This config section is also read by the launcher.
    # NOTE: Default values should match fault_tolerance.FaultToleranceConfig.

    workload_check_interval: float = 5.0
    initial_rank_heartbeat_timeout: Optional[float] = 60.0 * 60.0
    rank_heartbeat_timeout: Optional[float] = 45.0 * 60.0
    calculate_timeouts: bool = True
    safety_factor: float = 5.0
    rank_termination_signal: signal.Signals = signal.SIGKILL if os.name != 'nt' else signal.SIGTERM
    log_level: str = 'INFO'
    max_rank_restarts: int = 0
    max_subsequent_job_failures: int = 0
    additional_ft_launcher_args: str = ''
    simulated_fault: Optional[Any] = None


@dataclass
class ExpManagerConfig:
    """Experiment Manager config for validation of passed arguments."""

    # Log dir creation parameters
    explicit_log_dir: Optional[str] = None
    exp_dir: Optional[str] = None
    name: Optional[str] = None
    version: Optional[str] = None
    use_datetime_version: Optional[bool] = True
    resume_if_exists: Optional[bool] = False
    resume_past_end: Optional[bool] = False
    resume_ignore_no_checkpoint: Optional[bool] = False
    resume_from_checkpoint: Optional[str] = None
    # Logging parameters
    create_tensorboard_logger: Optional[bool] = True
    summary_writer_kwargs: Optional[Dict[Any, Any]] = None
    create_wandb_logger: Optional[bool] = False
    wandb_logger_kwargs: Optional[Dict[Any, Any]] = None
    create_mlflow_logger: Optional[bool] = False
    mlflow_logger_kwargs: Optional[MLFlowParams] = field(default_factory=lambda: MLFlowParams())
    create_dllogger_logger: Optional[bool] = False
    dllogger_logger_kwargs: Optional[DLLoggerParams] = field(default_factory=lambda: DLLoggerParams())
    create_clearml_logger: Optional[bool] = False
    clearml_logger_kwargs: Optional[ClearMLParams] = field(default_factory=lambda: ClearMLParams())
    create_neptune_logger: Optional[bool] = False
    neptune_logger_kwargs: Optional[Dict[Any, Any]] = None
    # Checkpointing parameters
    create_checkpoint_callback: Optional[bool] = True
    checkpoint_callback_params: Optional[CallbackParams] = field(default_factory=lambda: CallbackParams())
    create_early_stopping_callback: Optional[bool] = False
    early_stopping_callback_params: Optional[EarlyStoppingParams] = field(
        default_factory=lambda: EarlyStoppingParams()
    )
    create_preemption_callback: Optional[bool] = True
    # Additional exp_manager arguments
    files_to_copy: Optional[List[str]] = None
    # logs timing of train/val/test steps
    log_step_timing: Optional[bool] = True
    # log step time with nemo logger instead of lightning logger to avoid lightning logger overhead
    log_delta_step_timing: Optional[bool] = False
    step_timing_kwargs: Optional[StepTimingParams] = field(default_factory=lambda: StepTimingParams())
    # Configures creation of log files for different ranks
    log_local_rank_0_only: Optional[bool] = False
    log_global_rank_0_only: Optional[bool] = False
    # disable initial validation when resuming from a checkpoint saved during validation
    disable_validation_on_resume: Optional[bool] = True
    ema: Optional[EMAParams] = field(default_factory=lambda: EMAParams())
    # Wall clock time limit
    max_time_per_run: Optional[str] = None
    # time to sleep non 0 ranks during initialization
    seconds_to_sleep: float = 5
    # Straggler detection
    create_straggler_detection_callback: Optional[bool] = False
    straggler_detection_params: Optional[StragglerDetectionParams] = field(default_factory=StragglerDetectionParams)
    # Fault tolrance
    create_fault_tolerance_callback: Optional[bool] = False
    fault_tolerance: Optional[FaultToleranceParams] = field(default_factory=FaultToleranceParams)
    # logs TFLOPs per sec per gpu
    log_tflops_per_sec_per_gpu: Optional[bool] = True


class TimingCallback(Callback):
    """
    Logs execution time of train/val/test steps
    """

    def __init__(self, log_tokens_per_sec: bool = False, timer_kwargs={}):
        """init for TimitCallback

        Args:
            log_tokens_per_sec (bool, optional): _description_. Defaults to False.
            timer_kwargs (dict, optional): _description_. Defaults to {}.
        """
        self.log_tokens_per_sec = log_tokens_per_sec
        self.timer = timers.NamedTimer(**timer_kwargs)

    def _on_batch_start(self, name):
        """Setup the timer

        Args:
            name (_type_): name of timer
        """
        # reset only if we do not return mean of a sliding window
        if self.timer.buffer_size <= 0:
            self.timer.reset(name)

        if self.timer.is_active(name):
            logging.warning(
                f"Timer `{name}` was not correctly stopped, suggesting a "
                "possible issue. The timer will be reset for now."
            )
            self.timer.reset(name)

        self.timer.start(name)

    def _on_batch_end(self, name, pl_module):
        """end of the callback log

        Args:
            name (_type_): _description_
            pl_module (_type_): _description_
        """
        self.timer.stop(name)
        # Set the `batch_size=1` as WAR for `dataloader_iter`, which is not used for any metric
        pl_module.log(
            name + ' in s',
            torch.as_tensor(self.timer[name]),
            on_step=True,
            on_epoch=False,
            batch_size=1,
            prog_bar=(name == "train_step_timing"),
        )

    def on_train_batch_start(self, trainer, pl_module, batch, batch_idx):
        """wrapper

        Args:
            trainer (_type_): _description_
            pl_module (_type_): _description_
            batch (_type_): _description_
            batch_idx (_type_): _description_
        """
        self._on_batch_start("train_step_timing")

    def on_train_batch_end(self, trainer, pl_module, outputs, batch, batch_idx):
        """wrapper

        Args:
            trainer (_type_): _description_
            pl_module (_type_): _description_
            outputs (_type_): _description_
            batch (_type_): _description_
            batch_idx (_type_): _description_
        """
        self._on_batch_end("train_step_timing", pl_module)
        if self.log_tokens_per_sec:
            if "text" in batch:
                batch['tokens'] = batch['text']
            tokens_per_gpu = (
                (get_current_global_batch_size() // trainer.accumulate_grad_batches)
                * batch["tokens"].shape[1]
                / torch.distributed.get_world_size()
            )
            pl_module.log(
                "tokens_per_sec_per_gpu",
                tokens_per_gpu / (torch.as_tensor(self.timer["train_step_timing"])),
                on_step=True,
                on_epoch=False,
                batch_size=1,
                prog_bar=True,
            )

    def on_validation_batch_start(self, trainer, pl_module, batch, batch_idx, dataloader_idx=0):
        """on_validation_batch_start"""
        self._on_batch_start("validation_step_timing")

    def on_validation_batch_end(self, trainer, pl_module, outputs, batch, batch_idx, dataloader_idx=0):
        """on_validation_batch_end"""
        self._on_batch_end("validation_step_timing", pl_module)

    def on_test_batch_start(self, trainer, pl_module, batch, batch_idx, dataloader_idx=0):
        """on_test_batch_start"""
        self._on_batch_start("test_step_timing")

    def on_test_batch_end(self, trainer, pl_module, outputs, batch, batch_idx, dataloader_idx=0):
        """on_test_batch_end"""
        self._on_batch_end("test_step_timing", pl_module)

    def on_before_backward(self, trainer, pl_module, loss):
        """on_before_backward"""
        self._on_batch_start("train_backward_timing")

    def on_after_backward(self, trainer, pl_module):
        """on_after_backward"""
        self._on_batch_end("train_backward_timing", pl_module)


class DeltaTimingCallback(Callback):
    """
    Logs execution time of train/val/test steps using nemo logger. Calculates
    time from previous batch end to current batch end. This ensures accuracy.

    Note: step time will only be printed in stdout. If you have initialized
    loggers like TensorBoard, WandB, etc, step time will not be recorded there.
    Use this callback instead of 'TimingCallback' to avoid logging overhead with
    lightning logger used in the latter.
    """

    def __init__(self, timer_kwargs={}):
        """init

        Args:
            timer_kwargs (dict, optional): _description_. Defaults to {}.
        """
        self._sync_cuda = timer_kwargs.get("sync_cuda", False)
        self.timers = defaultdict(defaultdict)

    def _on_epoch_start(self, name, trainer, pl_module):
        """_on_epoch_start"""
        # synchronize pytorch cuda execution if supported
        if self._sync_cuda and torch.cuda.is_initialized():
            torch.cuda.synchronize()

        self.timers[name]["step"] = 0
        self.timers[name]["start"] = time.time()

    def _on_batch_end(self, name, trainer, pl_module):
        """_on_epoch_start"""
        # synchronize pytorch cuda execution if supported
        if self._sync_cuda and torch.cuda.is_initialized():
            torch.cuda.synchronize()

        end = time.time()
        dt = end - self.timers[name]["start"]
        logging.info(f'Step {self.timers[name]["step"]}: {name} in s={dt}')
        self.timers[name]["step"] += 1
        self.timers[name]["start"] = end

    def on_train_epoch_start(self, trainer, pl_module):
        """on_train_epoch_start"""
        self._on_epoch_start("train_step_timing in s", trainer, pl_module)

    def on_validation_epoch_start(self, trainer, pl_module):
        """on_validation_epoch_start"""
        self._on_epoch_start("validation_step_timing in s", trainer, pl_module)

    def on_train_batch_end(self, trainer, pl_module, outputs, batch, batch_idx):
        """on_train_batch_end"""
        self._on_batch_end("train_step_timing in s", trainer, pl_module)

    def on_validation_batch_end(self, trainer, pl_module, outputs, batch, batch_idx):
        """on_validation_batch_end"""
        self._on_batch_end("validation_step_timing in s", trainer, pl_module)


def exp_manager(trainer: 'lightning.pytorch.Trainer', cfg: Optional[Union[DictConfig, Dict]] = None) -> Optional[Path]:
    """
    exp_manager is a helper function used to manage folders for experiments. It follows the pytorch
    lightning paradigm of exp_dir/model_or_experiment_name/version. If the lightning trainer
    has a logger, exp_manager will get exp_dir, name, and version from the logger.
    Otherwise it will use the exp_dir and name arguments to create the logging
    directory. exp_manager also allows for explicit folder creation via explicit_log_dir.

    The version can be a datetime string or an integer. Datestime version can be disabled if
    use_datetime_version is set to False. It optionally creates TensorBoardLogger, WandBLogger,
    DLLogger, MLFlowLogger, ClearMLLogger, ModelCheckpoint objects from pytorch lightning.
    It copies sys.argv, and git information if available to the logging directory. It creates a
    log file for each process to log their output into.

    exp_manager additionally has a resume feature (resume_if_exists) which can be used to
    continuing training from the constructed log_dir. When you need to continue the training
    repeatedly (like on a cluster which you need multiple consecutive jobs), you need to avoid
    creating the version folders. Therefore from v1.0.0, when resume_if_exists is set to True,
    creating the version folders is ignored.

    Args:
        trainer (lightning.pytorch.Trainer): The lightning trainer.
        cfg (DictConfig, dict): Can have the following keys:

            - explicit_log_dir (str, Path): Can be used to override exp_dir/name/version folder
                creation.
                Defaults to None, which will use exp_dir, name, and version to construct the
                logging directory.
            - exp_dir (str, Path): The base directory to create the logging directory.
                Defaults to None, which logs to ./nemo_experiments.
            - name (str): The name of the experiment. Defaults to None which turns into "default"
                via name = name or "default".
            - version (str): The version of the experiment. Defaults to None which uses either a
                datetime string or lightning's TensorboardLogger system of using version_{int}.
            - use_datetime_version (bool): Whether to use a datetime string for version.
                Defaults to True.
            - resume_if_exists (bool): Whether this experiment is resuming from a previous run.
                If True, it sets trainer._checkpoint_connector._ckpt_path so that the trainer
                should auto-resume. exp_manager will move files under log_dir to log_dir/run_{int}.
                Defaults to False.
                From v1.0.0, when resume_if_exists is True, we would not create version folders to
                make it easier to find the log folder for next runs.
            - resume_past_end (bool): exp_manager errors out if resume_if_exists is True
                and a checkpoint matching ``*end.ckpt`` indicating a previous training run
                fully completed. This behaviour can be disabled, in which case the ``*end.ckpt``
                will be loaded by setting resume_past_end to True. Defaults to False.
            - resume_ignore_no_checkpoint (bool): exp_manager errors out if resume_if_exists is True
                and no checkpoint could be found. This behaviour can be disabled, in which case exp_manager
                will print a message and continue without restoring, by setting resume_ignore_no_checkpoint
                to True. Defaults to False.
            - resume_from_checkpoint (str): Can be used to specify a path to a specific checkpoint
                file to load from. This will override any checkpoint found when resume_if_exists
                is True. Defaults to None.
            - create_tensorboard_logger (bool): Whether to create a tensorboard logger and attach it
                to the pytorch lightning trainer. Defaults to True.
            - summary_writer_kwargs (dict): A dictionary of kwargs that can be passed to lightning's
                TensorboardLogger class. Note that log_dir is passed by exp_manager and cannot exist
                in this dict. Defaults to None.
            - create_wandb_logger (bool): Whether to create a Weights and Baises logger and attach it
                to the pytorch lightning trainer. Defaults to False.
            - wandb_logger_kwargs (dict): A dictionary of kwargs that can be passed to lightning's
                WandBLogger class. Note that name and project are required parameters if
                create_wandb_logger is True. Defaults to None.
            - create_mlflow_logger (bool): Whether to create an MLFlow logger and attach it to the
                pytorch lightning training. Defaults to False
            - mlflow_logger_kwargs (dict): optional parameters for the MLFlow logger
            - create_dllogger_logger (bool): Whether to create an DLLogger logger and attach it to the
                pytorch lightning training. Defaults to False
            - dllogger_logger_kwargs (dict): optional parameters for the DLLogger logger
            - create_clearml_logger (bool): Whether to create an ClearML logger and attach it to the
                pytorch lightning training. Defaults to False
            - clearml_logger_kwargs (dict): optional parameters for the ClearML logger
            - create_checkpoint_callback (bool): Whether to create a ModelCheckpoint callback and
                attach it to the pytorch lightning trainer. The ModelCheckpoint saves the top 3 models
                with the best "val_loss", the most recent checkpoint under ``*last.ckpt``, and the
                final checkpoint after training completes under ``*end.ckpt``.
                Defaults to True.
            - create_early_stopping_callback (bool): Flag to decide if early stopping should be used
                to stop training. Default is False. See EarlyStoppingParams dataclass above.
            - create_preemption_callback (bool): Flag to decide whether to enable preemption callback
                to save checkpoints and exit training immediately upon preemption. Default is True.
            - create_straggler_detection_callback (bool): Use straggler detection callback.
                Default is False.
            - create_fault_tolerance_callback (bool): Use fault tolerance callback. Default is False.
            - files_to_copy (list): A list of files to copy to the experiment logging directory.
                Defaults to None which copies no files.
            - log_local_rank_0_only (bool): Whether to only create log files for local rank 0.
                Defaults to False.
                Set this to True if you are using DDP with many GPUs and do not want many log files
                in your exp dir.
            - log_global_rank_0_only (bool): Whether to only create log files for global rank 0.
                Defaults to False.
                Set this to True if you are using DDP with many GPUs and do not want many log files
                in your exp dir.
            - max_time (str): The maximum wall clock time *per run*. This is intended to be used on
                clusters where you want a checkpoint to be saved after this specified time and be
                able to resume from that checkpoint. Defaults to None.
            - seconds_to_sleep (float): seconds to sleep non rank 0 processes for. Used to give
                enough time for rank 0 to initialize
            - train_time_interval (timedelta): pass an object of timedelta to save the model every
                timedelta. Defaults to None. (use _target_ with hydra to achieve this)

    returns:
        log_dir (Path): The final logging directory where logging files are saved. Usually the concatenation of
            exp_dir, name, and version.
    """
    # Add rank information to logger
    # Note: trainer.global_rank and trainer.is_global_zero are not set until trainer.fit, so have to hack around it
    local_rank = int(os.environ.get("LOCAL_RANK", 0))
    global_rank = trainer.node_rank * trainer.num_devices + local_rank
    logging.rank = global_rank

    if cfg is None:
        logging.error("exp_manager did not receive a cfg argument. It will be disabled.")
        return
    if trainer.fast_dev_run:
        logging.info("Trainer was called with fast_dev_run. exp_manager will return without any functionality.")
        return

    # Ensure passed cfg is compliant with ExpManagerConfig
    schema = OmegaConf.structured(ExpManagerConfig)
    # TODO: remove this check
    if is_global_rank_zero():
        logging.info('ExpManager schema')
        logging.info(schema)
    if isinstance(cfg, dict):
        cfg = OmegaConf.create(cfg)
    elif not isinstance(cfg, DictConfig):
        raise ValueError(f"cfg was type: {type(cfg)}. Expected either a dict or a DictConfig")
    cfg = OmegaConf.create(OmegaConf.to_container(cfg, resolve=True))
    cfg = OmegaConf.merge(schema, cfg)  # type: ExpManagerConfig

    # Ensures that trainer options are compliant with NeMo and exp_manager arguments
    error_checks(trainer, cfg)

    log_dir, exp_dir, name, version = get_log_dir(
        trainer=trainer,
        exp_dir=cfg.exp_dir,
        name=cfg.name,
        version=cfg.version,
        explicit_log_dir=cfg.explicit_log_dir,
        use_datetime_version=cfg.use_datetime_version,
        resume_if_exists=cfg.resume_if_exists,
    )

    check_resume(
        trainer,
        log_dir,
        cfg.resume_if_exists,
        cfg.resume_past_end,
        cfg.resume_ignore_no_checkpoint,
        cfg.checkpoint_callback_params.dirpath,
        cfg.resume_from_checkpoint,
    )

    checkpoint_name = name
    # If name returned from get_log_dir is "", use cfg.name for checkpointing
    if checkpoint_name is None or checkpoint_name == '':
        checkpoint_name = cfg.name or "default"

    # Set mlflow name if it's not set, before the main name is erased
    if cfg.create_mlflow_logger and (not cfg.mlflow_logger_kwargs.get("experiment_name", None)):
        cfg.mlflow_logger_kwargs.experiment_name = cfg.name
        logging.warning(
            'mlflow logger specified but no experiment name set. Using the same as Tensorboard: %s',
            cfg.mlflow_logger_kwargs.experiment_name,
        )

    cfg.name = name  # Used for configure_loggers so that the log_dir is properly set even if name is ""
    cfg.version = version

    # update app_state with log_dir, exp_dir, etc
    app_state = AppState()
    app_state.log_dir = log_dir
    app_state.exp_dir = exp_dir
    app_state.name = name
    app_state.version = version
    app_state.checkpoint_name = checkpoint_name
    app_state.create_checkpoint_callback = cfg.create_checkpoint_callback
    app_state.checkpoint_callback_params = cfg.checkpoint_callback_params

    # Create the logging directory if it does not exist
    # Cannot limit creation to global zero as all ranks write to own log file
    os.makedirs(log_dir, exist_ok=True)
    logging.info(f'Experiments will be logged at {log_dir}')
    trainer._default_root_dir = log_dir

    if cfg.log_local_rank_0_only is True and cfg.log_global_rank_0_only is True:
        raise ValueError(
            "Cannot set both log_local_rank_0_only and log_global_rank_0_only to True."
            "Please set either one or neither."
        )

    # This is set if the env var NEMO_TESTING is set to True.
    nemo_testing = get_envbool(NEMO_ENV_VARNAME_TESTING, False)

    # Handle logging to file
    log_file = log_dir / f'nemo_log_globalrank-{global_rank}_localrank-{local_rank}.txt'
    if cfg.log_local_rank_0_only is True and not nemo_testing:
        if local_rank == 0:
            logging.add_file_handler(log_file)
    elif cfg.log_global_rank_0_only is True and not nemo_testing:
        if global_rank == 0:
            logging.add_file_handler(log_file)
    else:
        # Logs on all ranks.
        logging.add_file_handler(log_file)

    # For some reason, LearningRateLogger requires trainer to have a logger. Safer to create logger on all ranks
    # not just global rank 0.
    if (
        cfg.create_tensorboard_logger
        or cfg.create_wandb_logger
        or cfg.create_mlflow_logger
        or cfg.create_dllogger_logger
        or cfg.create_clearml_logger
        or cfg.create_neptune_logger
    ):
        configure_loggers(
            trainer,
            exp_dir,
            log_dir,
            cfg.name,
            cfg.version,
            cfg.checkpoint_callback_params,
            cfg.create_tensorboard_logger,
            cfg.summary_writer_kwargs,
            cfg.create_wandb_logger,
            cfg.wandb_logger_kwargs,
            cfg.create_mlflow_logger,
            cfg.mlflow_logger_kwargs,
            cfg.create_dllogger_logger,
            cfg.dllogger_logger_kwargs,
            cfg.create_clearml_logger,
            cfg.clearml_logger_kwargs,
            cfg.create_neptune_logger,
            cfg.neptune_logger_kwargs,
        )

    # add loggers timing callbacks
    if cfg.log_delta_step_timing:
        timing_callback = DeltaTimingCallback(timer_kwargs=cfg.step_timing_kwargs or {})
        trainer.callbacks.insert(0, timing_callback)
    elif cfg.log_step_timing:
        timing_callback = TimingCallback(timer_kwargs=cfg.step_timing_kwargs or {})
        trainer.callbacks.insert(0, timing_callback)

    if cfg.ema.enable:
        ema_callback = EMA(
            decay=cfg.ema.decay,
            validate_original_weights=cfg.ema.validate_original_weights,
            cpu_offload=cfg.ema.cpu_offload,
            every_n_steps=cfg.ema.every_n_steps,
        )
        trainer.callbacks.append(ema_callback)

    if cfg.create_early_stopping_callback:
        early_stop_callback = EarlyStopping(**cfg.early_stopping_callback_params)
        trainer.callbacks.append(early_stop_callback)

    if cfg.create_checkpoint_callback:
        configure_checkpointing(
            trainer,
            log_dir,
            checkpoint_name,
            cfg.resume_if_exists,
            cfg.checkpoint_callback_params,
            cfg.create_preemption_callback,
        )

    if cfg.disable_validation_on_resume:
        # extend training loop to skip initial validation when resuming from checkpoint
        configure_no_restart_validation_training_loop(trainer)
    # Setup a stateless timer for use on clusters.
    if cfg.max_time_per_run is not None:
        found_ptl_timer = False
        for idx, callback in enumerate(trainer.callbacks):
            if isinstance(callback, Timer):
                # NOTE: PTL does not expose a `trainer.max_time`. By the time we are in this function,
                # PTL has already setup a timer if the user specifies `trainer.max_time` so best we
                # can do is replace that.
                # Working: If only `trainer.max_time` is set - it behaves as a normal PTL timer.
                # If only `exp_manager.max_time_per_run` is set - it behaves as a StateLessTimer.
                # If both are set, it also behaves as a StateLessTimer.
                logging.warning(
                    'Found a PTL Timer callback, replacing with a StatelessTimer callback. '
                    'This will happen if you set trainer.max_time as well as exp_manager.max_time_per_run.'
                )
                trainer.callbacks[idx] = StatelessTimer(cfg.max_time_per_run)
                found_ptl_timer = True
                break

        if not found_ptl_timer:
            trainer.max_time = cfg.max_time_per_run
            trainer.callbacks.append(StatelessTimer(cfg.max_time_per_run))

    if cfg.create_straggler_detection_callback:
        if HAVE_STRAGGLER_DET:
            logging.info("Enabling straggler detection...")
            straggler_det_args_dict = dict(cfg.straggler_detection_params)
            straggler_det_callback = StragglerDetectionCallback(**straggler_det_args_dict)
            trainer.callbacks.append(straggler_det_callback)
        else:
            raise ValueError(
                "`create_straggler_detection_callback` is True, but there is no Straggler Det. " "package installed."
            )

    if cfg.create_fault_tolerance_callback:
        if HAVE_FT:
            logging.info("Enabling fault tolerance...")
            ft_params = cfg.fault_tolerance
            # job failures are handled by the ft_launcher,
            # here we only need to know if the autoresume is enabled.
            ft_use_autoresume = ft_params.max_subsequent_job_failures > 0
            fault_tol_callback = FaultToleranceCallback(
                # log_dir is "<run name>/results/"
                exp_dir=Path(log_dir).parent,
                autoresume=ft_use_autoresume,
                calculate_timeouts=ft_params.calculate_timeouts,
                simulated_fault_params=ft_params.simulated_fault,
            )
            trainer.callbacks.append(fault_tol_callback)
        else:
            raise ValueError(
                'FaultToleranceCallback was enabled with create_fault_tolerance_callback, '
                'but fault_tolerance package is not installed.'
            )

    if cfg.log_tflops_per_sec_per_gpu:
        logging.info(
            "TFLOPs per sec per GPU will be calculated, conditioned on supported models. "
            "Defaults to -1 upon failure."
        )

    if is_global_rank_zero():
        # Move files_to_copy to folder and add git information if present
        if cfg.files_to_copy:
            for _file in cfg.files_to_copy:
                copy(Path(_file), log_dir)

        # Create files for cmd args and git info
        with open(log_dir / 'cmd-args.log', 'w', encoding='utf-8') as _file:
            _file.write(" ".join(sys.argv))

        # Try to get git hash
        git_repo, git_hash = get_git_hash()
        if git_repo:
            with open(log_dir / 'git-info.log', 'a', encoding='utf-8') as _file:
                _file.write(f'commit hash: {git_hash}')
                _file.write(get_git_diff())

        # Add err_file logging to global_rank zero
        logging.add_err_file_handler(log_dir / 'nemo_error_log.txt')

        # Add lightning file logging to global_rank zero
        add_filehandlers_to_pl_logger(log_dir / 'lightning_logs.txt', log_dir / 'nemo_error_log.txt')

    elif trainer.num_nodes * trainer.num_devices > 1:
        # sleep other ranks so rank 0 can finish
        # doing the initialization such as moving files
        time.sleep(cfg.seconds_to_sleep)

    add_handlers_to_mcore_logger()

    return log_dir


def error_checks(trainer: 'lightning.pytorch.Trainer', cfg: Optional[Union[DictConfig, Dict]] = None):
    """
    Checks that the passed trainer is compliant with NeMo and exp_manager's passed configuration.
    Checks that:
        - Throws error when hydra has changed the working directory.
          This causes issues with lightning's DDP
        - Throws error when trainer has loggers defined but create_tensorboard_logger
            or create_wandB_logger or create_mlflow_logger or create_dllogger_logger is True
        - Prints error messages when 1) run on multi-node and not Slurm, and
            2) run on multi-gpu without DDP
    """
    if HydraConfig.initialized() and get_original_cwd() != os.getcwd():
        raise ValueError(
            "Hydra changed the working directory. This interferes with ExpManger's functionality."
            " Please pass hydra.run.dir=. to your python script."
        )
    if trainer.logger is not None and (
        cfg.create_tensorboard_logger or cfg.create_wandb_logger or cfg.create_mlflow_logger
    ):
        raise LoggerMisconfigurationError(
            "The pytorch lightning trainer that was passed to exp_manager contained a logger, "
            "and either "
            f"create_tensorboard_logger: {cfg.create_tensorboard_logger} or create_wandb_logger: "
            f"{cfg.create_wandb_logger} or create_mlflow_logger: {cfg.create_mlflow_logger}"
            f"or create_dllogger_logger: {cfg.create_mlflow_logger} was set to True. "
            "These can only be used if trainer does not already have a logger."
        )
    if trainer.num_nodes > 1 and not check_slurm(trainer):
        logging.error(
            "You are running multi-node training without SLURM handling the processes."
            " Please note that this is not tested in NeMo and could result in errors."
        )
    if trainer.num_devices > 1 and not isinstance(trainer.strategy, DDPStrategy):
        logging.error(
            "You are running multi-gpu without ddp.Please note that this is not tested in NeMo and "
            "could result in errors."
        )


def _filter_out_unfinished_checkpoints(checkpoint_paths: Collection[Union[Path, str]]) -> Collection[Union[Path, str]]:
    """_filter_out_unfinished_checkpoints"""
    res = []
    for chkpt_path in checkpoint_paths:
        if NeMoModelCheckpoint.is_checkpoint_unfinished(chkpt_path):
            logging.warning(
                f'Checkpoint {chkpt_path} has the unfinished marker set - skipped while looking ' 'for the last one.'
            )
        else:
            res.append(chkpt_path)
    return res


def check_resume(
    trainer: 'lightning.pytorch.Trainer',
    log_dir: str,
    resume_if_exists: bool = False,
    resume_past_end: bool = False,
    resume_ignore_no_checkpoint: bool = False,
    dirpath: str = None,
    resume_from_checkpoint: str = None,
):
    """Checks that resume=True was used correctly with the arguments pass to exp_manager. Sets
    trainer._checkpoint_connector._ckpt_path as necessary.

    Returns:
        log_dir (Path): The log_dir
        exp_dir (str): The base exp_dir without name nor version
        name (str): The name of the experiment
        version (str): The version of the experiment

    Raises:
        NotFoundError: If resume is True, resume_ignore_no_checkpoint is False, and checkpoints
        could not be found.
        ValueError: If resume is True, and there were more than 1 checkpoint could found.
    """

    if not log_dir:
        raise ValueError(f"Resuming requires the log_dir {log_dir} to be passed to exp_manager")

    # is_s3_url from here has no dependency requirements
    from nemo.utils.s3_dirpath_utils import is_s3_url

    try:
        # when using an s3 dirpath, we rely on optional dependencies in the S3Utils class.
        if dirpath is not None and is_s3_url(dirpath):
            from nemo.utils.s3_utils import S3Utils
    except ImportError as err:
        return False, "Detected S3 dirpath while missing required dependencies.\n{}\n".format(
            err.output.decode("utf-8")
        )

    checkpoint = None
    if resume_from_checkpoint:
        checkpoint = resume_from_checkpoint
    if resume_if_exists:
        '''
        attach valid checkpoint path to trainer if current rank is rank zero of any
        data parallel groups this limit to only global rank 0 process calling s3,
        instead of all processes calling s3
        '''

        # If we are using S3 checkpointing, we want check_resume to only execute on a single rank
        # to avoid throttling S3.

        if is_global_rank_zero() or not (is_s3_url(dirpath) and is_multistorageclient_url(dirpath)):
            checkpoint_dir_exists = False
            if is_s3_url(dirpath):
                checkpoint_dir = dirpath
                checkpoint_dir_exists = S3Utils.s3_path_exists(checkpoint_dir, match_directory=True)

                if checkpoint_dir_exists:
                    # max number of last.ckpt files: save_last_k_checkpoints * tp * pp = 5*8*40.
                    # If optim states is saved distributedly, multiply by dp_size
                    all_keys = S3Utils.find_files_with_suffix(checkpoint_dir, suffix=None, return_key_only=False)
                    end_checkpoints = [k for k in all_keys if k.endswith('end.ckpt')]
                    last_checkpoints = [k for k in all_keys if k.endswith('last.ckpt')]
                else:
                    end_checkpoints = []
                    last_checkpoints = []
            elif is_multistorageclient_url(dirpath):
                msc = import_multistorageclient()
                checkpoint_dir = dirpath
                all_keys = msc.glob(f"{dirpath}**/*.ckpt")
                checkpoint_dir_exists = True if all_keys else False
                if all_keys:
                    end_checkpoints = sorted([k for k in all_keys if k.endswith('end.ckpt')], reverse=True)
                    last_checkpoints = sorted([k for k in all_keys if k.endswith('last.ckpt')], reverse=True)
                else:
                    end_checkpoints = []
                    last_checkpoints = []
            else:  # default non-s3 implementation
                # Use <log_dir>/checkpoints/ unless `dirpath` is set
                checkpoint_dir = Path(dirpath) if dirpath else Path(Path(log_dir) / "checkpoints")
                checkpoint_dir_exists = checkpoint_dir.exists()

                # when using distributed checkpointing, checkpoint_dir is a directory of directories
                # we check for this here
                dist_checkpoints = [d for d in list(checkpoint_dir.glob("*")) if d.is_dir()]
                end_dist_checkpoints = [d for d in dist_checkpoints if d.match("*end")]
                last_dist_checkpoints = [d for d in dist_checkpoints if d.match("*last")]

                end_checkpoints = (
                    end_dist_checkpoints if end_dist_checkpoints else list(checkpoint_dir.rglob("*end.ckpt"))
                )
                end_chkpt_cnt = len(end_checkpoints)
                end_checkpoints = _filter_out_unfinished_checkpoints(end_checkpoints)
                finished_end_chkpt_cnt = len(end_checkpoints)
                if end_chkpt_cnt > 0 and finished_end_chkpt_cnt == 0:
                    raise ValueError(
                        "End checkpoint is unfinished and cannot be used to resume the training."
                        " Please remove the checkpoint manually to avoid unexpected cosequences, such as"
                        " restarting from scratch."
                    )

                last_checkpoints = (
                    last_dist_checkpoints if last_dist_checkpoints else list(checkpoint_dir.rglob("*last.ckpt"))
                )
                last_chkpt_cnt = len(last_checkpoints)
                last_checkpoints = _filter_out_unfinished_checkpoints(last_checkpoints)
                finished_last_chkpt_cnt = len(last_checkpoints)
                if last_chkpt_cnt > 0 and finished_last_chkpt_cnt == 0:
                    raise ValueError(
                        "Last checkpoint is unfinished and cannot be used to resume the training."
                        " Please remove the checkpoint manually to avoid unexpected cosequences, "
                        " such as restarting from scratch. Hint: Iteration number can be added "
                        " to the checkpoint name pattern"
                        " to maximize chance that there is at least one finished last checkpoint to"
                        " resume from."
                    )

            if not checkpoint_dir_exists or (not len(end_checkpoints) > 0 and not len(last_checkpoints) > 0):
                if resume_ignore_no_checkpoint:
                    warn = (
                        f"There were no checkpoints found in checkpoint_dir or no checkpoint "
                        f"folder at checkpoint_dir :{checkpoint_dir}. "
                    )
                    if checkpoint is None:
                        warn += "Training from scratch."
                    elif checkpoint == resume_from_checkpoint:
                        warn += f"Training from {resume_from_checkpoint}."
                    logging.warning(warn)
                else:
                    raise NotFoundError(
                        f"There were no checkpoints found in checkpoint_dir or no checkpoint "
                        f"folder at checkpoint_dir :{checkpoint_dir}. Cannot resume."
                    )
            elif len(end_checkpoints) > 0:
                if resume_past_end:
                    if len(end_checkpoints) > 1:
                        if 'mp_rank' in str(end_checkpoints[0]):
                            checkpoint = end_checkpoints[0]
                        else:
                            raise ValueError(f"Multiple checkpoints {end_checkpoints} that matches *end.ckpt.")
                else:
                    raise ValueError(
                        f"Found {end_checkpoints[0]} indicating that the last training run has already completed."
                    )
            elif len(last_checkpoints) > 1:
                if any([s for s in ['mp_rank', 'tp_rank', 'fsdp_shard'] if s in str(last_checkpoints[0])]):
                    checkpoint = last_checkpoints[0]
                    checkpoint = uninject_model_parallel_rank(checkpoint)
                else:
                    raise ValueError(f"Multiple checkpoints {last_checkpoints} that matches *last.ckpt.")
            else:
                checkpoint = last_checkpoints[0]

    # PTL 2.0 supports ckpt_path instead of resume_from_checkpoint as the trainer flag
    if checkpoint is not None:
        trainer.ckpt_path = str(checkpoint)
        logging.info(f'Resuming training from checkpoint: {trainer.ckpt_path}')

    if is_global_rank_zero():
        # Check to see if any files exist that need to be moved
        files_to_move = []
        if Path(log_dir).exists():
            for child in Path(log_dir).iterdir():
                if child.is_file() and not child.name.startswith("events.out.tfevents"):
                    files_to_move.append(child)

        if len(files_to_move) > 0:
            # Move old files to a new folder
            other_run_dirs = Path(log_dir).glob("run_*")
            run_count = 0
            for fold in other_run_dirs:
                if fold.is_dir():
                    run_count += 1
            new_run_dir = Path(Path(log_dir) / f"run_{run_count}")
            new_run_dir.mkdir()
            for _file in files_to_move:
                move(str(_file), str(new_run_dir))


def check_explicit_log_dir(
    trainer: 'lightning.pytorch.Trainer', explicit_log_dir: Union[Path, str], exp_dir: str, name: str, version: str
) -> Tuple[Path, str, str, str]:
    """Checks that the passed arguments are compatible with explicit_log_dir.

    Returns:
        log_dir (Path): the log_dir
        exp_dir (str): the base exp_dir without name nor version
        name (str): The name of the experiment
        version (str): The version of the experiment

    Raise:
        LoggerMisconfigurationError
    """
    if trainer.logger is not None:
        raise LoggerMisconfigurationError(
            "The pytorch lightning trainer that was passed to exp_manager contained a "
            "logger and explicit_log_dir: "
            f"{explicit_log_dir} was pass to exp_manager. "
            "Please remove the logger from the lightning trainer."
        )
    # Checking only (explicit_log_dir) vs (exp_dir and version).
    # The `name` will be used as the actual name of checkpoint/archive.
    if exp_dir or version:
        logging.error(
            f"exp_manager received explicit_log_dir: {explicit_log_dir} and at least "
            f"one of exp_dir: {exp_dir}, "
            f"or version: {version}. Please note that exp_dir, name, and version will be ignored."
        )
    if is_global_rank_zero() and Path(explicit_log_dir).exists():
        logging.warning(f"Exp_manager is logging to {explicit_log_dir}, but it already exists.")
    return Path(explicit_log_dir), str(explicit_log_dir), "", ""


def get_log_dir(
    trainer: 'lightning.pytorch.Trainer',
    exp_dir: str = None,
    name: str = None,
    version: str = None,
    explicit_log_dir: str = None,
    use_datetime_version: bool = True,
    resume_if_exists: bool = False,
) -> Tuple[Path, str, str, str]:
    """
    Obtains the log_dir used for exp_manager.

    Returns:
        log_dir (Path): the log_dir
        exp_dir (str): the base exp_dir without name nor version
        name (str): The name of the experiment
        version (str): The version of the experiment
        explicit_log_dir (str): The explicit path to the log folder. Defaults to False.
        use_datetime_version (bool): Uses date and time as the version of the log folder.
            Defaults to True.
        resume_if_exists (bool): if resume_if_exists of the exp_manager's config is enabled or not.
            When enabled, the version folders would not get created.

    Raise:
        LoggerMisconfigurationError: If trainer is incompatible with arguments
        NotFoundError: If resume is True, resume_ignore_no_checkpoint is False, and checkpoints
            could not be found.
        ValueError: If resume is True, and there were more than 1 checkpoint could found.
    """
    if explicit_log_dir:  # If explicit log_dir was passed, short circuit
        return check_explicit_log_dir(trainer, explicit_log_dir, exp_dir, name, version)

    # Default exp_dir to ./nemo_experiments if None was passed
    _exp_dir = exp_dir
    if exp_dir is None:
        _exp_dir = str(Path.cwd() / 'nemo_experiments')

    # If the user has already defined a logger for the trainer,
    # use the logger defaults for logging directory
    if trainer.logger is not None:
        if trainer.logger.save_dir:
            if exp_dir:
                raise LoggerMisconfigurationError(
                    "The pytorch lightning trainer that was passed to exp_manager contained a "
                    "logger, the logger's "
                    f"save_dir was not None, and exp_dir ({exp_dir}) was not None. "
                    "If trainer.logger.save_dir "
                    "exists, exp_manager will use trainer.logger.save_dir as the "
                    "logging directory and exp_dir "
                    "must be None."
                )
            _exp_dir = trainer.logger.save_dir
        if name:
            raise LoggerMisconfigurationError(
                "The pytorch lightning trainer that was passed to exp_manager "
                "contained a logger, and name: "
                f"{name} was also passed to exp_manager. If the trainer contains a "
                "logger, exp_manager will use trainer.logger.name, and name passed "
                "to exp_manager must be None."
            )
        name = trainer.logger.name
        version = f"version_{trainer.logger.version}"
    # Use user-defined exp_dir, project_name, exp_name, and versioning options
    else:
        name = name or "default"
        version = version or os.environ.get(NEMO_ENV_VARNAME_VERSION, None)

        if not version:
            if resume_if_exists:
                logging.warning(
                    "No version folders would be created under the log folder as " "'resume_if_exists' is enabled."
                )
                version = None
            elif is_global_rank_zero():
                if use_datetime_version:
                    version = time.strftime('%Y-%m-%d_%H-%M-%S')
                else:
                    tensorboard_logger = TensorBoardLogger(save_dir=Path(_exp_dir), name=name, version=version)
                    version = f"version_{tensorboard_logger.version}"
                os.environ[NEMO_ENV_VARNAME_VERSION] = "" if version is None else version

    log_dir = Path(_exp_dir) / Path(str(name)) / Path("" if version is None else str(version))
    return log_dir, str(_exp_dir), name, version


def get_git_hash():
    """
    Helper function that tries to get the commit hash if running inside a git folder

    returns:
        Bool: Whether the git subprocess ran without error
        str: git subprocess output or error message
    """
    try:
        return (
            True,
            subprocess.check_output(['git', 'rev-parse', 'HEAD'], stderr=subprocess.STDOUT).decode(),
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as err:
        return False, "{}\n".format(err)


def get_git_diff():
    """
    Helper function that tries to get the git diff if running inside a git folder

    returns:
        Bool: Whether the git subprocess ran without error
        str: git subprocess output or error message
    """
    try:
        return subprocess.check_output(['git', 'diff'], stderr=subprocess.STDOUT).decode()
    except subprocess.CalledProcessError as err:
        return "{}\n".format(err.output.decode("utf-8"))


def configure_loggers(
    trainer: 'lightning.pytorch.Trainer',
    exp_dir: [Path, str],
    log_dir: [Path, str],
    name: str,
    version: str,
    checkpoint_callback_params: dict,
    create_tensorboard_logger: bool,
    summary_writer_kwargs: dict,
    create_wandb_logger: bool,
    wandb_kwargs: dict,
    create_mlflow_logger: bool,
    mlflow_kwargs: dict,
    create_dllogger_logger: bool,
    dllogger_kwargs: dict,
    create_clearml_logger: bool,
    clearml_kwargs: dict,
    create_neptune_logger: bool,
    neptune_kwargs: dict,
):
    """
    Creates TensorboardLogger and/or WandBLogger / MLFlowLogger / DLlogger / ClearMLLogger
    and attach them to trainer.
    Raises ValueError if summary_writer_kwargs or wandb_kwargs are misconfigured.
    """
    # Potentially create tensorboard logger and/or WandBLogger / MLFlowLogger / DLLogger
    logger_list = []
    if create_tensorboard_logger:
        if summary_writer_kwargs is None:
            summary_writer_kwargs = {}
        elif "log_dir" in summary_writer_kwargs:
            raise ValueError(
                "You cannot pass `log_dir` as part of `summary_writer_kwargs`. `log_dir` "
                "is handled by lightning's "
                "TensorBoardLogger logger."
            )
        tensorboard_logger = TensorBoardLogger(save_dir=exp_dir, name=name, version=version, **summary_writer_kwargs)
        logger_list.append(tensorboard_logger)
        logging.info("TensorboardLogger has been set up")

    if create_wandb_logger:
        if wandb_kwargs is None:
            wandb_kwargs = {}
        if "name" not in wandb_kwargs and "project" not in wandb_kwargs:
            raise ValueError("name and project are required for wandb_logger")

        # Update the wandb save_dir
        if wandb_kwargs.get('save_dir', None) is None:
            wandb_kwargs['save_dir'] = exp_dir
        os.makedirs(wandb_kwargs['save_dir'], exist_ok=True)
        wandb_logger = WandbLogger(version=version, **wandb_kwargs)

        logger_list.append(wandb_logger)
        logging.info("WandBLogger has been set up")

    if create_mlflow_logger:
        mlflow_logger = MLFlowLogger(run_name=version, **mlflow_kwargs)

        logger_list.append(mlflow_logger)
        logging.info("MLFlowLogger has been set up")

    if create_dllogger_logger:
        dllogger_logger = DLLogger(**dllogger_kwargs)

        logger_list.append(dllogger_logger)
        logging.info("DLLogger has been set up")

    if create_clearml_logger:
        clearml_logger = ClearMLLogger(
            clearml_cfg=clearml_kwargs,
            log_dir=log_dir,
            prefix=name,
            save_best_model=checkpoint_callback_params.save_best_model,
        )

        logger_list.append(clearml_logger)
        logging.info("ClearMLLogger has been set up")

    if create_neptune_logger:
        if neptune_kwargs is None:
            neptune_kwargs = {}
        if "name" not in neptune_kwargs and "project" not in neptune_kwargs:
            raise ValueError("name and project are required for neptune_logger")
        if "api_key" not in neptune_kwargs and not os.getenv("NEPTUNE_API_TOKEN", None):
            raise ValueError(
                "either api_key should be set in neptune_kwargs or NEPTUNE_API_TOKEN should "
                "be set in environment variable for neptune_logger"
            )
        neptune_logger = NeptuneLogger(**neptune_kwargs)

        logger_list.append(neptune_logger)
        logging.info("NeptuneLogger has been set up")

    trainer._logger_connector.configure_logger(logger_list)


class NeMoCheckpointConnector(_CheckpointConnector):
    """
    Wrapper around Lightning's _CheckpointConnector to use broadcasted checkpoint path in
    distributed training settings to pre-load checkpoint.
    """

    def resume_start(self, checkpoint_path=None) -> None:
        """resume_start"""
        checkpoint_path = self.trainer.ckpt_path
        if checkpoint_path is not None:
            logging.info(f'Resuming from checkpoint {checkpoint_path}, rank {torch.distributed.get_rank()}')
        start_time = time.perf_counter()
        super().resume_start(checkpoint_path)
        if checkpoint_path is not None:
            logging.info(
                'Time elapsed loading checkpoint/optimizer states: '
                f'{(time.perf_counter() - start_time):.2f} seconds, '
                f'rank {torch.distributed.get_rank()}'
            )


def configure_checkpointing(
    trainer: 'lightning.pytorch.Trainer',
    log_dir: Path,
    name: str,
    resume: bool,
    params: 'DictConfig',
    create_preemption_callback: bool,
):
    """Adds ModelCheckpoint to trainer. Raises CheckpointMisconfigurationError if trainer
    already has a ModelCheckpoint callback
    """
    for callback in trainer.callbacks:
        if isinstance(callback, ModelCheckpoint):
            raise CheckpointMisconfigurationError(
                "The pytorch lightning trainer that was passed to exp_manager "
                "contained a ModelCheckpoint "
                "and create_checkpoint_callback was set to True. "
                "Please either set create_checkpoint_callback "
                "to False, or remove ModelCheckpoint from the lightning trainer"
            )
    # Create the callback and attach it to trainer
    if "filepath" in params:
        if params.filepath is not None:
            logging.warning("filepath is deprecated. Please switch to dirpath and filename instead")
            if params.dirpath is None:
                params.dirpath = Path(params.filepath).parent
            if params.filename is None:
                params.filename = Path(params.filepath).name
        with open_dict(params):
            del params["filepath"]
    if params.dirpath is None:
        params.dirpath = Path(log_dir / 'checkpoints')
    if params.filename is None:
        params.filename = f'{name}--{{{params.monitor}:.4f}}-{{epoch}}'
    if params.prefix is None:
        params.prefix = name
    if params.always_save_nemo:
        app_state = AppState()
        if (
            (app_state.tensor_model_parallel_size is not None and app_state.tensor_model_parallel_size > 1)
            or (app_state.pipeline_model_parallel_size is not None and app_state.pipeline_model_parallel_size > 1)
            or (app_state.context_parallel_size is not None and app_state.context_parallel_size > 1)
        ):
            raise LoggerMisconfigurationError(
                "always_save_nemo is set to True, please ensure that model parallel is not used."
                f"tensor_model_parallel_size: {app_state.tensor_model_parallel_size},"
                f"pipeline_model_parallel_size: {app_state.pipeline_model_parallel_size},"
                f"context_parallel_size: {app_state.context_parallel_size},"
            )

    NeMoModelCheckpoint.CHECKPOINT_NAME_LAST = params.filename + '-last'

    logging.debug(params.dirpath)
    logging.debug(params.filename)
    logging.debug(params.prefix)

    if "val" in params.monitor:
        if (
            trainer.max_epochs is not None
            and trainer.max_epochs != -1
            and trainer.max_epochs < trainer.check_val_every_n_epoch
        ):
            logging.error(
                "The checkpoint callback was told to monitor a validation value but "
                "trainer.max_epochs("
                f"{trainer.max_epochs}) was less than "
                f"trainer.check_val_every_n_epoch({trainer.check_val_every_n_epoch}"
                f"). It is very likely this run will fail with "
                f"ModelCheckpoint(monitor='{params.monitor}') not found "
                "in the returned metrics. Please ensure that validation is run within trainer.max_epochs."
            )
        elif trainer.max_steps is not None and trainer.max_steps != -1:
            logging.warning(
                "The checkpoint callback was told to monitor a validation value and trainer's"
                " max_steps was set to "
                f"{trainer.max_steps}. Please ensure that max_steps will run for at least "
                f"{trainer.check_val_every_n_epoch} epochs to ensure that checkpointing"
                " will not error out."
            )

    checkpoint_callback = NeMoModelCheckpoint(n_resume=resume, **params)
    checkpoint_callback.last_model_path = trainer.ckpt_path or ""
    if 'mp_rank' in checkpoint_callback.last_model_path or 'tp_rank' in checkpoint_callback.last_model_path:
        checkpoint_callback.last_model_path = uninject_model_parallel_rank(checkpoint_callback.last_model_path)
    trainer.callbacks.append(checkpoint_callback)
    if create_preemption_callback:
        # Check if cuda is avialable as preemption is supported only on GPUs
        if torch.cuda.is_available():
            # By default PreemptionCallback handles SIGTERM. To handle other signals pass the
            # signal in the call as below:
            # PreemptionCallback(checkpoint_callback, signal.SIGCHLD)
            preemption_callback = PreemptionCallback(checkpoint_callback)
            trainer.callbacks.append(preemption_callback)
        else:
            logging.info("Preemption is supported only on GPUs, disabling preemption")


def check_slurm(trainer):
    """check_slurm"""
    try:
        return trainer.accelerator_connector.is_slurm_managing_tasks
    except AttributeError:
        return False


class StatelessTimer(Timer):
    """Extension of PTL timers to be per run."""

    def __init__(
        self,
        duration: timedelta = None,
        interval: str = Interval.step,
        verbose: bool = True,
    ) -> None:
        """stateless timer

        Args:
            duration (timedelta, optional): _description_. Defaults to None.
            interval (str, optional): _description_. Defaults to Interval.step.
            verbose (bool, optional): _description_. Defaults to True.
        """
        super().__init__(duration, interval, verbose)

    # Override PTL Timer's state dict to not store elapsed time information so that we can
    # restore and continue training.
    def state_dict(self) -> Dict[str, Any]:
        """state_dict"""
        return {}

    def load_state_dict(self, state_dict: Dict[str, Any]) -> None:
        """load_state_dict"""
        return

    def _check_time_remaining(self, trainer: lightning.pytorch.Trainer) -> None:
        """_check_time_remaining"""
        super()._check_time_remaining(trainer)
        if trainer.should_stop:
            checkpoint_callback: Optional[NeMoModelCheckpoint] = trainer.checkpoint_callback
            if checkpoint_callback:
                monitor_candidates = checkpoint_callback._monitor_candidates(trainer)
                checkpoint_callback._save_last_checkpoint(trainer, monitor_candidates)
            # Throw this exception to signal to Lightning to terminate gracefully.
            from lightning.pytorch.utilities.exceptions import _TunerExitException

            raise _TunerExitException()


def configure_no_restart_validation_training_loop(trainer: lightning.pytorch.Trainer) -> None:
    """configure_no_restart_validation_training_loop"""
    if type(trainer.fit_loop.epoch_loop) != _TrainingEpochLoop:
        warnings.warn("Detected custom epoch loop. Skipping no validation on restart support.", UserWarning)
        return
    # Pass trainer object to avoid trainer getting overwritten as None
    loop = SkipResumeTrainingValidationLoop(trainer, trainer.min_steps, trainer.max_steps)
    trainer.fit_loop.epoch_loop = loop


class SkipResumeTrainingValidationLoop(_TrainingEpochLoop):
    """
    Extend the PTL Epoch loop to skip validating when resuming.
    This happens when resuming a checkpoint that has already run validation, but loading restores
    the training state before validation has run.
    """

    def _should_check_val_fx(self, data_fetcher) -> bool:
        """_should_check_val_fx"""
        if self.restarting:
            return False
        return super()._should_check_val_fx(data_fetcher)


def clean_exp_ckpt(exp_log_dir: Union[str, Path], remove_ckpt: bool = True, remove_nemo: bool = False):
    """
    Helper method that removes Pytorch Lightning .ckpt files or NeMo .nemo files from the
    checkpoint directory

    Args:
        exp_log_dir: str path to the root directory of the current experiment.
        remove_ckpt: bool, whether to remove all *.ckpt files in the checkpoints directory.
        remove_nemo: bool, whether to remove all *.nemo files in the checkpoints directory.
    """
    exp_log_dir = str(exp_log_dir)

    if remove_ckpt:
        logging.info("Deleting *.ckpt files ...")
        ckpt_files = glob.glob(os.path.join(exp_log_dir, "checkpoints", "*.ckpt"))
        for filepath in ckpt_files:
            os.remove(filepath)
            logging.info(f"Deleted file : {filepath}")

    if remove_nemo:
        logging.info("Deleting *.nemo files ...")
        nemo_files = glob.glob(os.path.join(exp_log_dir, "checkpoints", "*.nemo"))
        for filepath in nemo_files:
            os.remove(filepath)
            logging.info(f"Deleted file : {filepath}")
