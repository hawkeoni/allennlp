"""
The ``train`` subcommand can be used to train a model.
It requires a configuration file and a directory in
which to write the results.

.. code-block:: bash

   $ allennlp train --help
    usage: allennlp train [-h] -s SERIALIZATION_DIR [-r] [-f] [-o OVERRIDES]
                          [--file-friendly-logging] [--node-rank NODE_RANK]
                          [--include-package INCLUDE_PACKAGE]
                          param_path

    Train the specified model on the specified dataset.

    positional arguments:
      param_path            path to parameter file describing the model to be
                            trained

    optional arguments:
      -h, --help            show this help message and exit
      -s SERIALIZATION_DIR, --serialization-dir SERIALIZATION_DIR
                            directory in which to save the model and its logs
      -r, --recover         recover training from the state in serialization_dir
      -f, --force           overwrite the output directory if it exists
      -o OVERRIDES, --overrides OVERRIDES
                            a JSON structure used to override the experiment
                            configuration
      --file-friendly-logging
                            outputs tqdm status on separate lines and slows tqdm
                            refresh rate
      --node-rank NODE_RANK
                            Rank of this node in the distributed setup (default =
                            0)
      --include-package INCLUDE_PACKAGE
                            additional packages to include
"""

import argparse
import logging
import os
from typing import Any, Dict, Iterable, List, Optional

import torch
import torch.distributed as dist
import torch.multiprocessing as mp
from overrides import overrides

from allennlp.commands.subcommand import Subcommand
from allennlp.common import Params, Registrable, Lazy
from allennlp.common.checks import check_for_gpu, ConfigurationError
from allennlp.common import util as common_util
from allennlp.data import DataIterator, DatasetReader, Instance, Vocabulary
from allennlp.models.archival import archive_model, CONFIG_NAME
from allennlp.models.model import _DEFAULT_WEIGHTS, Model
from allennlp.training.trainer_base import TrainerBase
from allennlp.training import util as training_util

logger = logging.getLogger(__name__)


@Subcommand.register("train")
class Train(Subcommand):
    @overrides
    def add_subparser(self, parser: argparse._SubParsersAction) -> argparse.ArgumentParser:
        description = """Train the specified model on the specified dataset."""
        subparser = parser.add_parser(self.name, description=description, help="Train a model.")

        subparser.add_argument(
            "param_path", type=str, help="path to parameter file describing the model to be trained"
        )

        subparser.add_argument(
            "-s",
            "--serialization-dir",
            required=True,
            type=str,
            help="directory in which to save the model and its logs",
        )

        subparser.add_argument(
            "-r",
            "--recover",
            action="store_true",
            default=False,
            help="recover training from the state in serialization_dir",
        )

        subparser.add_argument(
            "-f",
            "--force",
            action="store_true",
            required=False,
            help="overwrite the output directory if it exists",
        )

        subparser.add_argument(
            "-o",
            "--overrides",
            type=str,
            default="",
            help="a JSON structure used to override the experiment configuration",
        )

        subparser.add_argument(
            "--file-friendly-logging",
            action="store_true",
            default=False,
            help="outputs tqdm status on separate lines and slows tqdm refresh rate",
        )

        subparser.add_argument(
            "--node-rank", type=int, default=0, help="Rank of this node in the distributed setup"
        )

        subparser.set_defaults(func=train_model_from_args)

        return subparser


def train_model_from_args(args: argparse.Namespace):
    """
    Just converts from an ``argparse.Namespace`` object to string paths.
    """
    train_model_from_file(
        parameter_filename=args.param_path,
        serialization_dir=args.serialization_dir,
        overrides=args.overrides,
        file_friendly_logging=args.file_friendly_logging,
        recover=args.recover,
        force=args.force,
        node_rank=args.node_rank,
        include_package=args.include_package,
    )


