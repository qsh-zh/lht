import os
from typing import List, Optional

import hydra
from omegaconf import DictConfig
from pytorch_lightning import (
    Callback,
    LightningDataModule,
    LightningModule,
    Trainer,
    seed_everything,
)
from pytorch_lightning.loggers import LightningLoggerBase

from src.logger.jam_wandb import JamWandb
from src.utils import lht_utils

try:
    from jammy.utils.debug import decorate_exception_hook
except ImportError:
    # pylint: disable=ungrouped-imports
    from src.utils.lht_utils import decorate_exception_hook
log = lht_utils.get_logger(__name__)

# pylint: disable=too-many-branches, protected-access
@decorate_exception_hook
def train(config: DictConfig) -> Optional[float]:
    """Contains the training pipeline. Can additionally evaluate model on a testset, using best
    weights achieved during training.

    Args:
        config (DictConfig): Configuration composed by Hydra.

    Returns:
        Optional[float]: Metric score for hyperparameter optimization.
    """

    # Set seed for random number generators in pytorch, numpy and python.random
    if config.get("seed"):
        seed_everything(config.seed, workers=True)

    # Convert relative ckpt path to absolute path if necessary
    ckpt_path = config.trainer.get("resume_from_checkpoint")
    if ckpt_path and not os.path.isabs(ckpt_path):
        config.trainer.resume_from_checkpoint = os.path.join(
            hydra.utils.get_original_cwd(), ckpt_path
        )

    # Init lightning datamodule
    log.info(
        f"Instantiating datamodule <{config.datamodule._target_}>"  # pylint: disable=protected-access
    )
    datamodule: LightningDataModule = hydra.utils.instantiate(config.datamodule)

    # # Init lightning model
    # log.info(
    #     f"Instantiating model <{config.model.module._target_}>"  # pylint: disable=protected-access
    # )
    # model: LightningModule = hydra.utils.instantiate(config.model.module, config.model)

    # Init lightning model
    log.info(f"Instantiating model <{config.model._target_}>")
    model: LightningModule = hydra.utils.instantiate(config.model)

    # Init lightning callbacks
    callbacks: List[Callback] = []
    if "callbacks" in config:
        for _, cb_conf in config.callbacks.items():
            if "_target_" in cb_conf:
                log.info(
                    f"Instantiating callback <{cb_conf._target_}>"
                )  # pylint: disable=protected-access
                callbacks.append(hydra.utils.instantiate(cb_conf))

    # Init lightning loggers
    logger: List[LightningLoggerBase] = []
    if "logger" in config:
        for _, lg_conf in config.logger.items():
            if "_target_" in lg_conf:
                log.info(
                    f"Instantiating logger <{lg_conf._target_}>"
                )  # pylint: disable=protected-access
                logger.append(hydra.utils.instantiate(lg_conf))

    # Init lightning trainer
    log.info(
        f"Instantiating trainer <{config.trainer._target_}>"
    )  # pylint: disable=protected-access
    lht_utils.auto_gpu(config)
    trainer: Trainer = hydra.utils.instantiate(
        config.trainer, callbacks=callbacks, logger=logger, _convert_="partial"
    )

    # Send some parameters from config to all lightning loggers
    log.info("Logging hyperparameters!")
    JamWandb.g_cfg = config
    lht_utils.log_hyperparameters(
        config=config,
        model=model,
        datamodule=datamodule,
        trainer=trainer,
        callbacks=callbacks,
        logger=logger,
    )

    # reseed before training, encounter once after instantiation, randomness disappear
    if config.get("seed"):
        seed_everything(config.seed, workers=True)
    # Train the model
    if config.get("train"):
        log.info("Starting training!")
        trainer.fit(model=model, datamodule=datamodule)

    # Get metric score for hyperparameter optimization
    optimized_metric = config.get("optimized_metric")
    if optimized_metric and optimized_metric not in trainer.callback_metrics:
        raise Exception(
            "Metric for hyperparameter optimization not found! "
            "Make sure the `optimized_metric` in `hparams_search` config is correct!"
        )
    score = trainer.callback_metrics.get(optimized_metric)

    # Test the model
    if config.get("test"):
        ckpt_path = "best"
        if not config.get("train") or config.trainer.get("fast_dev_run"):
            ckpt_path = None
        log.info("Starting testing!")
        trainer.test(model=model, datamodule=datamodule, ckpt_path=ckpt_path)

    # Make sure everything closed properly
    log.info("Finalizing!")
    lht_utils.finish(
        config=config,
        model=model,
        datamodule=datamodule,
        trainer=trainer,
        callbacks=callbacks,
        logger=logger,
    )

    # Print path to best checkpoint
    if (
        not config.trainer.get("fast_dev_run")
        and config.get("train")
        and trainer.checkpoint_callback is not None
    ):
        log.info(f"Best model ckpt: {trainer.checkpoint_callback.best_model_path}")

    # Return metric score for hyperparameter optimization
    optimized_metric = config.get("optimized_metric")
    if optimized_metric:
        return trainer.callback_metrics[optimized_metric]
    return score
