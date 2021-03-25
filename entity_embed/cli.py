import csv
import json
import logging
import os
import random

import click
import numpy as np
import pytorch_lightning as pl
import torch
from pytorch_lightning.loggers import TensorBoardLogger

from . import DeduplicationDataModule, EntityEmbed, LinkageDataModule, LinkageEmbed, validate_best
from .data_utils.attr_config_parser import AttrConfigDictParser
from .early_stopping import EarlyStoppingMinEpochs, ModelCheckpointMinEpochs

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _fix_workers_kwargs(kwargs):
    # Accept -1 as "num_workers"
    if kwargs["num_workers"] == -1:
        kwargs["num_workers"] = os.cpu_count()
    # Duplicate "num_workers" key into "n_threads" key since _build_datamodule
    # uses "num_workers" and _build_model uses "n_threads"
    kwargs["n_threads"] = kwargs["num_workers"]


def _set_random_seeds(kwargs):
    if kwargs.get("random_seed") is not None:
        random_seed = kwargs["random_seed"]
        torch.manual_seed(random_seed)
        np.random.seed(random_seed)
        random.seed(random_seed)


def _build_row_dict(csv_filepath, kwargs):
    csv_encoding = kwargs["csv_encoding"]
    cluster_attr = kwargs.get("cluster_attr")
    row_dict = {}

    with open(csv_filepath, "r", newline="", encoding=csv_encoding) as row_dict_csv_file:
        for row in csv.DictReader(row_dict_csv_file):
            if cluster_attr in row:
                # force cluster_attr to be an int, if there's a cluster_attr
                row[cluster_attr] = int(row[cluster_attr])
            # force id attr to be an int
            row["id"] = int(row["id"])
            row_dict[row["id"]] = row

    logger.info(f"Finished reading {csv_filepath}")
    return row_dict


def _build_row_numericalizer(row_list, kwargs):
    attr_config_json = kwargs["attr_config_json"]

    with open(attr_config_json, "r") as attr_config_json_file:
        row_numericalizer = AttrConfigDictParser.from_json(attr_config_json_file, row_list=row_list)

    logger.info(f"Finished reading {attr_config_json}")
    return row_numericalizer


def _is_record_linkage(kwargs):
    left_source = kwargs.get("left_source")
    source_attr = kwargs.get("source_attr")
    if (left_source and not source_attr) or (not left_source and source_attr):
        raise KeyError(
            'You must provide BOTH "source_attr" and "left_source" to perform Record Linkage. '
            "Either remove both or provide both."
        )
    else:
        return bool(left_source)


def _build_datamodule(train_row_dict, valid_row_dict, test_row_dict, row_numericalizer, kwargs):
    datamodule_args = {
        "train_row_dict": train_row_dict,
        "valid_row_dict": valid_row_dict,
        "test_row_dict": test_row_dict,
        "cluster_attr": kwargs["cluster_attr"],
        "row_numericalizer": row_numericalizer,
        "batch_size": kwargs["batch_size"],
        "eval_batch_size": kwargs["eval_batch_size"],
    }

    if _is_record_linkage(kwargs):
        datamodule_cls = LinkageDataModule
        datamodule_args.update(
            {
                "source_attr": kwargs["source_attr"],
                "left_source": kwargs["left_source"],
            }
        )
    else:
        datamodule_cls = DeduplicationDataModule

    if kwargs.get("num_workers") or kwargs.get("multiprocessing_context"):
        for k in ["train_loader_kwargs", "eval_loader_kwargs"]:
            datamodule_args[k] = {}
            for inner_k in ["num_workers", "multiprocessing_context"]:
                if kwargs[inner_k]:
                    datamodule_args[k][inner_k] = kwargs[inner_k]

    if kwargs.get("random_seed"):
        datamodule_args["random_seed"] = kwargs["random_seed"]

    logger.info("Building datamodule...")
    return datamodule_cls(**datamodule_args)


def _build_model(row_numericalizer, kwargs):
    model_args = {"row_numericalizer": row_numericalizer, "eval_with_clusters": True}

    if _is_record_linkage(kwargs):
        model_cls = LinkageEmbed
        model_args.update(
            {
                "source_attr": kwargs["source_attr"],
                "left_source": kwargs["left_source"],
            }
        )
    else:
        model_cls = EntityEmbed

    if kwargs["embedding_size"]:
        model_args["embedding_size"] = kwargs["embedding_size"]

    if kwargs["lr"]:
        model_args["learning_rate"] = kwargs["lr"]

    if kwargs["ann_k"]:
        model_args["ann_k"] = kwargs["ann_k"]

    if kwargs["sim_threshold"]:
        model_args["sim_threshold_list"] = kwargs["sim_threshold"]

    model_args["index_build_kwargs"] = {}
    for k in ["m", "max_m0", "ef_construction", "n_threads"]:
        if kwargs[k]:
            model_args["index_build_kwargs"][k] = kwargs[k]

    model_args["index_search_kwargs"] = {}
    for k in ["ef_search", "n_threads"]:
        if kwargs[k]:
            model_args["index_search_kwargs"][k] = kwargs[k]

    logger.info("Building model...")

    return model_cls(**model_args)