def train_model_from_file(
    parameter_filename: str,
    serialization_dir: str,
    overrides: str = "",
    file_friendly_logging: bool = False,
    recover: bool = False,
    force: bool = False,
    node_rank: int = 0,
    include_package: List[str] = None,
) -> Model:
    """
    A wrapper around :func:`train_model` which loads the params from a file.

    # Parameters

    parameter_filename : ``str``
        A json parameter file specifying an AllenNLP experiment.
    serialization_dir : ``str``
        The directory in which to save results and logs. We just pass this along to
        :func:`train_model`.
    overrides : ``str``
        A JSON string that we will use to override values in the input parameter file.
    file_friendly_logging : ``bool``, optional (default=False)
        If ``True``, we make our output more friendly to saved model files.  We just pass this
        along to :func:`train_model`.
    recover : ``bool`, optional (default=False)
        If ``True``, we will try to recover a training run from an existing serialization
        directory.  This is only intended for use when something actually crashed during the middle
        of a run.  For continuing training a model on new data, see the ``fine-tune`` command.
    force : ``bool``, optional (default=False)
        If ``True``, we will overwrite the serialization directory if it already exists.
    node_rank : ``int``, optional
        Rank of the current node in distributed training
    include_package : ``str``, optional
        In distributed mode, extra packages mentioned will be imported in trainer workers.
    """
    # Load the experiment config from a file and pass it to ``train_model``.
    params = Params.from_file(parameter_filename, overrides)
    return train_model(
        params=params,
        serialization_dir=serialization_dir,
        file_friendly_logging=file_friendly_logging,
        recover=recover,
        force=force,
        node_rank=node_rank,
        include_package=include_package,
    )


