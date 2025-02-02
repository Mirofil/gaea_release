# python cnn/train_search.py mode=search_nasbench201 nas_algo=edarts search_config=method_edarts_space_nasbench201 run.seed=1 run.epochs=25 run.dataset=cifar10 search.single_level=true search.exclude_zero=true
# python cnn/train_search.py mode=search_pcdarts nas_algo=eedarts search_config=method_eedarts_space_pcdarts run.seed=1 run.epochs=50 run.dataset=cifar10 search.single_level=false search.exclude_zero=false

import os
import sys
import time
import glob
import numpy as np
import torch
import train_utils
import aws_utils
import random
import copy
import logging
import hydra
import pickle
import torch.nn as nn
import torch.utils
import torch.nn.functional as F
import time
from tqdm import tqdm
from torch.autograd import Variable
from torch.utils.tensorboard import SummaryWriter
from search_spaces.darts.genotypes import PRIMITIVES, count_ops
import nasbench301 as nb

import wandb
from pathlib import Path
lib_dir = (Path(__file__).parent / '..' / 'code' / 'AutoDL').resolve()
if str(lib_dir) not in sys.path: sys.path.insert(0, str(lib_dir))


def get_torch_home():
    if "TORCH_HOME" in os.environ:
        return os.environ["TORCH_HOME"]
    elif "HOME" in os.environ:
        return os.path.join(os.environ["HOME"], ".torch")
    else:
        raise ValueError(
            "Did not find HOME in os.environ. "
            "Please at least setup the path of HOME or TORCH_HOME "
            "in the environment."
        )

def wandb_auth(fname: str = "nas_key.txt"):
  gdrive_path = "/content/drive/MyDrive/colab/wandb/nas_key.txt"
  if "WANDB_API_KEY" in os.environ:
      wandb_key = os.environ["WANDB_API_KEY"]
  elif os.path.exists(os.path.abspath("~" + os.sep + ".wandb" + os.sep + fname)):
      # This branch does not seem to work as expected on Paperspace - it gives '/storage/~/.wandb/nas_key.txt'
      print("Retrieving WANDB key from file")
      f = open("~" + os.sep + ".wandb" + os.sep + fname, "r")
      key = f.read().strip()
      os.environ["WANDB_API_KEY"] = key
  elif os.path.exists("/root/.wandb/"+fname):
      print("Retrieving WANDB key from file")
      f = open("/root/.wandb/"+fname, "r")
      key = f.read().strip()
      os.environ["WANDB_API_KEY"] = key

  elif os.path.exists(
      os.path.expandvars("%userprofile%") + os.sep + ".wandb" + os.sep + fname
  ):
      print("Retrieving WANDB key from file")
      f = open(
          os.path.expandvars("%userprofile%") + os.sep + ".wandb" + os.sep + fname,
          "r",
      )
      key = f.read().strip()
      os.environ["WANDB_API_KEY"] = key
  elif os.path.exists(gdrive_path):
      print("Retrieving WANDB key from file")
      f = open(gdrive_path, "r")
      key = f.read().strip()
      os.environ["WANDB_API_KEY"] = key
  wandb.login()

  
def load_nb301():
    version = '0.9'
    # current_dir = os.path.dirname(os.path.abspath(__file__))
    current_dir = os.path.dirname(get_torch_home())

    models_0_9_dir = os.path.join(current_dir, 'nb_models_0.9')
    model_paths_0_9 = {
        model_name : os.path.join(models_0_9_dir, '{}_v0.9'.format(model_name))
        for model_name in ['xgb', 'gnn_gin', 'lgb_runtime']
    }
    models_1_0_dir = os.path.join(current_dir, 'nb_models_1.0')
    model_paths_1_0 = {
        model_name : os.path.join(models_1_0_dir, '{}_v1.0'.format(model_name))
        for model_name in ['xgb', 'gnn_gin', 'lgb_runtime']
    }
    model_paths = model_paths_0_9 if version == '0.9' else model_paths_1_0

    # If the models are not available at the paths, automatically download
    # the models
    # Note: If you would like to provide your own model locations, comment this out
    if not all(os.path.exists(model) for model in model_paths.values()):
        nb.download_models(version=version, delete_zip=True,
                        download_dir=current_dir)

    # Load the performance surrogate model
    #NOTE: Loading the ensemble will set the seed to the same as used during training (logged in the model_configs.json)
    #NOTE: Defaults to using the default model download path
    print("==> Loading performance surrogate model...")
    ensemble_dir_performance = model_paths['xgb']
    print(ensemble_dir_performance)
    performance_model = nb.load_ensemble(ensemble_dir_performance)
    
    return performance_model

