from typing import Optional, Any, Dict, List
from pathlib import Path
import sys
import os
from datetime import timedelta
import pytorch_lightning as pl
from pytorch_lightning.loggers import WandbLogger
from pytorch_lightning.strategies import DDPStrategy
from pytorch_lightning.callbacks import ModelCheckpoint, Callback
import git
from termcolor import colored
import argparse
import yaml
import uuid

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, PROJECT_ROOT)
from mpinets.data_loader import DataModule
from mpinets.model import TrainingMotionPolicyNetwork


def setup_trainer(
    gpus: int,
    test: bool,
    should_log: bool,
    should_checkpoint: bool,
    logger: Optional[WandbLogger],
    checkpoint_interval: int,
    checkpoint_dir: str,
    validation_interval: float,
) -> pl.Trainer:
    """
    Creates the Pytorch Lightning trainer object

    :param gpus int: The number of GPUs (if more than 1, uses DDP)
    :param test bool: Whether to use a test dataset
    :param should_log bool: Whether to log to Weights and Biases
    :param should_checkpoint bool: Whether to save checkpoints
    :param logger Optional[WandbLogger]: The logger object
    :param checkpoint_interval int: The number of minutes between checkpoints
    :param checkpoint_dir str: The directory in which to save checkpoints (a subdirectory will
                               be created according to the experiment ID)
    :param validation_interval float: How often to run the validation step, either as a proportion
                                      of the training epoch or as a number of batches
    :rtype pl.Trainer: The trainer object
    """
    args: Dict[str, Any] = {}

    if test:
        args = {**args, "limit_train_batches": 10, "limit_val_batches": 3}
        validation_interval = 2  # Overwritten to be an appropriate size for test
    if (isinstance(gpus, list) and len(gpus) > 1) or (
        isinstance(gpus, int) and gpus > 1
    ):
        args = {
            **args,
            "strategy": DDPStrategy(find_unused_parameters=False),
        }
    if validation_interval is not None:
        args = {**args, "val_check_interval": validation_interval}
    callbacks: List[Callback] = []
    if should_log:
        assert logger is not None, "If should_log is True, logger should not be None"
        experiment_id = str(logger.experiment.id)
    else:
        experiment_id = str(uuid.uuid1())
    if should_checkpoint:
        if checkpoint_dir is not None:
            dirpath = Path(checkpoint_dir).resolve() / experiment_id
        else:
            dirpath = PROJECT_ROOT / "checkpoints" / experiment_id
        pl.utilities.rank_zero_info(f"Saving checkpoints to {dirpath}")
        every_n_checkpoint = ModelCheckpoint(
            monitor="val_loss",
            save_last=True,
            dirpath=dirpath,
            train_time_interval=timedelta(minutes=checkpoint_interval),
        )
        epoch_end_checkpoint = ModelCheckpoint(
            monitor="val_loss",
            save_last=True,
            dirpath=dirpath,
            save_on_train_epoch_end=True,
        )
        epoch_end_checkpoint.CHECKPOINT_NAME_LAST = "epoch-{epoch}-end"
        callbacks.extend([every_n_checkpoint, epoch_end_checkpoint])

    trainer = pl.Trainer(
        enable_checkpointing=should_log,
        callbacks=callbacks,
        max_epochs=1 if test else 500,
        gradient_clip_val=1.0,
        gpus=gpus,
        precision=16,
        logger=False if logger is None else logger,
        **args,
    )
    return trainer


def setup_logger(
    should_log: bool, experiment_name: str, config_values: Dict[str, Any]
) -> Optional[WandbLogger]:
    if not should_log:
        pl.utilities.rank_zero_info("Disabling all logs")
        return None
    logger = WandbLogger(
        name=experiment_name,
        project="mpinets",
        log_model=True,
    )
    logger.log_hyperparams(config_values)
    return logger


def check_for_uncommitted_changes():
    """
    Checks whether this repo has uncommitted changes

    :raises Exception: Raises when there are uncommitted changes
    """
    repo = git.Repo(search_parent_directories=True)
    if repo.is_dirty(untracked_files=False):
        raise Exception(
            "Uncommitted changes found in local git repo. "
            "Commit all changes before running experiments "
            "to maintain reproducibility."
        )


def confirm_allow_dirty_repo() -> bool:
    """
    Ask user to enter Y or N (case-insensitive).
    Code is borrowed from here:
        https://gist.github.com/gurunars/4470c97c916e7b3c4731469c69671d06
    :rtype bool: True if the answer is either y or yes (regardless of capitalization).
    """
    answer = ""
    message = (
        "you have set --allow-dirty-repo which will run an experiment with"
        " uncommitted changes. This will forcibly disable logging, as logged"
        " experiments should be reproducible. Do you wish to continue? [y/n] "
    )
    while answer not in ["y", "yes", "n", "no"]:
        answer = input(colored("Warning:", "red") + message).lower()
    return answer in ["y", "yes"]


def parse_args_and_configuration():
    """
    Checks the command line arguments and merges them with the configuration yaml file
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("yaml_config", type=str)
    parser.add_argument(
        "--test",
        action="store_true",
        help="Allow dirty repo and test with only a few batches (disables logging)",
    )
    parser.add_argument(
        "--no-logging", action="store_true", help="Don't log to weights and biases"
    )
    parser.add_argument(
        "--no-checkpointing", action="store_true", help="Don't checkpoint"
    )
    parser.add_argument(
        "--allow-dirty-repo",
        action="store_true",
        help="Run with uncommitted changes (disables logging)",
    )
    args = parser.parse_args()

    if args.test:
        args.no_logging = True
    elif args.allow_dirty_repo and confirm_allow_dirty_repo():
        args.no_logging = True
    else:
        check_for_uncommitted_changes()

    with open(args.yaml_config) as f:
        configuration = yaml.safe_load(f)

    return {
        "training_node_name": os.uname().nodename,
        **configuration,
        **vars(args),
    }


def run():
    """
    Runs the training procedure
    """
    config = parse_args_and_configuration()

    color_name = colored(config["experiment_name"], "green")
    pl.utilities.rank_zero_info(f"Experiment name: {color_name}")
    logger = setup_logger(
        not config["no_logging"],
        config["experiment_name"],
        config,
    )

    trainer = setup_trainer(
        config["gpus"],
        config["test"],
        should_log=not config["no_logging"],
        should_checkpoint=not config["no_checkpointing"],
        logger=logger,
        checkpoint_interval=config["checkpoint_interval"],
        checkpoint_dir=config["save_checkpoint_dir"],
        validation_interval=config["validation_interval"],
    )
    dm = DataModule(
        batch_size=config["batch_size"],
        **(config["shared_parameters"] or {}),
        **(config["data_module_parameters"] or {}),
    )
    mdl = TrainingMotionPolicyNetwork(
        **(config["shared_parameters"] or {}),
        **(config["training_model_parameters"] or {}),
    )
    if logger is not None:
        logger.watch(mdl, log="gradients", log_freq=100)
    trainer.fit(model=mdl, datamodule=dm)


if __name__ == "__main__":
    run()