def train_model(
    params: Params,
    serialization_dir: str,
    file_friendly_logging: bool = False,
    recover: bool = False,
    force: bool = False,
    node_rank: int = 0,
    include_package: List[str] = None,
    batch_weight_key: str = "",
    # For fine-tuning:
    model: Model = None,
    extend_vocab: bool = False,
    embedding_sources_mapping: Dict[str, str] = None,
) -> Model:
    """
    Trains the model specified in the given :class:`Params` object, using the data and training
    parameters also specified in that object, and saves the results in ``serialization_dir``.

    # Parameters

    params : ``Params``
        A parameter object specifying an AllenNLP Experiment.
    serialization_dir : ``str``
        The directory in which to save results and logs.
    file_friendly_logging : ``bool``, optional (default=False)
        If ``True``, we add newlines to tqdm output, even on an interactive terminal, and we slow
        down tqdm's output to only once every 10 seconds.
    recover : ``bool``, optional (default=False)
        If ``True``, we will try to recover a training run from an existing serialization
        directory.  This is only intended for use when something actually crashed during the middle
        of a run.  For continuing training a model on new data, see the ``fine-tune`` command.
    force : ``bool``, optional (default=False)
        If ``True``, we will overwrite the serialization directory if it already exists.
    node_rank : ``int``, optional
        Rank of the current node in distributed training
    include_package : ``List[str]``, optional
        In distributed mode, extra packages mentioned will be imported in trainer workers.
    batch_weight_key : ``str``, optional (default="")
        If non-empty, name of metric used to weight the loss on a per-batch basis.
    model : ``Model``, optional
        A model to fine tune.
    extend_vocab : ``bool``, optional (default=False)
        If ``True``, we use the new instances to extend your vocabulary.
        Used only when fine-tuning.
    embedding_sources_mapping : ``Dict[str, str]``, optional (default=None)
        Mapping from model paths to the pretrained embedding filepaths.
        Used only when fine-tuning.

    # Returns

    best_model : ``Model``
        The model with the best epoch weights.
    """
    training_util.create_serialization_dir(params, serialization_dir, recover, force)
    params.to_file(os.path.join(serialization_dir, CONFIG_NAME))

    distributed_params = params.params.pop("distributed", None)
    # If distributed isn't in the config and the config contains strictly
    # one cuda device, we just run a single training process.
    if distributed_params is None:
        model = _train_worker(
            process_rank=0,
            params=params,
            serialization_dir=serialization_dir,
            file_friendly_logging=file_friendly_logging,
            recover=recover,
            include_package=include_package,
            batch_weight_key=batch_weight_key,
            model=model,
            extend_vocab=extend_vocab,
            embedding_sources_mapping=embedding_sources_mapping,
        )
        archive_model(serialization_dir)
        return model

    # Otherwise, we are running multiple processes for training.
    else:
        # We are careful here so that we can raise a good error if someone
        # passed the wrong thing - cuda_devices are required.
        device_ids = distributed_params.pop("cuda_devices", None)
        multi_device = isinstance(device_ids, list) and len(device_ids) > 1
        num_nodes = distributed_params.pop("num_nodes", 1)

        if not (multi_device or num_nodes > 1):
            raise ConfigurationError(
                "Multiple cuda devices/nodes need to be configured to run distributed training."
            )
        check_for_gpu(device_ids)

        master_addr = distributed_params.pop("master_address", "127.0.0.1")
        master_port = distributed_params.pop("master_port", 29500)
        num_procs = len(device_ids)
        world_size = num_nodes * num_procs

        logging.info(
            f"Switching to distributed training mode since multiple GPUs are configured"
            f"Master is at: {master_addr}:{master_port} | Rank of this node: {node_rank} | "
            f"Number of workers in this node: {num_procs} | Number of nodes: {num_nodes} | "
            f"World size: {world_size}"
        )

        # Creating `Vocabulary` objects from workers could be problematic since the data iterators
        # in each worker will yield only `rank` specific instances. Hence it is safe to construct
        # the vocabulary and write it to disk before initializing the distributed context. The workers
        # will load the vocabulary from the path specified.
        vocab = training_util.make_vocab_from_params(params.duplicate(), serialization_dir)
        params["vocabulary"] = {
            "type": "from_files",
            "directory": os.path.join(serialization_dir, "vocabulary"),
            "padding_token": vocab._padding_token,
            "oov_token": vocab._oov_token,
        }

        mp.spawn(
            _train_worker,
            args=(
                params.duplicate(),
                serialization_dir,
                file_friendly_logging,
                recover,
                include_package,
                batch_weight_key,
                node_rank,
                master_addr,
                master_port,
                world_size,
                device_ids,
                model,
                extend_vocab,
                embedding_sources_mapping,
            ),
            nprocs=num_procs,
        )
        archive_model(serialization_dir)
        model = Model.load(params, serialization_dir)
        return model