def count_ops_nb201(arch):
  ops = ['none', 'skip_connect', 'nor_conv_1x1', 'nor_conv_3x3', 'avg_pool_3x3']
  arch_str = str(arch)
  counts = {op: arch_str.count(op) for op in ops}
  return counts

@hydra.main(config_path="../configs/cnn/config.yaml", strict=False)
def main(args):
    
    np.set_printoptions(precision=3)
    
    # if os.path.exists('/storage/gaea_release'):
    #     save_dir = '/storage/gaea_release/'
    # else:
    #     save_dir = os.getcwd()
    #     save_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)), save_dir)
    save_dir = f'/storage/gaea_release/exps/{args.run.seed}-{args.run.dataset}-{args.search.method}'
    try:
        os.makedirs(save_dir)
    except Exception as e:
        print(f"Failed to make dir due to {e}")
    logging.info(f"Save dir: {save_dir}")

    log = os.path.join(save_dir, "log.txt")

    wandb_auth()
    run = wandb.init(project="NAS", group=f"Search_Cell_gaea", reinit=True)
    wandb.config.update(args)

    # Setup SummaryWriter
    summary_dir = os.path.join(save_dir, "summary")
    if not os.path.exists(summary_dir):
        os.mkdir(summary_dir)
    writer = SummaryWriter(summary_dir)

    if args.run.s3_bucket is not None:
        aws_utils.download_from_s3(log, args.run.s3_bucket, log)

        train_utils.copy_code_to_experiment_dir("/code/nas-theory/cnn", save_dir)
        aws_utils.upload_directory(
            os.path.join(save_dir, "scripts"), args.run.s3_bucket
        )

    train_utils.set_up_logging(log)

    if not torch.cuda.is_available():
        logging.info("no gpu device available")
        sys.exit(1)

    torch.cuda.set_device(args.run.gpu)
    logging.info("gpu device = %d" % args.run.gpu)
    logging.info("args = %s", args.pretty())

    rng_seed = train_utils.RNGSeed(args.run.seed)

    if args.search.method in ["edarts", "gdarts", "eedarts"]:
        if args.search.fix_alphas:
            from architect.architect_edarts_edge_only import (
                ArchitectEDARTS as Architect,
            )
        else:
            from architect.architect_edarts import ArchitectEDARTS as Architect
    elif args.search.method in ["darts", "fdarts"]:
        from architect.architect_darts import ArchitectDARTS as Architect
    elif args.search.method == "egdas":
        from architect.architect_egdas import ArchitectEGDAS as Architect
    else:
        raise NotImplementedError

    if args.search.search_space in ["darts", "darts_small"]:
        from search_spaces.darts.model_search import DARTSNetwork as Network
    elif "nas-bench-201" in args.search.search_space:
        from search_spaces.nasbench_201.model_search import (
            NASBENCH201Network as Network,
        )
    elif args.search.search_space == "pcdarts":
        from search_spaces.pc_darts.model_search import PCDARTSNetwork as Network
    else:
        raise NotImplementedError

    if args.train.smooth_cross_entropy:
        criterion = train_utils.cross_entropy_with_label_smoothing
    else:
        criterion = nn.CrossEntropyLoss()

    num_train, num_classes, train_queue, valid_queue = train_utils.create_data_queues(
        args
    )

    print("dataset: {}, num_classes: {}".format(args.run.dataset, num_classes))

    model = Network(
        args.train.init_channels,
        num_classes,
        args.search.nodes,
        args.train.layers,
        criterion,
        **{
            "auxiliary": args.train.auxiliary,
            "search_space_name": args.search.search_space,
            "exclude_zero": args.search.exclude_zero,
            "track_running_stats": args.search.track_running_stats,
        }
    )
    model = model.cuda()
    logging.info("param size = %fMB", train_utils.count_parameters_in_MB(model))

    optimizer, scheduler = train_utils.setup_optimizer(model, args)
    
    print(args)
    if "nas-bench-201" not in args.search.search_space:
        api = load_nb301()
    else:
        from nats_bench   import create
        api = create(None, 'topology', fast_mode=True, verbose=False)


    # TODO: separate args by model, architect, etc
    # TODO: look into using hydra for config files
    architect = Architect(model, args, writer)

    # Try to load a previous checkpoint
    try:
        start_epochs, history = train_utils.load(
            save_dir, rng_seed, model, optimizer, architect, args.run.s3_bucket
        )
        scheduler.last_epoch = start_epochs - 1
        (
            num_train,
            num_classes,
            train_queue,
            valid_queue,
        ) = train_utils.create_data_queues(args)
    except Exception as e:
        logging.info(e)
        start_epochs = 0

    best_valid = 0
    for epoch in tqdm(range(start_epochs, args.run.epochs), desc="Iterating over epochs"):
        lr = scheduler.get_lr()[0]
        logging.info("epoch %d lr %e", epoch, lr)

        model.drop_path_prob = args.train.drop_path_prob * epoch / args.run.epochs

        # training
        train_acc, train_obj = train(
            args, train_queue, valid_queue, model, architect, criterion, optimizer, lr,
        )
        architect.baseline = train_obj
        architect.update_history()
        architect.log_vars(epoch, writer)

        if "update_lr_state" in dir(scheduler):
            scheduler.update_lr_state(train_obj)

        logging.info("train_acc %f", train_acc)

        # History tracking
        for vs in [("alphas", architect.alphas), ("edges", architect.edges)]:
            for ct in vs[1]:
                v = vs[1][ct]
                logging.info("{}-{}".format(vs[0], ct))
                logging.info(v)
        # Calling genotypes sets alphas to best arch for EGDAS and MEGDAS
        # so calling here before infer.
        genotype = architect.genotype()
        logging.info("genotype = %s", genotype)
        
        if "nas-bench-201" not in args.search.search_space:
            genotype_perf = api.predict(config=genotype, representation='genotype', with_noise=False)
            ops_count = count_ops(genotype)
            width = {k: train_utils.genotype_width(getattr(genotype, k)) for k in ["normal", "reduce"]}
            depth = {k: train_utils.genotype_depth(getattr(genotype, k)) for k in ["normal", "reduce"]}
        else:
            index = api.query_index_by_arch(genotype)
            datasets = ["cifar10", "cifar10-valid", "cifar100", "ImageNet16-120"]
            results = {dataset: {} for dataset in datasets}
            for dataset in datasets:
                results[dataset] = api.get_more_info(
                    index, dataset, iepoch=199, hp='200', is_random=False
                )
            for dataset in datasets:
                if (
                    "test-accuracy" in results[dataset].keys()
                ):  # Actually it seems all the datasets have this field?
                    results[dataset] = results[dataset]["test-accuracy"]
                
            genotype_perf = results
            ops_count = count_ops_nb201(genotype)
            width = None
            depth = None
        logging.info(f"Genotype performance: {genotype_perf}, ops_count: {ops_count}, width: {width}, depth: {depth}")

        if not args.search.single_level:
            valid_acc, valid_obj = train_utils.infer(
                valid_queue,
                model,
                criterion,
                report_freq=args.run.report_freq,
                discrete=args.search.discrete,
            )
            if valid_acc > best_valid:
                best_valid = valid_acc
                best_genotype = architect.genotype()
            logging.info("valid_acc %f", valid_acc)
        else:
            valid_acc, valid_obj = -1, -1

        if "nas-bench-201" not in args.search.search_space:

            wandb_log = {"train_acc":train_acc, "train_loss":train_obj, "val_acc": valid_acc, "valid_loss":valid_obj, 
                        "search.final.cifar10": genotype_perf, "epoch":epoch, "ops": ops_count, "width":width, "depth": depth}
        else:
            wandb_log = {"train_acc":train_acc, "train_loss":train_obj, "val_acc": valid_acc, "valid_loss":valid_obj, 
                "search.final": genotype_perf, "epoch":epoch, "ops": ops_count, "width":width, "depth": depth}
        wandb.log(wandb_log)
        
        train_utils.save(
            save_dir,
            epoch + 1,
            rng_seed,
            model,
            optimizer,
            architect,
            save_history=True,
            s3_bucket=args.run.s3_bucket,
        )

        scheduler.step()

    valid_acc, valid_obj = train_utils.infer(
        valid_queue,
        model,
        criterion,
        report_freq=args.run.report_freq,
        discrete=args.search.discrete,
    )
    if valid_acc > best_valid:
        best_valid = valid_acc
        best_genotype = architect.genotype()
    logging.info("valid_acc %f", valid_acc)

    if args.run.s3_bucket is not None:
        filename = "cnn_genotypes.txt"
        aws_utils.download_from_s3(filename, args.run.s3_bucket, filename)

        with open(filename, "a+") as f:
            f.write("\n")
            f.write(
                "{}{}{}{} = {}".format(
                    args.search.search_space,
                    args.search.method,
                    args.run.dataset.replace("-", ""),
                    args.run.seed,
                    best_genotype,
                )
            )
        aws_utils.upload_to_s3(filename, args.run.s3_bucket, filename)
        aws_utils.upload_to_s3(log, args.run.s3_bucket, log)


