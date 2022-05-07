import os
from typing import Any, Dict, List, Optional, Tuple, Type, TypeVar, Union, cast

import torch
import torch.distributed as dist
import torch.multiprocessing as mp
from pytorch_lightning.utilities.seed import seed_everything
from torch import Tensor
from tqdm import tqdm


def init_process(rank, size, fn, backend, *args):
    """Initialize the distributed environment."""
    os.environ["MASTER_ADDR"] = "127.0.0.1"
    os.environ["MASTER_PORT"] = "9000"
    dist.init_process_group(backend, rank=rank, world_size=size)
    fn(rank, size, *args)


class LayerParallelTrainer:
    """
    Multi-GPU layer parallel trainer for DTP.

    Works with DTP layer parallel model, can be tested with the following command:
    python main.py model=layer_parallel_dtp trainer=layer_parallel scheduler=cosine network=simple_vgg datamodule=cifar10 trainer.gpus=6 datamodule.num_workers=1

    For imagenet32, do:
    python main.py model=layer_parallel_dtp \
    trainer=layer_parallel \
    scheduler=cosine \
    network=simple_vgg \
    datamodule=imagenet32 \
    datamodule.num_workers=1 \
    trainer.gpus=6 \
    datamodule.batch_size=256 \
    model.hparams.feedback_training_iterations=[25,35,40,60,25] \
    model.hparams.f_optim.lr=0.01 \
    model.hparams.b_optim.momentum=0.9 \
    model.hparams.b_optim.lr=[1e-4,3.5e-4,8e-3,8e-3,0.18]
    """

    def __init__(self, gpus, max_epochs, seed) -> None:
        self.max_epochs = max_epochs
        self.gpus = gpus
        self.seed = seed
        # self.device = "cpu" if not torch.cuda.is_available() else torch.cuda.current_device()

    def fit(self, model, datamodule):
        # setup distributed processes
        processes = []
        mp.set_start_method("spawn")
        # number of layers must be equal to number of processes for layer parallel feedback weight training
        size = len(model.backward_net)
        for rank in range(size):
            p = mp.Process(
                target=init_process,
                args=(rank, size, self.fit_worker, "gloo", model, datamodule),
            )
            p.start()
            processes.append(p)
        for p in processes:
            p.join()

    def fit_worker(self, rank, size, model, datamodule):
        self.device = torch.device(
            "cuda:{}".format(rank % self.gpus)
        )  # set different GPU for each process
        print(f"[rank {rank}] {self.seed}")

        # we set same seed for each process since we want to have exact same
        # batch on every process, we just parallelize the feedback training not data
        seed_everything(self.seed, workers=True)

        # broadcast params from rank 0 to all other processes just to be safe
        for param in model.parameters():
            dist.broadcast(param.data, src=0)

        datamodule.setup(stage="fit")
        dist.barrier()  # wait for dataloading on all processes to finish
        # test if you get same batches
        # batch = next(iter(datamodule.train_dataloader()))
        # print(f"rank {rank}, seed {self.seed}, batch labels: {batch[1]}")

        optim_config = model.configure_optimizers()
        self.setup_optim(optim_config)
        scheduler = self._schedulers[0]
        model = model.to(self.device)
        model.trainer = self  # set trainer as model's attribute like lightning

        # training loop
        for epoch in range(self.max_epochs):
            # run training epoch
            self.train_epoch(model, datamodule.train_dataloader(), optim_config)

            # evaluate model on validation set
            metric = self.val_epoch(model, datamodule.val_dataloader())

            # scheduler step
            scheduler.step()
            print(f"epoch: {epoch}, val metric: {metric}")

    def train_epoch(self, model, train_dataloader, optim_config):
        model.train()
        model.optimizers = self.optimizers
        model.lr_schedulers = self.lr_schedulers
        losses = []
        rank = dist.get_rank()
        if rank == 0:
            pbar = tqdm(total=len(train_dataloader))

        for step, batch in enumerate(train_dataloader):
            # transfer batch to device
            batch = tuple(t.to(device=self.device) for t in batch)

            # feedback weight training for a layer corresponding to rank
            x, y = batch
            feedback_training_outputs: Dict = model.feedback_loss(x, y, rank=rank, phase="train")

            # for i in range(self.gpus):
            #     if i == self.gpus - 1:
            #         print(
            #             f"[rank {rank}][step {step}] feedback net param layer {i} {model.backward_net[::-1][i][0].weight.data.view(-1)[:10]}"
            #         )
            #     else:
            #         print(
            #             f"[rank {rank}][step {step}] feedback net param layer {i} {model.backward_net[::-1][i][-1].weight.data.view(-1)[:10]}"
            #         )
            # sync feedback net params
            updated_param_list = [None for _ in range(dist.get_world_size())]
            dist.all_gather_object(updated_param_list, model.backward_net[::-1][rank].state_dict())
            for i, layer in enumerate(model.backward_net[::-1]):
                layer.load_state_dict(updated_param_list[i])

            # for i in range(self.gpus):
            #     if i == self.gpus - 1:
            #         print(
            #             f"[rank {rank}][step {step}] updated feedback net param layer {i} {model.backward_net[::-1][i][0].weight.data.view(-1)[:10]}"
            #         )
            #     else:
            #         print(
            #             f"[rank {rank}][step {step}] updated feedback net param layer {i} {model.backward_net[::-1][i][-1].weight.data.view(-1)[:10]}"
            #         )
            # broadcast forward params from rank 0 to be safe
            # (not necessary)

            # forward weight training
            # print(
            #     f"[rank {rank}][step {step}] forward net param layer {i} {model.forward_net[2][0].weight.data.view(-1)[:10]}"
            # )
            forward_training_outputs: Dict = model.forward_loss(x, y, rank=rank, phase="train")
            forward_loss: Tensor = forward_training_outputs["loss"]
            forward_optimizer = model.forward_optimizer
            forward_optimizer.zero_grad()
            forward_loss.backward()
            forward_optimizer.step()
            # self.logger.log(f"F_lr", forward_optimizer.param_groups[0]["lr"])
            forward_loss = forward_loss.detach()
            last_layer_loss: Tensor = forward_training_outputs["layer_losses"][-1].detach()
            if rank == 0:
                pbar.set_description("loss {:0.4f}".format(last_layer_loss.item()))
                pbar.update(1)
                # print(f"[rank {rank}][step {step}] forward loss {last_layer_loss.item()}")
            losses.append(last_layer_loss.item())
        return torch.tensor(losses).mean()

    def setup_optim(self, optim_config):
        self._optimizers = []
        self._schedulers = []
        for config in optim_config:
            optimizer = config["optimizer"]
            self._optimizers.append(optimizer)
            if "lr_scheduler" in config:
                scheduler = config["lr_scheduler"]["scheduler"]
                self._schedulers.append(scheduler)

    def optimizers(self):
        """lightning-like method to get optimizers."""
        return self._optimizers

    def lr_schedulers(self):
        return self._schedulers

    def val_epoch(self, model, val_dataloader):
        model.eval()
        metrics = []

        for step, batch in enumerate(val_dataloader):
            # transfer batch to device
            batch = tuple(t.to(device=self.device) for t in batch)

            # forward pass
            metric = model.validation_step(batch, step)
            metrics.append(metric.item())

        return torch.tensor(metrics).mean()

    def test(self, model, datamodule, verbose=False):
        # verbose argument is just a dummy argument to match lightning format
        datamodule.setup(stage="test")
        return self.val_epoch(model, datamodule.test_dataloader())