def _train_worker(
    process_rank: int,
    params: Params,
    serialization_dir: str,
    file_friendly_logging: bool = False,
    recover: bool = False,
    include_package: List[str] = None,
    batch_weight_key: str = "",
    node_rank: int = 0,
    master_addr: str = "127.0.0.1",
    master_port: int = 29500,
    world_size: int = 1,
    distributed_device_ids: List[str] = None,
    # For fine-tuning:
    model: Model = None,
    extend_vocab: bool = False,
    embedding_sources_mapping: Dict[str, str] = None,
) -> Optional[Model]:
    """
    Helper to train the configured model/experiment. In distributed mode, this is spawned as a
    worker process. In a single GPU experiment, this returns the ``Model`` object and in distributed
    training, nothing is returned.

    # Parameters

    process_rank : ``int``
        The process index that is initialized using the GPU device id.
    params : ``Params``
        A parameter object specifying an AllenNLP Experiment.
    serialization_dir : ``str``
        The directory in which to save results and logs.
    file_friendly_logging : ``bool``, optional (default=False)
        If ``True``, we add newlines to tqdm output, even on an interactive terminal, and we slow
        down tqdm's output to only once every 10 seconds.
    recover : ``bool``, optional (default=False)
        If ``True``, we will try to recover a training run from an existing serialization
        directory.  This is only intended for use when something actually crashed during the middle
        of a run.  For continuing training a model on new data, see the ``fine-tune`` command.
    include_package : ``List[str]``, optional
        In distributed mode, since this function would have been spawned as a separate process,
        the extra imports need to be done again. NOTE: This does not have any effect in single
        GPU training.
    node_rank : ``int``, optional
        Rank of the node
    world_size : ``int``, optional
        The number of processes involved in distributed training.

    # Returns

    best_model : ``Model``
        The model with the best epoch weights.
    """
    common_util.prepare_global_logging(
        serialization_dir, file_friendly_logging, rank=process_rank, world_size=world_size
    )
    common_util.prepare_environment(params)

    distributed = world_size > 1

    # not using `allennlp.common.util.is_master` as the process group is yet to be initialized
    master = process_rank == 0

    if distributed:
        # Since the worker is spawned and not forked, the extra imports
        # need to be done again.
        if include_package is not None:
            for package_name in include_package:
                common_util.import_submodules(package_name)

        num_procs_per_node = len(distributed_device_ids)
        # The Unique identifier of the worker process among all the processes in the
        # distributed training group is computed here. This is used while initializing
        # the process group using `init_process_group`
        global_rank = node_rank * num_procs_per_node + process_rank

        # Number of processes per node is useful to know if a process
        # is a master in the local node(node in which it is running)
        os.environ["ALLENNLP_PROCS_PER_NODE"] = str(num_procs_per_node)

        # In distributed training, the configured device is always going to be a list.
        # The corresponding gpu id for the particular worker is obtained by picking the id
        # from the device list with the rank as index
        gpu_id = distributed_device_ids[process_rank]  # type: ignore

        # Till now, "cuda_device" might not be set in the trainer params.
        # But a worker trainer needs to only know about its specific GPU id.
        params["trainer"]["cuda_device"] = gpu_id
        params["trainer"]["world_size"] = world_size
        params["trainer"]["distributed"] = True

        torch.cuda.set_device(int(gpu_id))
        dist.init_process_group(
            backend="nccl",
            init_method=f"tcp://{master_addr}:{master_port}",
            world_size=world_size,
            rank=global_rank,
        )
        logging.info(
            f"Process group of world size {world_size} initialized "
            f"for distributed training in worker {global_rank}"
        )

    train_loop = TrainModel.from_params(
        params=params,
        serialization_dir=serialization_dir,
        local_rank=process_rank,
        batch_weight_key=batch_weight_key,
    )

    try:
        if distributed:  # let the setup get ready for all the workers
            dist.barrier()

        metrics = train_loop.run()
    except KeyboardInterrupt:
        # if we have completed an epoch, try to create a model archive.
        if master and os.path.exists(os.path.join(serialization_dir, _DEFAULT_WEIGHTS)):
            logging.info(
                "Training interrupted by the user. Attempting to create "
                "a model archive using the current best epoch weights."
            )
            archive_model(serialization_dir)
        raise

    if master:
        train_loop.finish(metrics)

    if not distributed:
        return train_loop.model

    return None  # to make mypy happy