def train(
    args,
    train_queue,
    valid_queue,
    model,
    architect,
    criterion,
    optimizer,
    lr,
    random_arch=False,
):
    objs = train_utils.AvgrageMeter()
    top1 = train_utils.AvgrageMeter()
    top5 = train_utils.AvgrageMeter()

    for step, datapoint in enumerate(train_queue):

        # The search dataqueue for nas-bench-201  returns both train and valid data
        # when looping through queue.  This is disabled with single level is indicated.
        if "nas-bench-201" in args.search.search_space and not (
            args.search.single_level
        ):
            input, target, input_search, target_search = datapoint
        else:
            input, target = datapoint
            input_search, target_search = next(iter(valid_queue))

        n = input.size(0)

        input = Variable(input, requires_grad=False).cuda()
        target = Variable(target, requires_grad=False).cuda()

        # get a random minibatch from the search queue with replacement
        input_search = Variable(input_search, requires_grad=False).cuda()
        target_search = Variable(target_search, requires_grad=False).cuda()

        model.train()

        # TODO: move architecture args into a separate dictionary within args
        if not random_arch:
            architect.step(
                input,
                target,
                input_search,
                target_search,
                **{
                    "eta": lr,
                    "network_optimizer": optimizer,
                    "unrolled": args.search.unrolled,
                    "update_weights": True,
                }
            )
        # if random_arch or model.architect_type == "snas":
        #    architect.sample_arch_configure_model()

        optimizer.zero_grad()
        architect.zero_arch_var_grad()
        architect.set_model_alphas()
        architect.set_model_edge_weights()

        logits, logits_aux = model(input, discrete=args.search.discrete)
        loss = criterion(logits, target)
        if args.train.auxiliary:
            loss_aux = criterion(logits_aux, target)
            loss += args.train.auxiliary_weight * loss_aux

        loss.backward()
        nn.utils.clip_grad_norm(model.parameters(), args.train.grad_clip)
        optimizer.step()

        prec1, prec5 = train_utils.accuracy(logits, target, topk=(1, 5))
        objs.update(loss.item(), n)
        top1.update(prec1.item(), n)
        top5.update(prec5.item(), n)

        if step % args.run.report_freq == 0:
            logging.info("train %03d %e %f %f", step, objs.avg, top1.avg, top5.avg)

    return top1.avg, objs.avg


if __name__ == "__main__":
    if 'TORCH_HOME' not in os.environ:
        print("Changing os environ")
        if os.path.exists('/storage/.torch/'):
            os.environ["TORCH_HOME"] = '/storage/.torch/'

        gdrive_torch_home = "/content/drive/MyDrive/colab/data/TORCH_HOME"

        if os.path.exists(gdrive_torch_home):
            os.environ["TORCH_HOME"] = "/content/drive/MyDrive/colab/data/TORCH_HOME"
    main()