def _build_trainer(kwargs):
    min_epochs = kwargs["min_epochs"]
    monitor = kwargs["early_stopping_monitor"]
    min_delta = kwargs["early_stopping_min_delta"]
    patience = kwargs["early_stopping_patience"]
    mode = kwargs["early_stopping_mode"] or ("min" if "pair_entity_ratio_at" in monitor else "max")

    early_stop_callback = EarlyStoppingMinEpochs(
        min_epochs=min_epochs,
        monitor=monitor,
        min_delta=min_delta,
        patience=patience,
        verbose=True,
        mode=mode,
    )

    checkpoint_callback = ModelCheckpointMinEpochs(
        min_epochs=min_epochs,
        monitor=monitor,
        save_top_k=1,
        mode=mode,
        verbose=True,
        dirpath=kwargs["model_save_dir"],
    )

    trainer_args = {
        "gpus": 1,
        "min_epochs": min_epochs,
        "max_epochs": kwargs["max_epochs"],
        "check_val_every_n_epoch": kwargs["check_val_every_n_epoch"],
        "callbacks": [early_stop_callback, checkpoint_callback],
        "reload_dataloaders_every_epoch": True,  # for shuffling ClusterDataset every epoch
    }

    if kwargs["tb_name"] and kwargs["tb_save_dir"]:
        trainer_args["logger"] = TensorBoardLogger(
            kwargs["tb_save_dir"],
            name=kwargs["tb_name"],
        )
    elif kwargs["tb_name"] or kwargs["tb_save_dir"]:
        raise KeyError(
            'Please provide both "tb_name" and "tb_save_dir" to enable '
            "TensorBoardLogger or omit both to disable it"
        )

    return pl.Trainer(**trainer_args)