class TrainModel(Registrable):
    """
    This class exists so that we can easily read a configuration file with the `allennlp train`
    command.  The basic logic is that we call `train_loop =
    TrainModel.from_params(params_from_config_file)`, then `train_loop.run()`.  This class performs
    very little logic, pushing most of it to the `TrainerBase` that has a `train()` method.  The
    point here is to construct all of the dependencies for the `TrainerBase` in a way that we can do
    it using `from_params()`, while having all of those dependencies transparently documented and
    not hidden in calls to `params.pop()`.  If you are writing your own training loop, you almost
    certainly should not use this class, but you might look at the code for this class to see what
    we do, to make writing your training loop easier.

    In particular, if you are tempted to call the `__init__` method of this class, you are probably
    doing something unnecessary.  Literally all we do after `__init__` is call `trainer.train()`.  You
    can do that yourself, if you've constructed a `Trainer` already.  What this class gives you is a
    way to construct the `Trainer` by means of a config file.  The actual constructor that we use
    with `from_params` in this class is `from_partial_objects`.  See that method for a description
    of all of the allowed top-level keys in a configuration file used with `allennlp train`.
    """

    default_implementation = "default"

    def __init__(
        self,
        serialization_dir: str,
        model: Model,
        trainer: TrainerBase,
        evaluation_dataset: Iterable[Instance] = None,
        evaluation_iterator: DataIterator = None,
        evaluate_on_test: bool = False,
        batch_weight_key: str = "",
    ) -> None:
        self.serialization_dir = serialization_dir
        self.model = model
        self.trainer = trainer
        self.evaluation_dataset = evaluation_dataset
        self.evaluation_iterator = evaluation_iterator
        self.evaluate_on_test = evaluate_on_test
        self.batch_weight_key = batch_weight_key

    def run(self) -> Dict[str, Any]:
        return self.trainer.train()

    def finish(self, metrics: Dict[str, Any]):
        if self.evaluation_dataset and self.evaluate_on_test:
            logger.info("The model will be evaluated using the best epoch weights.")
            test_metrics = training_util.evaluate(
                self.model,
                self.evaluation_dataset,
                self.evaluation_iterator,
                cuda_device=self.trainer.cuda_device,
                batch_weight_key=self.batch_weight_key,
            )

            for key, value in test_metrics.items():
                metrics["test_" + key] = value
        elif self.evaluation_dataset:
            logger.info(
                "To evaluate on the test set after training, pass the "
                "'evaluate_on_test' flag, or use the 'allennlp evaluate' command."
            )
        common_util.dump_metrics(
            os.path.join(self.serialization_dir, "metrics.json"), metrics, log=True
        )

    @classmethod
    def from_partial_objects(
        cls,
        serialization_dir: str,
        local_rank: int,
        batch_weight_key: str,
        dataset_reader: DatasetReader,
        train_data_path: str,
        model: Lazy[Model],
        iterator: DataIterator,
        trainer: Lazy[TrainerBase],
        vocabulary: Lazy[Vocabulary] = None,
        datasets_for_vocab_creation: List[str] = None,
        validation_dataset_reader: DatasetReader = None,
        validation_data_path: str = None,
        validation_iterator: DataIterator = None,
        test_data_path: str = None,
        evaluate_on_test: bool = False,
    ) -> "TrainModel":
        """
        This method is intended for use with our `FromParams` logic, to construct a `TrainModel`
        object from a config file passed to the `allennlp train` command.  The arguments to this
        method are the allowed top-level keys in a configuration file (except for the first three,
        which are obtained separately).

        You *could* use this outside of our `FromParams` logic if you really want to, but there
        might be easier ways to accomplish your goal than instantiating `Lazy` objects.  If you are
        writing your own training loop, we recommend that you look at the implementation of this
        method for inspiration and possibly some utility functions you can call, but you very likely
        should not use this method directly.

        The `Lazy` type annotations here are a mechanism for building dependencies to an object
        sequentially - the `TrainModel` object needs data, a model, and a trainer, but the model
        needs to see the data before it's constructed (to create a vocabulary) and the trainer needs
        the data and the model before it's constructed.  Objects that have sequential dependencies
        like this are labeled as `Lazy` in their type annotations, and we pass the missing
        dependencies when we call their `construct()` method, which you can see in the code below.

        # Parameters
        serialization_dir: `str`
            The directory where logs and model archives will be saved.
        local_rank: `int`
        batch_weight_key: `str`
        dataset_reader: `DatasetReader`
            The `DatasetReader` that will be used for training and (by default) for validation.
        train_data_path: `str`
            The file (or directory) that will be passed to `dataset_reader.read()` to construct the
            training data.
        model: `Lazy[Model]`
            The model that we will train.  This is lazy because it depends on the `Vocabulary`;
            after constructing the vocabulary we call `model.construct(vocab=vocabulary)`.
        iterator: `DataIterator`
            The iterator we use to batch instances from the dataset reader at training and (by
            default) validation time.
        trainer: `Lazy[TrainerBase]`
            The `Trainer` that actually implements the training loop.  This is a lazy object because
            it depends on the model that's going to be trained.
        vocabulary: `Lazy[Vocabulary]`, optional (default=None)
            The `Vocabulary` that we will use to convert strings in the data to integer ids (and
            possibly set sizes of embedding matrices in the `Model`).  By default we construct the
            vocabulary from the instances that we read.
        datasets_for_vocab_creation: `List[str]`, optional (default=None)
            If you pass in more than one dataset but don't want to use all of them to construct a
            vocabulary, you can pass in this key to limit it.  Valid entries in the list are
            "train", "validation" and "test".
        validation_dataset_reader: `DatasetReader`, optional (default=None)
            If given, we will use this dataset reader for the validation data instead of
            `dataset_reader`.
        validation_data_path: `str`, optional (default=None)
            If given, we will use this data for computing validation metrics and early stopping.
        validation_iterator: `DataIterator`, optional (default=None)
            If given, we will use this iterator for batching and scheduling instances for the
            validation data, instead of `iterator`.
        test_data_path: `str`, optional (default=None)
            If given, we will use this as test data.  This makes it available for vocab creation by
            default, but nothing else.
        evaluate_on_test: `bool`, optional (default=False)
            If given, we will evaluate the final model on this data at the end of training.  Note
            that we do not recommend using this for actual test data in every-day experimentation;
            you should only very rarely evaluate your model on actual test data.
        """

        datasets = training_util.read_all_datasets(
            train_data_path=train_data_path,
            dataset_reader=dataset_reader,
            validation_dataset_reader=validation_dataset_reader,
            validation_data_path=validation_data_path,
            test_data_path=test_data_path,
        )

        if datasets_for_vocab_creation:
            for key in datasets_for_vocab_creation:
                if key not in datasets:
                    raise ConfigurationError(f"invalid 'dataset_for_vocab_creation' {key}")

        instance_generator = (
            instance
            for key, dataset in datasets.items()
            if not datasets_for_vocab_creation or key in datasets_for_vocab_creation
            for instance in dataset
        )

        vocabulary_ = vocabulary.construct(instances=instance_generator)
        if not vocabulary_:
            vocabulary_ = Vocabulary.from_instances(instance_generator)
        model_ = model.construct(vocab=vocabulary_)

        # Initializing the model can have side effect of expanding the vocabulary.
        # Save the vocab only in the master. In the degenerate non-distributed
        # case, we're trivially the master.
        if common_util.is_master():
            vocabulary_path = os.path.join(serialization_dir, "vocabulary")
            vocabulary_.save_to_files(vocabulary_path)

        iterator.index_with(model_.vocab)
        validation_iterator = validation_iterator or iterator
        validation_iterator.index_with(model_.vocab)  # it is ok to call this twice

        # We don't need to pass serialization_dir and local_rank here, because they will have been
        # passed through the trainer by from_params already, because they were keyword arguments to
        # construct this class in the first place.
        trainer_ = trainer.construct(
            model=model_,
            iterator=iterator,
            train_data=datasets["train"],
            validation_iterator=validation_iterator,
            validation_data=datasets.get("validation"),
        )

        return cls(
            serialization_dir=serialization_dir,
            model=model_,
            trainer=trainer_,
            evaluation_dataset=datasets.get("test"),
            evaluation_iterator=validation_iterator,
            evaluate_on_test=evaluate_on_test,
            batch_weight_key=batch_weight_key,
        )


TrainModel.register("default", constructor="from_partial_objects")(TrainModel)