@click.command()
@click.option(
    "--attr_config_json",
    type=str,
    required=True,
    help="Path of the JSON configuration file "
    "that defines how columns will be processed by the neural network",
)
@click.option(
    "--train_csv",
    type=str,
    required=True,
    help="Path of the TRAIN input dataset CSV file",
)
@click.option(
    "--valid_csv",
    type=str,
    required=True,
    help="Path of the VALID input dataset CSV file",
)
@click.option(
    "--test_csv",
    type=str,
    required=True,
    help="Path of the TEST input dataset CSV file",
)
@click.option(
    "--unlabeled_csv",
    type=str,
    required=True,
    help="Path of the UNLABELED input dataset CSV file",
)
@click.option(
    "--csv_encoding", type=str, default="utf-8", help="Encoding of the input dataset CSV file"
)
@click.option(
    "--cluster_attr",
    type=str,
    required=True,
    help="Column of the CSV dataset that contains the true cluster assignment. "
    "Equivalent to the label in tabular classification",
)
@click.option(
    "--source_attr",
    type=str,
    help="Set this when doing Record Linkage. "
    "Column of the CSV dataset that contains the indication of the left or right source "
    "for Record Linkage",
)
@click.option(
    "--left_source",
    type=str,
    help="Set this when doing Record Linkage. "
    "Consider any row with this value in the `source_attr` column as the left_source dataset. "
    "The rows with other `source_attr` values are considered the right dataset",
)
@click.option(
    "--embedding_size", type=int, default=300, help="Embedding Dimensionality, for example: 300"
)
@click.option("--lr", type=float, default=0.001, help="Learning Rate for training")
@click.option("--min_epochs", type=int, default=5, help="Min number of epochs to run")
@click.option("--max_epochs", type=int, default=100, help="Max number of epochs to run")
@click.option(
    "--early_stopping_monitor",
    type=str,
    default="valid_recall_at_0.3",
    help="Metric to be monitored for early stoping. E.g. `valid_recall_at_0.3`. "
    "The float on `at_X` must be one of `sim_threshold`",
)
@click.option(
    "--early_stopping_min_delta",
    type=float,
    default=0.0,
    help="Minimum change in the monitored metric to qualify as an improvement",
)
@click.option(
    "--early_stopping_patience",
    type=int,
    default=20,
    help="Number of validation runs with no improvement after which training will be stopped",
)
@click.option(
    "--early_stopping_mode",
    type=str,
    default="max",
    help="Mode for early stopping. Values are `max` or `min`. "
    "Based on `early_stopping_monitor` metric",
)
@click.option("--tb_save_dir", type=str, help="TensorBoard save directory")
@click.option("--tb_name", type=str, help="TensorBoard experiment name")
@click.option(
    "--check_val_every_n_epoch",
    type=int,
    default=1,
    help="Run validation every N epochs.",
)
@click.option("--batch_size", type=int, required=True, help="Training batch size, in CLUSTERS")
@click.option("--eval_batch_size", type=int, required=True, help="Evaluation batch size, in ROWS")
@click.option(
    "--num_workers",
    type=int,
    default=-1,
    help="Number of workers to use in PyTorch Lightning datamodules "
    "and also number of threads to use in ANN. Set -1 to use all available CPUs",
)
@click.option(
    "--multiprocessing_context",
    type=str,
    default="fork",
    help="Context name for multiprocessing for PyTorch Lightning datamodules, "
    "like `spawn`, `fork`, `forkserver` (currently only tested with `fork`)",
)
@click.option(
    "--sim_threshold",
    type=float,
    multiple=True,
    help="Cosine similarity thresholds to use when computing validation and testing metrics. "
    "For each of these thresholds, validation and testing metrics "
    "(precision, recall, etc.) are computed, "
    "but ignoring any ANN neighbors with cosine similarity BELOW the threshold",
)
@click.option(
    "--ann_k",
    type=int,
    help="When finding duplicates, use this number as the K for the Approximate Nearest Neighbors",
)
@click.option("--m", type=int, help="Parameter for the ANN. See N2 docs: n2.readthedocs.io")
@click.option("--max_m0", type=int, help="Parameter for the ANN. See N2 docs: n2.readthedocs.io")
@click.option(
    "--ef_construction", type=int, help="Parameter for the ANN. See N2 docs: n2.readthedocs.io"
)
@click.option("--ef_search", type=int, help="Parameter for the ANN. See N2 docs: n2.readthedocs.io")
@click.option("--random_seed", type=int, help="Random seed to help with reproducibility")
@click.option(
    "--model_save_dir",
    type=str,
    help="Directory path where to save the best validation model checkpoint"
    " using PyTorch Lightning",
)
def train(**kwargs):
    """
    Transform entities like companies, products, etc. into vectors
    to support scalable Record Linkage / Entity Resolution
    using Approximate Nearest Neighbors.
    """
    _fix_workers_kwargs(kwargs)
    _set_random_seeds(kwargs)
    train_row_dict = _build_row_dict(csv_filepath=kwargs["train_csv"], kwargs=kwargs)
    valid_row_dict = _build_row_dict(csv_filepath=kwargs["valid_csv"], kwargs=kwargs)
    test_row_dict = _build_row_dict(csv_filepath=kwargs["test_csv"], kwargs=kwargs)
    unlabeled_row_dict = _build_row_dict(csv_filepath=kwargs["unlabeled_csv"], kwargs=kwargs)
    row_list_all = [
        *train_row_dict.values(),
        *valid_row_dict.values(),
        *test_row_dict.values(),
        *unlabeled_row_dict.values(),
    ]
    row_numericalizer = _build_row_numericalizer(row_list=row_list_all, kwargs=kwargs)
    del row_list_all, unlabeled_row_dict
    datamodule = _build_datamodule(
        train_row_dict=train_row_dict,
        valid_row_dict=valid_row_dict,
        test_row_dict=test_row_dict,
        row_numericalizer=row_numericalizer,
        kwargs=kwargs,
    )
    model = _build_model(row_numericalizer=row_numericalizer, kwargs=kwargs)

    trainer = _build_trainer(kwargs)
    trainer.fit(model, datamodule)
    del model, datamodule
    valid_metrics = validate_best(trainer)
    logger.info(valid_metrics)
    test_metrics = trainer.test(ckpt_path="best", verbose=False)
    logger.info(test_metrics)

    logger.info("Saved best model at path:")
    logger.info(trainer.checkpoint_callback.best_model_path)

    return 0


def _load_model(kwargs):
    if _is_record_linkage(kwargs):
        model_cls = LinkageEmbed
    else:
        model_cls = EntityEmbed

    return model_cls.load_from_checkpoint(kwargs["model_save_filepath"], datamodule=None)


def _predict_pairs(row_dict, model, kwargs):
    eval_batch_size = kwargs["eval_batch_size"]
    num_workers = kwargs["num_workers"]
    multiprocessing_context = kwargs["multiprocessing_context"]
    ann_k = kwargs["ann_k"]
    sim_threshold = kwargs["sim_threshold"]

    index_build_kwargs = {}
    for k in ["m", "max_m0", "ef_construction", "n_threads"]:
        if kwargs[k]:
            index_build_kwargs[k] = kwargs[k]

    index_search_kwargs = {}
    for k in ["ef_search", "n_threads"]:
        if kwargs[k]:
            index_search_kwargs[k] = kwargs[k]

    if _is_record_linkage(kwargs):
        found_pair_set = model.predict_pairs(
            row_dict=row_dict,
            batch_size=eval_batch_size,
            ann_k=ann_k,
            sim_threshold=sim_threshold,
            loader_kwargs={
                "num_workers": num_workers,
                "multiprocessing_context": multiprocessing_context,
            },
            index_build_kwargs=index_build_kwargs,
            index_search_kwargs=index_search_kwargs,
        )
    else:
        found_pair_set = model.predict_pairs(
            row_dict=row_dict,
            batch_size=eval_batch_size,
            ann_k=ann_k,
            sim_threshold=sim_threshold,
            loader_kwargs={
                "num_workers": num_workers,
                "multiprocessing_context": multiprocessing_context,
            },
            index_build_kwargs=index_build_kwargs,
            index_search_kwargs=index_search_kwargs,
        )
    return list(found_pair_set)


def _write_json(found_pairs, kwargs):
    with open(kwargs["output_json"], "w", encoding="utf-8") as f:
        json.dump(found_pairs, f, indent=4)


@click.command()
@click.option(
    "--model_save_filepath",
    type=str,
    help="Path where the model checkpoint was saved",
)
@click.option(
    "--attr_config_json",
    type=str,
    required=True,
    help="Path of the JSON configuration file "
    "that defines how columns will be processed by the neural network",
)
@click.option(
    "--unlabeled_csv",
    type=str,
    required=True,
    help="Path of the unlabeled input dataset CSV file",
)
@click.option(
    "--csv_encoding",
    type=str,
    default="utf-8",
    help="Encoding of the input and output dataset CSV files",
)
@click.option(
    "--source_attr",
    type=str,
    help="Set this when doing Record Linkage. "
    "Column of the CSV dataset that contains the indication of the left or right source "
    "for Record Linkage",
)
@click.option(
    "--left_source",
    type=str,
    help="Set this when doing Record Linkage. "
    "Consider any row with this value in the `source_attr` column as the left_source dataset. "
    "The rows with other `source_attr` values are considered the right dataset",
)
@click.option("--eval_batch_size", type=int, required=True, help="Evaluation batch size, in ROWS")
@click.option(
    "--num_workers",
    type=int,
    default=-1,
    help="Number of workers to use in PyTorch Lightning datamodules "
    "and also number of threads to use in ANN. Set -1 to use all available CPUs",
)
@click.option(
    "--multiprocessing_context",
    type=str,
    default="fork",
    help="Context name for multiprocessing for PyTorch Lightning datamodules, "
    "like `spawn`, `fork`, `forkserver` (currently only tested with `fork`)",
)
@click.option(
    "--sim_threshold",
    type=float,
    multiple=False,
    default=[0.3, 0.5, 0.7],
    help="A SINGLE cosine similarity threshold to use when finding duplicates. "
    "Any ANN neighbors with cosine similarity BELOW this threshold is ignored",
)
@click.option(
    "--ann_k",
    type=int,
    default=100,
    help="When finding duplicates, use this number as the K for the Approximate Nearest Neighbors",
)
@click.option("--m", type=int, help="Parameter for the ANN. See N2 docs: n2.readthedocs.io")
@click.option("--max_m0", type=int, help="Parameter for the ANN. See N2 docs: n2.readthedocs.io")
@click.option(
    "--ef_construction", type=int, help="Parameter for the ANN. See N2 docs: n2.readthedocs.io"
)
@click.option("--ef_search", type=int, help="Parameter for the ANN. See N2 docs: n2.readthedocs.io")
@click.option("--random_seed", type=int, help="Random seed to help with reproducibility")
@click.option(
    "--output_json",
    type=str,
    required=True,
    help="Path of the output CSV file that will contain the `cluster_attr` with the found values. "
    "The CSV will be equal to the dataset CSV but with the additional `cluster_attr` column",
)
def predict(**kwargs):
    _fix_workers_kwargs(kwargs)
    _set_random_seeds(kwargs)
    model = _load_model(kwargs)
    row_dict = _build_row_dict(
        csv_filepath=kwargs["unlabeled_csv"],
        kwargs=kwargs,
    )
    found_pairs = _predict_pairs(row_dict=row_dict, model=model, kwargs=kwargs)
    _write_json(found_pairs=found_pairs, kwargs=kwargs)

    logger.info(f"Found {len(found_pairs)} candidate pairs, writing to JSON file at:")
    logger.info(kwargs["output_json"])

    return 0
